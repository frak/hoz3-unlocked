"""Solved schedule-checksum algorithm.

The 218-byte programme's trailing checksum (bytes 216-217) is a GF(2)-affine
byte hash, recovered by measuring its linear structure and finding the byte-to-
byte recurrence that measurement alone missed (see docs/checksum-problem.md).

    ck(blob) = BASE ^ sum_{p=1..213} X8^(p-1) . T(blob[p])

where T is a fixed linear 8->16 byte table (columns T1) and X8 is the single
16x16 GF(2) operator that advances one byte position. Because the map is
propagated by X8 rather than measured per position, it computes the checksum for
any programme shape and length -- no captured span, no clamped gaps.

Constants were solved from 560 captured (programme, checksum) pairs; the model
reproduces all of them and the documented worked example (9b f6) with zero
error. Regenerate/verify with capture/collect-checksums.py --solve.
"""

# 16x16 byte-advance operator, one row per output bit: output bit j is the
# parity of (X8[j] & state). Solved, not a standard CRC polynomial.
X8 = [
    0x0f7c, 0x8d54, 0xab6e, 0x2472, 0x1012, 0xc356, 0x8f5c, 0x2020,
    0x8f44, 0xaf66, 0x040c, 0xef64, 0x2032, 0xafe4, 0x6100, 0xef44,
]

# Byte table columns: T1[b] is the checksum image of bit b of a byte at the
# first hashed position. The full byte map is the XOR of the set bits' columns.
T1 = [0x6e3c, 0xb751, 0x9166, 0x38d4, 0x17c8, 0xc487, 0x8d08, 0xd535]

# Constant term: absorbs the checksum's init/final constants and the fixed
# padding/terminator bytes, which never vary in a well-formed programme.
BASE = 0xc3dc

# The hash runs from byte 1 (byte 0 is the constant lead 0x00) through the last
# byte that can vary -- the command flags at 212-213. Bytes past that are always
# zero and contribute nothing.
FIRST_POS = 1
LAST_POS = 213


def _mul_x8(v):
    out = 0
    for j in range(16):
        if bin(X8[j] & v).count('1') & 1:
            out |= 1 << j
    return out


# Per-position byte columns, X8-propagated once at import: _cols[p][b].
_cols = {}
_cur = list(T1)
for _p in range(FIRST_POS, LAST_POS + 1):
    _cols[_p] = _cur
    _cur = [_mul_x8(v) for v in _cur]


def checksum(blob):
    """Return the 16-bit schedule checksum for a 218-byte programme."""
    acc = BASE
    for p in range(FIRST_POS, LAST_POS + 1):
        v = blob[p]
        cols = _cols[p]
        b = 0
        while v:
            if v & 1:
                acc ^= cols[b]
            v >>= 1
            b += 1
    return acc
