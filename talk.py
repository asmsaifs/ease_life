"""Talk-back: send microphone/TTS audio to the camera speaker.

Mirrors the official web player (AudioTalkPlugin in webPlayer.min.js), which
rides the live VRS WebSocket session:

  BIN 0x07 + u32BE(len(json)) + json + raw G.711A bytes
    json = {"enctype":0,"channelCount":1,"sampleRate":8000,"timeSpan":20}
  stop: BIN 0x02 + u32BE(len(json)) + json (no audio bytes)

Uplink codec mirrors the downlink (audioEncodeType 0 = G711A, 8 kHz mono).
Audio is chunked in 20 ms frames (160 samples) paced in realtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse

import aiohttp

from . import g711
from .vrs_live import TalkSendError, build_req

_LOGGER = logging.getLogger(__name__)

CMD_TALK_DATA = 0x07
CMD_TALK_STOP = 0x02
CMD_SYNC = 0x03

SAMPLE_RATE = 8000
CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160


class TalkError(Exception):
    """Talk-back failed."""


def _talk_header() -> bytes:
    js = json.dumps({"enctype": 0, "channelCount": CHANNELS,
                     "sampleRate": SAMPLE_RATE,
                     "timeSpan": FRAME_MS}).encode()
    return len(js).to_bytes(4, "big") + js


def talk_data_frame(alaw_chunk: bytes) -> bytes:
    return bytes([CMD_TALK_DATA]) + _talk_header() + alaw_chunk


def talk_stop_frame() -> bytes:
    return bytes([CMD_TALK_STOP]) + _talk_header()


async def async_stream_pcm16(send_fn, pcm: bytes) -> float:
    """Chunk PCM, A-law encode and pace in realtime via send_fn(payload).

    send_fn is any async callable taking one binary frame (dedicated talk
    session or the proxy's live upstream).  Returns seconds streamed.
    Raises TalkError/TalkSendError when the session is gone.
    """
    frame_bytes = FRAME_SAMPLES * 2
    # Pad tail with silence so every chunk is a full 20 ms frame.
    if len(pcm) % frame_bytes:
        pcm += b"\x00" * (frame_bytes - len(pcm) % frame_bytes)
    nframes = len(pcm) // frame_bytes
    start = time.monotonic()
    try:
        for i in range(nframes):
            chunk = pcm[i * frame_bytes:(i + 1) * frame_bytes]
            await send_fn(talk_data_frame(g711.pcm16_to_alaw(chunk)))
            # Pace in realtime so the camera plays continuously.
            due = start + (i + 1) * FRAME_MS / 1000.0
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
    except (TalkSendError, TalkError):
        raise
    except Exception as err:  # noqa: BLE001 (e.g. closing transport)
        raise TalkError(f"talk send failed: {err}") from err
    return nframes * FRAME_MS / 1000.0


class TalkSession:
    """One talk-back utterance over a dedicated live WebSocket session."""

    def __init__(self, vrs_host, token, device_id, product_key, has_audio=False):
        self._vrs_host = vrs_host
        self._token = token
        self._device_id = device_id
        self._product_key = product_key
        self._has_audio = has_audio
        self._session = None
        self._ws = None

    async def open(self) -> None:
        url = f"wss://{self._vrs_host}/h5player/live"
        headers = {"Origin": "https://www.ehomeease.com",
                   "User-Agent": "Mozilla/5.0"}
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        self._session = aiohttp.ClientSession(timeout=timeout)
        try:
            self._ws = await self._session.ws_connect(url, headers=headers,
                                                      heartbeat=20)
            await self._ws.send_str(
                build_req(self._token, self._device_id, self._product_key,
                          has_audio=self._has_audio)
            )
            payload = urllib.parse.quote(
                json.dumps({"time": 0, "cmd": 3}))
            await self._ws.send_bytes(
                bytes([CMD_SYNC]) + payload.encode())
        except Exception:
            await self.close()
            raise

    async def send_pcm16(self, pcm: bytes) -> float:
        """Stream s16le mono 8 kHz PCM to the speaker; returns seconds sent."""
        if not self._ws:
            raise TalkError("talk session not open")

        async def _send(payload: bytes) -> None:
            assert self._ws is not None
            await self._ws.send_bytes(payload)

        return await async_stream_pcm16(_send, pcm)

    async def stop(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.send_bytes(talk_stop_frame())
            except OSError:
                pass

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except OSError:
                pass
            self._ws = None
        if self._session is not None:
            try:
                await self._session.close()
            except OSError:
                pass
            self._session = None


async def async_speak_pcm16(hass, live_params, device_id: str, pcm: bytes) -> float:
    """Open a talk session, play s16le/8kHz/mono PCM, stop, close."""
    session = TalkSession(live_params["vrs_host"], live_params["token"],
                          device_id,
                          live_params.get("product_key", ""))
    try:
        await session.open()
        seconds = await session.send_pcm16(pcm)
    finally:
        try:
            await session.stop()
        finally:
            await session.close()
    _LOGGER.debug("ease_life: spoke %.1fs of audio", seconds)
    return seconds
