"""Round-trip the codec against every blob Hozelock actually sent.

Corpus is data/captures/hb-raw.tsv (see capture-setup.md). Checksums are carried
through rather than recomputed -- test_checksum.py covers the algorithm itself.
"""
import re
import sys
from pathlib import Path

from hozelock import codec

DATA = Path(__file__).resolve().parents[1] / 'data'

CK = slice(216, 218)


def corpus(path=DATA / 'captures' / 'hb-raw.tsv'):
    seen = {}
    for line in open(path):
        f = line.rstrip('\n').split('\t')
        f += [''] * (3 - len(f))
        if not f[0] or not f[2]:
            continue
        try:
            body = bytes.fromhex(f[2].replace(':', '')).decode('latin1')
        except ValueError:
            continue
        blob = codec.unwrap(body)
        if blob:
            seen.setdefault(bytes(blob), float(f[0]))
    return seen


def main():
    blobs = corpus()
    schedules = {b for b in blobs if len(b) == codec.SCHEDULE_LEN}
    beats = {b for b in blobs if len(b) == codec.HEARTBEAT_LEN}
    print(f'corpus: {len(schedules)} schedule blobs, {len(beats)} heartbeat responses')

    failures = []

    for blob in sorted(schedules):
        lead, events = codec.decode_schedule(blob)
        if not events:
            continue
        rebuilt = codec.encode_schedule(lead, events, flags=blob[211:216],
                                        checksum=blob[CK])
        if rebuilt != blob:
            diff = [i for i in range(len(blob)) if blob[i] != rebuilt[i]]
            failures.append(f'schedule lead={lead}: differs at {diff[:8]}')

    for blob in sorted(beats):
        f = codec.decode_heartbeat_response(blob)
        rebuilt = codec.encode_heartbeat_response(
            f['clock_minutes'], f['clock_seconds'], f['generation'],
            flag=f['flag'], checksum=f['checksum'])
        if rebuilt != blob:
            diff = [i for i in range(len(blob)) if blob[i] != rebuilt[i]]
            failures.append(f'heartbeat gen={f["generation"]:04x}: differs at {diff}')

    # The invariant the server will rely on before serving anything
    for blob in sorted(schedules):
        lead, events = codec.decode_schedule(blob)
        total = lead + sum(g for _, g in events)
        if events and total != codec.CYCLE_UNITS:
            failures.append(f'schedule lead={lead}: totals {total}, not {codec.CYCLE_UNITS}')

    # The hub's decoder needs the padding; without it the programme is refused.
    for blob in sorted(schedules | beats):
        wrapped = codec.wrap(blob)
        if codec.unwrap(wrapped) != blob:
            failures.append(f'{len(blob)}-byte blob does not survive wrap/unwrap')
        n = len(codec.b64_encode(blob))
        if n % 4:
            failures.append(f'{len(blob)}-byte blob encodes to {n} chars, not a '
                            f'multiple of 4 - padding was stripped')

    # Offsets 7-8 as a bitfield. 03 10 was observed live on 11 Nov 2026 -- a
    # manual run started while the controller was resynchronising -- and the
    # earlier lookup table could only call it "unknown 0310".
    cases = {
        (0x00, 0x00): ('idle', False),
        (0x01, 0x10): ('manual', False),
        (0x01, 0x04): ('scheduled', False),
        (0x02, 0x00): ('idle', True),        # was the unexplained "state-02"
        (0x03, 0x10): ('manual', True),      # watering while resyncing
        (0x03, 0x04): ('scheduled', True),
        (0x08, 0x10): ('unknown 0810', False),   # unknown bits stay honest
    }
    for (b7, b8), want in cases.items():
        got = codec.decode_watering(b7, b8)
        if got != want:
            failures.append(f'watering {b7:02x} {b8:02x}: {got} != {want}')

    if failures:
        print(f'\n{len(failures)} FAILURES:')
        for f in failures[:10]:
            print('  ' + f)
        return 1
    print('all blobs round-trip exactly (checksums carried through, not computed)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
