"""Local HTTP FLV proxy that bridges VRS WebSocket live streams to HA's stream component.

HA's bundled PyAV cannot speak the VRS WebSocket handshake, so this proxy (running inside
HA Core on 127.0.0.1) keeps one upstream WebSocket per watched device and re-serves the FLV
over plain HTTP.  ``Camera.stream_source`` points at ``http://127.0.0.1:<port>/live/<id>.flv``.

The VRS server caps a live session at roughly 60 seconds and then closes the socket; the web
player simply reconnects.  Each new session restarts with its own ``FLV`` header and its own
timestamp base, and interleaves proprietary ``0x61`` metadata tags.  Feeding that verbatim to
PyAV produces "Invalid data found" / "Timestamp discontinuity" errors.  :class:`FlvRewriter`
therefore rebuilds a single continuous FLV: it strips repeated headers, drops ``0x61`` tags,
and remaps tag timestamps so they keep increasing across session boundaries.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time

from aiohttp import web

from .vrs_live import TalkSendError, VrsLiveClient

_LOGGER = logging.getLogger(__name__)

TAG_AUDIO = 8
TAG_VIDEO = 9
TAG_SCRIPT = 18
TAG_VRS_META = 0x61  # proprietary VRS "liveTimestamp" metadata tag
# Largest raw timestamp step we accept as genuine.  The VRS video clock jumps
# backwards/forwards by tens of seconds about once per GOP (observed +-16..57 s):
# naive forwarding makes HA's TimestampValidator drop every IDR (step <= 0) so
# segments never roll and playback freezes.  Anomalous steps are therefore
# replaced by the stream's nominal frame interval (tracked per tag type), which
# keeps muxer DTS strictly increasing and paced at realtime.  Audio steps
# ~20 ms and video ~67 ms, so 2 s is far above anything legitimate.
MAX_DELTA_MS = 2_000
# Fallback frame interval per tag type until the first genuine step is observed.
NOMINAL_STEP_MS = {8: 20, 9: 67}
HISTORY_LIMIT = 512 * 1024
# Upstream session robustness: the VRS server caps a live session (~60-100 s
# observed) and occasionally goes quiet without closing it.  A standby session
# is pre-connected before the cap (seamless, keyframe-aligned handover); a
# silence watchdog converts quiet-but-open sessions into reconnects.
PRECONNECT_AGE = 50.0
WATCHDOG_SILENCE = 10.0
STANDBY_BUFFER_LIMIT = 2 * 1024 * 1024
STANDBY_RETRY_COOLDOWN = 5.0
# The camera's G.711 audio clock runs slower than its video clock and drifts
# further every session; muxing both into one timeline leaves HA's muxer out of
# sync (see evidence/E-011).  Video-only is therefore the default; audio can be
# enabled per setup via the integration options (FlvProxy keep_audio flag) for
# listening, with the drift caveat.
KEEP_AUDIO = False
# FLV header: signature, version 1, flags, 9-byte data offset, PreviousTagSize0 = 0.


def flv_header(keep_audio: bool) -> bytes:
    return (
        b"FLV\x01" + bytes([0x05 if keep_audio else 0x01])
        + b"\x00\x00\x00\x09\x00\x00\x00\x00"
    )


# Backwards-compatible module constant (video-only header).
FLV_HEADER = flv_header(False)


def _tag_total(tag_type: int, size: int) -> int:
    return 11 + size + 4


def _strip_audio_metadata(tag: bytes) -> bytes:
    """Strip the ``audiocodecid`` entry from an onMetaData script tag.

    The VRS server always advertises G.711 audio + AVC video, but the proxy
    forwards video-only when ``keep_audio`` is False.  Passing the audio claim
    through while sending no audio tags (and a video-only FLV header) is
    inconsistent metadata that strict FLV readers can trip over.  Rebuild a
    video-only metadata on a best-effort basis; unrecognised AMF layouts pass
    through untouched.
    """
    body = tag[11:11 + int.from_bytes(tag[1:4], "big")]
    off = 0
    if len(body) < 5 or body[off] != 0x02:
        return tag  # not an AMF string root
    namelen = int.from_bytes(body[off + 1:off + 3], "big")
    name = body[off + 3:off + 3 + namelen]
    if name != b"onMetaData":
        return tag
    off = 3 + namelen
    if body[off] != 0x08:
        return tag  # not an ECMA array
    count = int.from_bytes(body[off + 1:off + 5], "big")
    off += 5
    entries = []
    for _ in range(count):
        if off + 2 > len(body):
            return tag
        klen = int.from_bytes(body[off:off + 2], "big")
        key = body[off + 2:off + 2 + klen]
        off += 2 + klen
        if off >= len(body):
            return tag
        marker = body[off]
        if marker == 0x00:  # number
            end = off + 9
        elif marker == 0x01:  # boolean
            end = off + 2
        elif marker == 0x02:  # string
            slen = int.from_bytes(body[off + 1:off + 3], "big")
            end = off + 3 + slen
        else:
            return tag
        if end > len(body):
            return tag
        entries.append((key, body[off:end]))
        off = end
    if off < len(body):
        if body[off:off + 3] != b"\x00\x00\x09":  # object-end terminator
            return tag
    keep = [e for e in entries if e[0] != b"audiocodecid"]
    if len(keep) == len(entries):
        return tag
    new_body = b"\x02" + namelen.to_bytes(2, "big") + name
    new_body += b"\x08" + len(keep).to_bytes(4, "big")
    for key, value in keep:
        new_body += len(key).to_bytes(2, "big") + key + value
    new_body += b"\x00\x00\x09"
    total = 11 + len(new_body) + 4
    out = bytearray(tag)
    out[1:4] = len(new_body).to_bytes(3, "big")
    out[11:11 + len(new_body)] = new_body
    out = out[:total]
    out[total - 4:total] = (11 + len(new_body)).to_bytes(4, "big")
    return bytes(out)


def _has_video_keyframe(data: bytes) -> bool:
    """True if raw FLV bytes contain a complete AVC IDR (NALU) tag.

    Conservative: returns True only on positive confirmation (used to align
    standby handover); misalignment or trailing partials yield False.
    """
    i = 0
    n = len(data)
    if n >= 13 and data[:3] == b"FLV":
        i = 13
    while i + 11 <= n:
        if data[i:i + 3] == b"FLV" and i + 13 <= n:
            i += 13
            continue
        size = (data[i + 1] << 16) | (data[i + 2] << 8) | data[i + 3]
        if size < 2 or size > 4_000_000:
            return False
        if i + 11 + size + 4 > n:
            return False  # trailing partial tag
        if (data[i] == TAG_VIDEO and (data[i + 11] >> 4) == 1
                and data[i + 12] == 1):
            return True
        i += 11 + size + 4
    return False


class FlvRewriter:
    """Turns a sequence of VRS FLV sessions into one continuous FLV tag stream."""

    def __init__(self, keep_audio: bool = KEEP_AUDIO) -> None:
        self._buf = bytearray()
        self._state: dict[int, dict[str, int]] = {}
        self._new_session = False
        self._seen_script = False
        self._session_start = 0
        self._keep_audio = keep_audio
        self._aac = None
        self._aac_failed = False
        self._aac_seq_pending = True

    def reset(self) -> None:
        """Drop any partial tag left over from a truncated session."""
        self._buf.clear()
        self._new_session = True
        if self._aac is not None:
            self._aac.reset_session()
        self._aac_seq_pending = True

    def _anchor_session(self) -> None:
        # Anchor every stream to one clock so audio/video cannot drift apart
        # session after session.  Start where the furthest-along stream left
        # off, plus 1 ms: HA's TimestampValidator drops packets with
        # dts <= previous, so emitting exactly last_out would discard the
        # session-start IDR and leave the muxer with unreferenced P-frames
        # until the next IDR.
        self._session_start = (
            max((state["last_out"] for state in self._state.values()), default=0)
            + 1
        )
        for state in self._state.values():
            state["base"] = None
        self._new_session = False

    def _rewrite_audio(self, tag: bytes) -> bytes:
        """Transcode a G.711A audio tag to AAC FLV tag(s)."""
        from . import audio_transcode

        size = (tag[1] << 16) | (tag[2] << 8) | tag[3]
        if size < 2 or (tag[11] >> 4) != 7:
            return b""  # not G.711 A-law; never seen, drop rather than break A/V
        if self._new_session:
            self._anchor_session()
        if self._aac is None and not self._aac_failed:
            try:
                self._aac = audio_transcode.AlawToAac()
            except Exception as err:  # noqa: BLE001 (PyAV missing/broken?)
                self._aac_failed = True
                _LOGGER.warning("ease_life: audio transcode unavailable: %s", err)
                return b""
        if self._aac is None:
            return b""
        raw = (tag[7] << 24) | (tag[4] << 16) | (tag[5] << 8) | tag[6]
        state = self._state.get(TAG_AUDIO)
        if state is None:
            state = self._state[TAG_AUDIO] = {
                "base": None, "last_raw": 0, "last_out": 0, "step": 20,
            }
        last_out = state["last_out"]
        if state["base"] is None:
            state["base"] = raw
            state["last_raw"] = raw
            out_ts_in = self._session_start
        else:
            step = raw - state["last_raw"]
            if 0 < step <= MAX_DELTA_MS:
                state["step"] = step
            else:
                step = state["step"]
            state["last_raw"] = raw
            out_ts_in = last_out + step
        if out_ts_in > last_out + MAX_DELTA_MS:
            out_ts_in = last_out + MAX_DELTA_MS
        if out_ts_in < last_out:
            out_ts_in = last_out
        state["last_out"] = out_ts_in
        # NOTE: snapshot the ASC timestamp BEFORE feeding: feed() advances the
        # frame counter, and the header must sort strictly before any frame
        # emitted from this batch (else HA drops the frame as dts<=prev).
        asc_ts = self._aac.asc_timestamp()
        try:
            frames = self._aac.feed(tag[12:11 + size], out_ts_in)
        except Exception as err:  # noqa: BLE001 (protect video on audio errors)
            _LOGGER.warning("ease_life: audio frame dropped: %s", err)
            return b""
        out = bytearray()
        if self._aac_seq_pending and self._aac.asc and asc_ts is not None:
            out += audio_transcode.make_audio_tag(
                b"\xaf\x00" + self._aac.asc, asc_ts)
            self._aac_seq_pending = False
        for ts, au in frames:
            out += audio_transcode.make_audio_tag(b"\xaf\x01" + au, ts)
        return bytes(out)

    def feed(self, data: bytes) -> bytes:
        buf = self._buf
        buf += data
        out = bytearray()
        while True:
            if len(buf) >= 3 and buf[:3] == b"FLV":
                if len(buf) < 13:
                    break
                del buf[:13]
                self._new_session = True
                continue
            if len(buf) < 11:
                break
            size = (buf[1] << 16) | (buf[2] << 8) | buf[3]
            total = _tag_total(buf[0], size)
            if len(buf) < total:
                break
            tag = bytes(buf[:total])
            del buf[:total]
            rewritten = self._rewrite(tag)
            if rewritten:
                out += rewritten
        return bytes(out)

    def _rewrite(self, tag: bytes) -> bytes:
        tag_type = tag[0]
        if tag_type == TAG_VRS_META:
            return b""
        if tag_type == TAG_SCRIPT:
            if self._seen_script:
                return b""
            self._seen_script = True
            if self._keep_audio:
                return tag
            return _strip_audio_metadata(tag)
        if tag_type == TAG_AUDIO and not self._keep_audio:
            return b""
        if tag_type == TAG_AUDIO:
            return self._rewrite_audio(tag)
        if tag_type not in (TAG_AUDIO, TAG_VIDEO):
            return b""
        size = (tag[1] << 16) | (tag[2] << 8) | tag[3]
        if tag_type == TAG_VIDEO and size >= 2 and tag[11] == 0x17 and tag[12] == 0:
            # AVC sequence header: redundant (every IDR carries in-band SPS/PPS,
            # verified against live captures) and the session-start instance
            # carries a bogus timestamp.  Forwarding it as a 50-byte "keyframe"
            # risks HA muxing it as a sync sample, so drop it; ffmpeg builds
            # extradata from the in-band parameter sets instead.
            return b""
        if self._new_session:
            self._anchor_session()
        raw = (tag[7] << 24) | (tag[4] << 16) | (tag[5] << 8) | tag[6]
        state = self._state.get(tag_type)
        if state is None:
            state = self._state[tag_type] = {
                "base": None,
                "last_raw": 0,
                "last_out": 0,
                "step": NOMINAL_STEP_MS.get(tag_type, 67),
            }
        last_out = state["last_out"]
        if state["base"] is None:
            # First media tag of a session (or of the stream): anchor the raw
            # clock here no matter how bogus it looks.  The bogus value never
            # reaches the output; the stream keeps the session-start clock.
            state["base"] = raw
            state["last_raw"] = raw
            out_ts = self._session_start
        else:
            step = raw - state["last_raw"]
            if 0 < step <= MAX_DELTA_MS:
                state["step"] = step
            else:
                # Clock jump (the VRS video clock resets about once per GOP)
                # or a straight duplicate: pace at the nominal frame interval
                # so muxer DTS stays strictly increasing and realtime-paced.
                # Emitting last_out (step 0) would make HA drop the packet --
                # for an IDR that means a keyframeless stream that never rolls
                # segments and freezes playback.
                step = state["step"]
            state["last_raw"] = raw
            out_ts = last_out + step
        if out_ts > last_out + MAX_DELTA_MS:
            out_ts = last_out + MAX_DELTA_MS
        if out_ts < last_out:
            out_ts = last_out
        state["last_out"] = out_ts
        if (self._keep_audio and size >= 2 and tag[11] >> 4 == 1
                and tag[12] == 1):
            # Video IDR: ask for a fresh AAC sequence header so any subscriber
            # whose history starts here gets audio init before audio data.
            self._aac_seq_pending = True
        rewritten = bytearray(tag)
        rewritten[4] = (out_ts >> 16) & 0xFF
        rewritten[5] = (out_ts >> 8) & 0xFF
        rewritten[6] = out_ts & 0xFF
        rewritten[7] = (out_ts >> 24) & 0xFF
        return bytes(rewritten)


def _is_keyframe(tag: bytes) -> bool:
    if tag[0] != TAG_VIDEO:
        return False
    size = (tag[1] << 16) | (tag[2] << 8) | tag[3]
    if size < 2:
        return False
    return (tag[11] >> 4) == 1 and tag[12] == 1


class _Subscriber:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        self.closed = False


class _DeviceStream:
    def __init__(self, hass, params_provider, device_id, keep_audio=False):
        self.hass = hass
        self._params_provider = params_provider
        self.device_id = device_id
        self.keep_audio = keep_audio
        self.subscribers: set[_Subscriber] = set()
        self.history = bytearray()
        self._rewriter = FlvRewriter(keep_audio=keep_audio)
        self._task: asyncio.Task | None = None
        self._client = None  # active VrsLiveClient while upstream runs

    def subscribe(self) -> _Subscriber:
        sub = _Subscriber()
        sub.queue.put_nowait(flv_header(self.keep_audio) + bytes(self.history))
        self.subscribers.add(sub)
        self._ensure_task()
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        self.subscribers.discard(sub)

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_background_task(
                self._run(), name=f"ease_life_flv_{self.device_id}"
            )

    def _remember(self, data: bytes) -> None:
        keyframe = None
        index = 0
        length = len(data)
        while index + 15 <= length:
            size = (data[index + 1] << 16) | (data[index + 2] << 8) | data[index + 3]
            total = _tag_total(data[index], size)
            if index + total > length:
                break
            if _is_keyframe(data[index:index + total]):
                keyframe = index
            index += total
        if keyframe is not None:
            self.history = bytearray(data[keyframe:])
        else:
            self.history += data
        if len(self.history) > HISTORY_LIMIT:
            self._trim_history()

    def _trim_history(self) -> None:
        data = bytes(self.history)
        index = 0
        keyframe = None
        length = len(data)
        while index + 15 <= length:
            size = (data[index + 1] << 16) | (data[index + 2] << 8) | data[index + 3]
            total = _tag_total(data[index], size)
            if index + total > length:
                break
            if _is_keyframe(data[index:index + total]):
                keyframe = index
            index += total
        self.history = bytearray(data[keyframe:]) if keyframe is not None else bytearray()

    def _broadcast(self, chunk: bytes) -> None:
        data = self._rewriter.feed(chunk)
        if not data:
            return
        self._remember(data)
        for sub in list(self.subscribers):
            if sub.closed:
                continue
            try:
                sub.queue.put_nowait(data)
            except asyncio.QueueFull:
                sub.closed = True
                try:
                    sub.queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def _run(self) -> None:
        while self.subscribers:
            try:
                params = await self._params_provider(self.device_id)
                if not params or not params.get("vrs_host"):
                    _LOGGER.warning("ease_life: no live params for %s", self.device_id)
                    await asyncio.sleep(10)
                    continue
                await self._run_session(params)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                self._rewriter.reset()
                _LOGGER.warning(
                    "ease_life: live stream error for %s: %s", self.device_id, err
                )
            if self.subscribers:
                await asyncio.sleep(1)
        _LOGGER.debug("ease_life: live stream stopped for %s", self.device_id)
        self._task = None

    def _new_client(self, params) -> VrsLiveClient:
        return VrsLiveClient(
            vrs_host=params["vrs_host"],
            token=params["token"],
            device_id=self.device_id,
            product_key=params.get("product_key", "45c8bd2a-e70"),
            relay_server=params.get("relay_server", ""),
            has_audio=self.keep_audio,
        )

    async def _pump(self, client, gen_id, queue, stop) -> None:
        """Forward one upstream session's FLV chunks to the generation queue."""
        try:
            async for chunk in client.stream(should_stop=stop):
                queue.put_nowait((gen_id, chunk))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("ease_life: upstream pump %s ended: %s", gen_id, err)
        finally:
            try:
                queue.put_nowait((gen_id, None))
            except asyncio.QueueFull:
                pass

    async def _run_session(self, params) -> None:
        """Run one generation: active session with standby handover.

        A standby session is pre-connected before the server TTL kills the
        active one and takes over (keyframe-aligned) with no output gap.
        A silence watchdog converts quiet-but-open sessions into reconnects.
        """
        queue: asyncio.Queue = asyncio.Queue()
        gens: dict[int, list] = {}
        gen_seq = 0
        active = None
        t_active_start = 0.0
        last_data = 0.0
        standby = None
        standby_buf = bytearray()
        standby_since = 0.0
        standby_cooldown_until = 0.0

        def start_pump():
            nonlocal gen_seq
            gen_seq += 1
            client = self._new_client(params)
            task = asyncio.create_task(
                self._pump(client, gen_seq, queue,
                           lambda: not self.subscribers),
                name=f"ease_life_pump_{self.device_id}_{gen_seq}")
            gens[gen_seq] = [client, task]
            return gen_seq

        async def stop_pump(gen_id):
            entry = gens.pop(gen_id, None)
            if entry is None:
                return
            _, task = entry
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        async def shutdown():
            for gen_id in list(gens):
                await stop_pump(gen_id)
            self._client = None
            self._rewriter.reset()

        async def switch_to(gen_id, buffered: bytes, reason: str):
            nonlocal active, t_active_start, standby, standby_buf
            if active is not None and active != gen_id:
                await stop_pump(active)
            active = gen_id
            entry = gens.get(gen_id)
            self._client = entry[0] if entry else None
            t_active_start = time.monotonic()
            standby = None
            standby_buf = bytearray()
            self._rewriter.reset()
            _LOGGER.info("ease_life: live handover for %s (%s)",
                         self.device_id, reason)
            if buffered:
                self._broadcast(buffered)

        active = start_pump()
        self._client = gens[active][0]
        t_active_start = time.monotonic()
        last_data = t_active_start
        try:
            while self.subscribers:
                now = time.monotonic()
                if (standby is None and active is not None
                        and now - t_active_start >= PRECONNECT_AGE
                        and now >= standby_cooldown_until):
                    standby = start_pump()
                    standby_buf = bytearray()
                    standby_since = now
                    _LOGGER.debug("ease_life: preconnect standby for %s",
                                  self.device_id)
                try:
                    gen_id, chunk = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    gen_id, chunk = None, None
                now = time.monotonic()
                if gen_id is None:
                    if (active is not None and self.subscribers
                            and now - last_data >= WATCHDOG_SILENCE):
                        _LOGGER.warning(
                            "ease_life: upstream silent %.0fs, forcing reconnect for %s",
                            now - last_data, self.device_id)
                        await stop_pump(active)
                        active = None
                        self._client = None
                        if standby is not None and standby_buf:
                            await switch_to(
                                standby, bytes(standby_buf), "watchdog-fallback")
                        else:
                            self._rewriter.reset()
                            return
                    continue
                if chunk is None:
                    if gen_id == active:
                        if standby is not None and standby_buf:
                            await switch_to(
                                standby, bytes(standby_buf), "active-closed")
                        else:
                            self._rewriter.reset()
                            return
                    elif gen_id == standby:
                        standby = None
                        standby_buf = bytearray()
                        standby_cooldown_until = now + STANDBY_RETRY_COOLDOWN
                    continue
                if gen_id == active:
                    last_data = now
                    self._broadcast(chunk)
                elif gen_id == standby:
                    standby_buf += chunk
                    if len(standby_buf) > STANDBY_BUFFER_LIMIT:
                        await stop_pump(standby)
                        standby = None
                        standby_buf = bytearray()
                        standby_cooldown_until = now + STANDBY_RETRY_COOLDOWN
                    elif _has_video_keyframe(standby_buf):
                        await switch_to(
                            standby, bytes(standby_buf),
                            f"preconnect-ready in {now - standby_since:.1f}s")
                # else: stale generation after a switch; ignore
        finally:
            await shutdown()


class FlvProxy:
    def __init__(self, hass, params_provider, host="127.0.0.1", port=8765,
                 keep_audio=False, proxy_token=""):
        self.hass = hass
        self._params_provider = params_provider
        self.host = host
        self.port = port
        self.keep_audio = keep_audio
        self.proxy_token = proxy_token or ""
        self._streams: dict[str, _DeviceStream] = {}
        self._runner: web.AppRunner | None = None
        if self.proxy_token:
            _LOGGER.debug("ease_life: FLV proxy token auth enabled")
        elif host not in ("127.0.0.1", "localhost", "::1"):
            _LOGGER.warning(
                "ease_life: FLV proxy bound to %s WITHOUT an access token; "
                "anyone on the network can watch. Set a proxy token in options.",
                host)

    def url_for(self, device_id: str) -> str:
        url = f"http://{self.host}:{self.port}/live/{device_id}.flv"
        if self.proxy_token:
            url += f"?token={self.proxy_token}"
        return url

    def _stream(self, device_id: str) -> _DeviceStream:
        stream = self._streams.get(device_id)
        if stream is None:
            stream = _DeviceStream(self.hass, self._params_provider, device_id,
                                   keep_audio=self.keep_audio)
            self._streams[device_id] = stream
        return stream

    def talk_sender(self, device_id: str):
        """Return an async talk-frame sender on the live upstream, if any.

        The callable re-resolves the current client on every call, so it
        survives the ~82 s upstream rotations.  Returns None when no upstream
        session is active (caller should open a dedicated talk session).
        """
        stream = self._streams.get(device_id)
        if stream is None:
            return None

        async def _send(payload: bytes) -> None:
            client = stream._client
            if client is None:
                raise TalkSendError("live session not active")
            await client.async_send_talk(payload)

        if stream._client is None:
            return None
        return _send

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        device_id = request.match_info["device"]
        if self.proxy_token and not hmac.compare_digest(
                request.query.get("token", ""), self.proxy_token):
            return web.Response(status=401, text="unauthorized\n")
        stream = self._stream(device_id)
        sub = stream.subscribe()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "video/x-flv", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)
        try:
            while True:
                chunk = await sub.queue.get()
                if chunk is None:
                    break
                await response.write(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  (client disconnect / write on closing transport)
            pass
        finally:
            stream.unsubscribe(sub)
        return response

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/live/{device}.flv", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        _LOGGER.info("ease_life: FLV proxy listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        for stream in self._streams.values():
            if stream._task and not stream._task.done():
                stream._task.cancel()
        self._streams.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
