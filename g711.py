"""G.711 A-law encode/decode (pure Python; no audioop/numpy in HA Core).

Encoder follows the canonical Sun algorithm and is verified against
audioop.lin2alaw on 2000 random samples (see tools/closeli/g711.py).
A-law silence is 0xD5.
"""

_SEG_END = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)


def lin2alaw(pcm: int) -> int:
    """Encode one signed 16-bit PCM sample to 8-bit A-law."""
    if pcm > 32767:
        pcm = 32767
    elif pcm < -32768:
        pcm = -32768
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    seg = 0
    while seg < 8 and pcm > _SEG_END[seg]:
        seg += 1
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    if seg < 2:
        aval |= (pcm >> 4) & 0x0F
    else:
        aval |= (pcm >> (seg + 3)) & 0x0F
    return aval ^ mask


def pcm16_to_alaw(samples: bytes) -> bytes:
    """s16le mono bytes -> A-law bytes."""
    import struct

    n = len(samples) // 2
    vals = struct.unpack("<%dh" % n, samples[: n * 2])
    return bytes(lin2alaw(v) for v in vals)


def alaw2lin(a: int) -> int:
    """Decode one 8-bit A-law sample to signed 16-bit PCM."""
    a ^= 0x55
    sign = a & 0x80
    seg = (a & 0x70) >> 4
    q = a & 0x0F
    if seg == 0:
        pcm = (q << 4) + 8
    else:
        pcm = ((q << 4) + 0x108) << (seg - 1)
    return pcm if sign else -pcm


_ALAW_TO_S16 = None


def alaw_to_pcm16(data: bytes) -> bytes:
    """A-law bytes -> s16le mono bytes (table driven)."""
    global _ALAW_TO_S16
    if _ALAW_TO_S16 is None:
        import struct

        _ALAW_TO_S16 = [struct.pack("<h", alaw2lin(b)) for b in range(256)]
    table = _ALAW_TO_S16
    return b"".join(table[b] for b in data)
