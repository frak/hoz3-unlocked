"""Solved heartbeat-response checksum.

The 24-byte response's trailing checksum (bytes 22-23) is GF(2)-affine in the
demo flag (byte 5) and the 16-bit generation (bytes 8-9), and independent of the
clock -- see docs/checksum.md. It is the SAME bespoke byte field as the
schedule checksum: byte 9 (generation low) uses the byte table L9, and byte 8
(generation high) is one position earlier, so its table is X8^340 . L9, where X8
is the schedule's byte-advance operator (X8 has order 341 on the checksum span,
so X8^340 = X8^-1).

    ck(flag, gen) = BASE ^ sum byte9-bits.L9 ^ sum byte8-bits.L8 ^ (flag ? Lf : 0)

Solved from 75 captured pairs plus a live generation sweep, and confirmed live
against the real service for generations up to 0x103c (byte8 0x08..0x10), whose
byte8 table bits are reached only by the X8 extrapolation. This removes the old
lookup-table cap of 30 (normal) / 45 (demo) usable generations: any generation
now yields a valid checksum.
"""

# Generation low byte (byte 9): measured checksum image of each bit.
L9 = [0xdb16, 0x9a2f, 0xef14, 0x5f04, 0xdd31, 0x0221, 0x3db4, 0x043a]

# Generation high byte (byte 8): L8[b] == X8^340 . L9[b] (one byte position
# earlier in the same field). Baked in so this module needs no matrix math.
L8 = [0x03d0, 0xdb02, 0x174c, 0xb0f5, 0x878f, 0x8a1e, 0xdbe9, 0x1a44]

# Demo-flag (byte 5, 0x10) contribution, and the constant term ck(flag=0, gen=0).
LFLAG = 0xc6d9
BASE = 0x8ae8

FLAG_DEMO = 0x10


def checksum(flag, generation):
    """16-bit checksum for a heartbeat response with this flag and generation."""
    acc = BASE
    gen = generation & 0xffff
    for b in range(8):
        if (gen >> b) & 1:
            acc ^= L9[b]
        if (gen >> (8 + b)) & 1:
            acc ^= L8[b]
    if flag:
        acc ^= LFLAG
    return acc
