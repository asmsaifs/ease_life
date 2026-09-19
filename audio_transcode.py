"""G.711 A-law downlink -> AAC FLV tags for HA's HLS output.

HA's stream worker only muxes ``aac``/``mp3`` into HLS, so the camera's A-law
audio must be transcoded.  This module accumulates 8 kHz mono A-law, encodes
1024-sample (128 ms) AAC-LC frames via PyAV (ADTS container path, headers
stripped), and returns raw AUs ready to be wrapped as FLV ``0xAF`` tags.

Frame timestamps are assigned from an audio clock anchored at the first input
timestamp of the session (``base + k*128``), which tracks the video clock
because both advance in realtime (verified: A/V offset stable <100 ms over a
full VRS session, see evidence/E-011).
"""

from __future__ import annotations

import io
import logging
import struct

_LOGGER = logging.getLogger(__name__)

FRAME_SAMPLES = 1024
FRAME_MS = FRAME_SAMPLES * 1000 // 8000  # 128


class TranscodeError(Exception):
    """Audio transcode failed."""


class AlawToAac:
    """Stateful A-law -> AAC framer (one per FLV rewriter)."""

    def __init__(self) -> None:
        import av  # noqa: PLC0415 (lazy: only needed when audio enabled)

        self._buf = io.BytesIO()
        self._out = av.open(self._buf, mode="w", format="adts")
        self._stream = self._out.add_stream("aac", rate=8000, layout="mono")
        self._read_off = 0
        self._pcm = bytearray()
        self._asc: bytes | None = None
        self._base_ts: int | None = None
        self._frames_out = 0

    def reset_session(self) -> None:
        """Drop buffered sub-frame audio; re-anchor the clock next feed."""
        self._pcm.clear()
        self._base_ts = None
        self._frames_out = 0

    @property
    def asc(self) -> bytes | None:
        return self._asc

    @property
    def base_ts(self) -> int | None:
        """Input-clock anchor of frame 0 (None before first input)."""
        return self._base_ts

    @property
    def frames_out(self) -> int:
        return self._frames_out

    def asc_timestamp(self) -> int | None:
        """Timestamp for a sequence header: strictly between the last emitted
        frame and the next one, so HA's validator never drops it and no audio
        frame is ever sacrificed for it."""
        if self._base_ts is None:
            return None
        assert self._base_ts is not None
        return self._base_ts + self._frames_out * FRAME_MS + FRAME_MS // 2

    def feed(self, alaw: bytes, input_ts: int) -> list[tuple[int, bytes]]:
        """Feed A-law bytes with their (remapped) FLV timestamp.

        Returns a list of (out_ts, raw AAC access unit) ready for FLV tags.
        Frame 0 is assigned base+128ms, reserving [base, base+128ms) for the
        AAC sequence header (see asc_timestamp), so init and media never
        share a timestamp and HA's validator drops nothing.
        """
        from . import g711

        if self._base_ts is None:
            self._base_ts = input_ts
        self._pcm += g711.alaw_to_pcm16(alaw)
        aus: list[tuple[int, bytes]] = []
        while len(self._pcm) >= FRAME_SAMPLES * 2:
            raw = bytes(self._pcm[: FRAME_SAMPLES * 2])
            del self._pcm[: FRAME_SAMPLES * 2]
            au = self._encode_frame(raw)
            if au is not None:
                self._frames_out += 1
                out_ts = self._base_ts + self._frames_out * FRAME_MS
                aus.append((out_ts, au))
        return aus

    def _encode_frame(self, pcm_s16: bytes) -> bytes | None:
        import av  # noqa: PLC0415

        vals = struct.unpack("<%dh" % FRAME_SAMPLES, pcm_s16)
        fbytes = struct.pack("<%df" % FRAME_SAMPLES,
                             *(v / 32768.0 for v in vals))
        frame = av.AudioFrame(format="fltp", layout="mono",
                              samples=FRAME_SAMPLES)
        frame.sample_rate = 8000
        frame.planes[0].update(fbytes)
        frame.pts = self._frames_out * FRAME_SAMPLES
        for packet in self._stream.encode(frame):
            self._out.mux(packet)
        if self._asc is None:
            extra = self._stream.codec_context.extradata
            if extra:
                self._asc = bytes(extra)
        return self._drain_one()

    def _drain_one(self) -> bytes | None:
        """Pull a single complete ADTS frame payload, if available."""
        data = self._buf.getvalue()
        i = self._read_off
        if len(data) - i < 7:
            return None
        if data[i] != 0xFF or (data[i + 1] & 0xF0) != 0xF0:
            raise TranscodeError("ADTS sync lost")
        if (data[i + 1] & 0x01) != 1:
            raise TranscodeError("unexpected ADTS CRC")
        flen = (((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3)
                | ((data[i + 5] & 0xE0) >> 5))
        if flen < 7 or len(data) - i < flen:
            return None
        au = data[i + 7:i + flen]
        i += flen
        self._read_off = i
        if i == len(data):
            self._buf.seek(0)
            self._buf.truncate()
            self._read_off = 0
        return bytes(au)


def make_audio_tag(payload: bytes, out_ts: int) -> bytes:
    """Wrap payload bytes as a complete FLV audio tag."""
    size = len(payload)
    head = bytes([8, (size >> 16) & 0xFF, (size >> 8) & 0xFF, size & 0xFF,
                  (out_ts >> 16) & 0xFF, (out_ts >> 8) & 0xFF,
                  out_ts & 0xFF, (out_ts >> 24) & 0xFF, 0, 0, 0])
    return head + payload + struct.pack(">I", 11 + size)
