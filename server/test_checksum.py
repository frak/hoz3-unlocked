"""Verify the solved schedule checksum against every captured sample.

Run with the venv interpreter: uv run python test_checksum.py

The samples are (programme, real-checksum) pairs the true service produced;
bytes 216-217 are the checksum the hub validates. The solved algorithm must
reproduce every one, including the documented worked example (9b f6).
"""
import base64
import json
import sys
from pathlib import Path

from hozelock import codec, heartbeat_checksum
from hozelock.checksums import CHECKSUMS

DATA = Path(__file__).resolve().parents[1] / 'data'
SAMPLES = DATA / 'checksum-samples.jsonl'


def samples():
    for line in open(SAMPLES):
        try:
            blob = base64.b64decode(json.loads(line)['blob'])
        except Exception:
            continue
        if len(blob) == codec.SCHEDULE_LEN:
            yield blob


def main():
    failures = []
    n = 0
    for blob in samples():
        n += 1
        want = (blob[216] << 8) | blob[217]
        got = codec.schedule_checksum(blob)
        if got != want:
            failures.append(f'{blob[1:20].hex()}...: got {got:04x} want {want:04x}')

    # The worked example from docs/checksum.md.
    worked = next(samples())
    assert worked[1:9].hex() == '542dac3c742dac3c', 'first sample is the worked example'
    if codec.schedule_checksum(worked) != 0x9bf6:
        failures.append(f'worked example: got {codec.schedule_checksum(worked):04x}, '
                        f'want 9bf6')

    # Recompute-and-serve round trip: a freshly built programme keeps its checksum.
    lead, events = codec.decode_schedule(worked)
    rebuilt = codec.encode_schedule(lead, events, flags=worked[211:216])
    ck = codec.schedule_checksum(rebuilt)
    served = rebuilt[:216] + bytes([ck >> 8, ck & 0xff])
    if served != worked:
        diff = [i for i in range(len(worked)) if served[i] != worked[i]]
        failures.append(f'round-trip differs at {diff[:8]}')

    # Heartbeat response checksum: every captured (flag, generation) pair, plus
    # the live generation sweep/validation covering high generations whose byte8
    # table bits are reached only by extrapolation.
    hb_n = 0
    for (flag, gen), ck in CHECKSUMS.items():
        hb_n += 1
        if heartbeat_checksum.checksum(flag, gen) != ck:
            failures.append(f'heartbeat table flag={flag:02x} gen={gen:04x}: '
                            f'got {heartbeat_checksum.checksum(flag, gen):04x} want {ck:04x}')
    for path, key in ((DATA / 'hb_samples_live.jsonl', None),
                      (DATA / 'hb_validate.json', None)):
        if not path.exists():
            continue
        rows = ([json.loads(l) for l in open(path)] if path.suffix == '.jsonl'
                else json.load(open(path)))
        for r in rows:
            hb_n += 1
            want = r['ck'] if 'ck' in r else int(r['real'], 16)
            if heartbeat_checksum.checksum(r['flag'], r['gen']) != want:
                failures.append(f'heartbeat {path.name} gen={r["gen"]:04x}: wrong')

    print(f'checked {n} captured programmes and {hb_n} heartbeat pairs')
    if failures:
        print(f'{len(failures)} FAILURES:')
        for f in failures[:10]:
            print('  ' + f)
        return 1
    print(f'all {n} checksums reproduced, worked example 9b f6 OK, round-trip OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
