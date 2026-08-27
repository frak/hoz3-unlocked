#!/usr/bin/env python3
"""Collect (programme, checksum) pairs to solve the schedule checksum.

The hub validates the checksum on the 218-byte programme and we cannot compute
it (see docs/protocol.md). It is GF(2)-affine over the 33 bytes that ever vary,
so ~264 independent samples determine it completely.

Each round: write a new schedule via the cloud API, wait for the real service to
hand the hub a programme, and record it off the wire. Runs unattended.

Requires the hub on the REAL service with the capture bridge up (br0), and
tcpdump. Run on the Pi:

    sudo -E ./collect-checksums.py --iface br0 --rounds 400
    ./collect-checksums.py --solve                 # any machine, no hardware

The hub id comes from $HOZELOCK_HUB or capture/.hubid, both untracked -- the
cloud API has no authentication, so the id is a credential in all but name.

The live schedule is snapshotted at start and restored on exit, including on
Ctrl-C or a crash.
"""
import argparse
import atexit
import base64
import json
import pathlib
import random
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from hozelock import codec  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    'hozelock_api', pathlib.Path(__file__).with_name('hozelock-api.py'))
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

SENTINEL = re.compile(r'#!hb=([A-Za-z0-9_\-=]+)\.')
SAMPLES = pathlib.Path(__file__).resolve().parents[1] / 'data' / 'checksum-samples.jsonl'

# Only these bytes ever vary; the rest is constant padding. 264 bits.
VARYING = list(range(1, 32)) + [212, 213]


class Sniffer:
    """Pull programmes off the wire as they go past."""

    def __init__(self, iface):
        self.proc = subprocess.Popen(
            ['tcpdump', '-i', iface, '-l', '-A', '-s', '0', '-q', 'tcp port 80'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            errors='replace')
        self.buf = ''
        self.latest = None

    def poll(self):
        line = self.proc.stdout.readline()
        if not line:
            return None
        self.buf = (self.buf + line)[-8000:]
        for m in SENTINEL.finditer(self.buf):
            try:
                blob = codec.b64_decode(m.group(1))
            except Exception:
                continue
            if len(blob) == codec.SCHEDULE_LEN:
                self.buf = self.buf[m.end():]
                self.latest = bytes(blob)
                return self.latest
        return None

    def wait_for_new(self, previous, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            blob = self.poll()
            if blob and blob != previous:
                return blob
        return None

    def stop(self):
        self.proc.terminate()


def random_schedule(rng):
    """A schedule chosen to move as many programme bytes as possible."""
    events = []
    for _ in range(rng.choice([1, 2, 2, 3])):
        if rng.random() < 0.3:
            start = rng.choice([-2000, -1000])          # sunrise / sunset
        else:
            start = rng.randrange(0, 24 * 60, 5) * 60_000
        events.append((start, rng.choice(list(range(1, 31)) + [35, 40, 60, 90, 130])))
    return sorted(set(events))


def collect(args):
    rng = random.Random(args.seed)
    original = api.get_schedule(args.hub, args.schedule)
    if not original:
        sys.exit(f'no schedule {args.schedule!r} - check --hub')
    print(f'snapshotted {args.schedule!r}; it will be restored on exit')

    restored = []

    def restore():
        if restored:
            return
        restored.append(True)
        code, _ = api.put_schedule(args.hub, args.schedule, original)
        print(f'\nrestored original schedule -> {code}')

    atexit.register(restore)
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    api.call(args.hub, '/controllers/actions/setMode', 'POST',
             {'controllerIDs': [args.controller], 'mode': 'demo'})

    sniffer = Sniffer(args.iface)
    SAMPLES.parent.mkdir(parents=True, exist_ok=True)
    seen = load_samples()
    print(f'{len(seen)} samples already on file; rank {rank_of(seen)}/{len(VARYING) * 8}')

    previous = None
    written = 0
    try:
        with open(SAMPLES, 'a') as out:
            for n in range(args.rounds):
                events = random_schedule(rng)
                sched = api.set_events(json.loads(json.dumps(original)), events)
                code, _ = api.put_schedule(args.hub, args.schedule, sched)
                if code >= 300:
                    print(f'[{n}] PUT failed {code}, backing off')
                    time.sleep(10)
                    continue
                blob = sniffer.wait_for_new(previous, args.timeout)
                if blob is None:
                    print(f'[{n}] no programme seen in {args.timeout}s')
                    continue
                previous = blob
                out.write(json.dumps({
                    'at': datetime.now().isoformat(timespec='seconds'),
                    'events': events,
                    'blob': base64.b64encode(blob).decode(),
                }) + '\n')
                out.flush()
                written += 1
                if written % 10 == 0:
                    seen = load_samples()
                    r = rank_of(seen)
                    print(f'[{n}] {len(seen)} samples, rank {r}/{len(VARYING) * 8}')
                    if r >= len(VARYING) * 8:
                        print('full rank reached - the map is determined')
                        break
    finally:
        sniffer.stop()
        restore()
    return 0


def load_samples():
    if not SAMPLES.exists():
        return []
    out = []
    for line in open(SAMPLES):
        try:
            out.append(base64.b64decode(json.loads(line)['blob']))
        except Exception:
            continue
    return out


def bits_of(blob, reference):
    """Difference vector over the varying bytes, as an integer bitmask."""
    v = 0
    for i, pos in enumerate(VARYING):
        v |= (blob[pos] ^ reference[pos]) << (i * 8)
    return v


def rank_of(blobs):
    if len(blobs) < 2:
        return 0
    ref = blobs[0]
    basis = {}
    for blob in blobs[1:]:
        v = bits_of(blob, ref)
        while v:
            h = v.bit_length() - 1
            if h in basis:
                v ^= basis[h]
            else:
                basis[h] = v
                break
    return len(basis)


def import_corpus():
    """Programmes recorded before this tool existed are still valid samples."""
    raw = SAMPLES.parent / 'hb-raw.tsv'
    have = {bytes(b) for b in load_samples()}
    added = 0
    SAMPLES.parent.mkdir(parents=True, exist_ok=True)
    with open(SAMPLES, 'a') as out:
        for line in open(raw):
            f = line.rstrip('\n').split('\t')
            f += [''] * (3 - len(f))
            if not f[0] or not f[2]:
                continue
            try:
                body = bytes.fromhex(f[2].replace(':', '')).decode('latin1')
            except ValueError:
                continue
            blob = codec.unwrap(body)
            if blob and len(blob) == codec.SCHEDULE_LEN and bytes(blob) not in have:
                have.add(bytes(blob))
                out.write(json.dumps({
                    'at': datetime.fromtimestamp(float(f[0])).isoformat(timespec='seconds'),
                    'events': None,
                    'blob': base64.b64encode(bytes(blob)).decode(),
                }) + '\n')
                added += 1
    blobs = load_samples()
    print(f'imported {added}; {len(blobs)} samples, '
          f'rank {rank_of(blobs)}/{len(VARYING) * 8}')
    return 0


def solve(args):
    blobs = load_samples()
    n_bits = len(VARYING) * 8
    print(f'{len(blobs)} samples, {n_bits} unknowns, rank {rank_of(blobs)}')
    if len(blobs) < 2:
        return 1
    ref = blobs[0]
    basis = {}
    for blob in blobs[1:]:
        v = bits_of(blob, ref)
        img = ((blob[216] << 8) | blob[217]) ^ ((ref[216] << 8) | ref[217])
        while v:
            h = v.bit_length() - 1
            if h in basis:
                bv, bi = basis[h]
                v ^= bv
                img ^= bi
            else:
                basis[h] = (v, img)
                break
        else:
            if img:
                print('INCONSISTENT: a zero difference maps to a non-zero checksum '
                      'delta - the checksum depends on something outside VARYING')
                return 1
    print(f'consistent so far; {len(basis)} independent vectors')
    if len(basis) < n_bits:
        print(f'need {n_bits - len(basis)} more independent samples')
        return 2
    print('solved - the checksum can now be computed for any programme')
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--solve', action='store_true', help='analyse what is on file')
    p.add_argument('--import-corpus', action='store_true',
                   help='seed from programmes already captured in data/')
    p.add_argument('--hub', help='default: $HOZELOCK_HUB or capture/.hubid')
    p.add_argument('--controller', default='0')
    p.add_argument('--schedule', help='default: the hub default schedule')
    p.add_argument('--iface', default='br0')
    p.add_argument('--rounds', type=int, default=400)
    p.add_argument('--timeout', type=int, default=90)
    p.add_argument('--seed', type=int, default=1)
    args = p.parse_args()
    if args.import_corpus:
        return import_corpus()
    if args.solve:
        return solve(args)
    args.hub = api.resolve_hub(args.hub)
    if args.schedule is None:
        args.schedule = f'default_{args.hub}'
    return collect(args)


if __name__ == '__main__':
    sys.exit(main())
