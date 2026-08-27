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
import select
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
SNAPSHOT = SAMPLES.with_name('schedule-snapshot.json')
DEMO_REFRESH_S = 20 * 60

# Only these bytes ever vary; the rest is constant padding. 264 bits.
VARYING = list(range(1, 32)) + [212, 213]
# Schedule changes never move the command-flag bytes, so a collection run tops
# out here; the last 16 bits come from a waterNow and a stop.
SCHEDULE_RANK = 31 * 8


class Sniffer:
    """Pull programmes off the wire as they go past."""

    def __init__(self, iface):
        self.proc = subprocess.Popen(
            ['tcpdump', '-i', iface, '-l', '-A', '-s', '0', '-q', 'tcp port 80'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            errors='replace')
        self.buf = ''
        self.latest = None

    def poll(self, timeout=1.0):
        # readline() blocks forever when no packets arrive, which would defeat
        # every timeout above it. Wait on the pipe instead.
        if not select.select([self.proc.stdout], [], [], timeout)[0]:
            return None
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
            blob = self.poll(timeout=min(1.0, max(0.05, end - time.time())))
            if blob and blob != previous:
                return blob
        return None

    def stop(self):
        self.proc.terminate()


DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday',
        'Sunday']


def random_schedule(rng):
    """Independent events per weekday.

    Writing the same events to all seven days moves only two duration bytes out
    of the fourteen in the programme; varying each day exercises the whole
    chain, so far fewer rounds are needed.
    """
    per_day = {}
    for day in DAYS:
        # Two per day fills the programme without overflowing it: simulation
        # gives 1:1 rank growth here, against 0.4 when 1-event days are mixed in
        # (too few bytes move) and no better with three.
        events = {(rng.randrange(0, 24 * 60) * 60_000, rng.randrange(1, 181))
                  for _ in range(2)}
        per_day[day] = sorted(events)
    return per_day


def collect(args):
    rng = random.Random(args.seed)
    original = api.get_schedule(args.hub, args.schedule)
    if not original:
        sys.exit(f'no schedule {args.schedule!r} - check --hub')
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(original, indent=1))
    print(f'snapshotted {args.schedule!r} to {SNAPSHOT}; restored on exit')

    restored = []

    def restore():
        if restored:
            return
        restored.append(True)
        # The API may be why we are exiting, so try harder than usual here.
        for attempt in range(6):
            code, _ = api.put_schedule(args.hub, args.schedule, original)
            if code < 300 and code != api.NETWORK_ERROR:
                print(f'\nrestored original schedule -> {code}')
                return
            time.sleep(5 * (attempt + 1))
        print('\nCOULD NOT RESTORE the original schedule - it is saved to '
              f'{SNAPSHOT}; re-apply it when the API is reachable', file=sys.stderr)

    atexit.register(restore)
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    # Demo mode lapses after about an hour, and without it a round costs ~16
    # minutes instead of ~10 seconds. Re-assert it well inside that window.
    demo_expiry = 0

    def keep_demo():
        nonlocal demo_expiry
        if time.time() < demo_expiry:
            return
        code, _ = api.call(args.hub, '/controllers/actions/setMode', 'POST',
                           {'controllerIDs': [args.controller], 'mode': 'demo'})
        if code < 300 and code != api.NETWORK_ERROR:
            demo_expiry = time.time() + DEMO_REFRESH_S
        else:
            print(f'    demo mode refresh failed ({code}); rounds will be slow')

    keep_demo()

    sniffer = Sniffer(args.iface)
    SAMPLES.parent.mkdir(parents=True, exist_ok=True)
    seen = load_samples()
    print(f'{len(seen)} samples already on file; rank {rank_of(seen)}/{len(VARYING) * 8}')

    previous = None
    written = 0
    started = time.time()
    start_rank = rank_of(load_samples())
    try:
        with open(SAMPLES, 'a') as out:
            for n in range(args.rounds):
                keep_demo()
                events = random_schedule(rng)
                sched = api.set_days(json.loads(json.dumps(original)), events)
                code, body = api.put_schedule(args.hub, args.schedule, sched)
                if code == api.NETWORK_ERROR:
                    print(f'[{n}] {body}; waiting 60s')
                    time.sleep(60)
                    continue
                if code >= 300:
                    detail = body if isinstance(body, str) else json.dumps(body or {})
                    print(f'[{n}] PUT failed {code}: {detail[:600]}')
                    print(f'     events were {events}')
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
                    rate = (time.time() - started) / max(written, 1)
                    gained = max(r - start_rank, 1)
                    per_sample = gained / written
                    left = (SCHEDULE_RANK - r) / per_sample * rate / 60
                    print(f'[{n}] {len(seen)} samples, rank {r}/{SCHEDULE_RANK}, '
                          f'{rate:.0f}s each, {per_sample:.2f} rank/sample, '
                          f'~{left:.0f} min left')
                    if r >= SCHEDULE_RANK:
                        print('schedule bits complete - now measure the command '
                              'flags with a waterNow and a stop')
                        break
    finally:
        sniffer.stop()
        restore()
    return 0


SHAPES = [
    ('one absolute',        [(6 * 3600_000, 30)]),
    ('two absolute',        [(6 * 3600_000, 30), (21 * 3600_000, 45)]),
    ('three absolute',      [(6 * 3600_000, 30), (13 * 3600_000, 20),
                             (21 * 3600_000, 45)]),
    ('sunrise only',        [(-2000, 45)]),
    ('sunrise + sunset',    [(-2000, 45), (-1000, 60)]),
    ('solar + absolute',    [(-2000, 45), (13 * 3600_000, 20)]),
    ('absolute + solar',    [(13 * 3600_000, 20), (-1000, 60)]),
    ('unsorted absolute',   [(21 * 3600_000, 45), (6 * 3600_000, 30)]),
    ('odd minute duration', [(6 * 3600_000, 17)]),
    ('non-5-minute start',  [(6 * 3600_000 + 7 * 60_000, 30)]),
    ('overlapping',         [(6 * 3600_000, 120), (7 * 3600_000, 30)]),
    ('long duration',       [(6 * 3600_000, 180)]),
]


def probe_shapes(args):
    """Find which schedule shapes the API accepts, before a long run."""
    original = api.get_schedule(args.hub, args.schedule)
    if not original:
        sys.exit(f'no schedule {args.schedule!r}')
    try:
        for name, events in SHAPES:
            sched = api.set_events(json.loads(json.dumps(original)), events)
            code, body = api.put_schedule(args.hub, args.schedule, sched)
            msg = ''
            if code >= 300:
                detail = body if isinstance(body, str) else json.dumps(body or {})
                msg = detail[detail.find('errorMessage'):][:160]
            print(f'  {name:<22} {code} {msg}')
            time.sleep(1)
    finally:
        api.put_schedule(args.hub, args.schedule, original)
        print('\noriginal schedule restored')
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
    p.add_argument('--probe-shapes', action='store_true',
                   help='find which schedule shapes the API accepts')
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
    if args.probe_shapes:
        return probe_shapes(args)
    if args.schedule is None:
        args.schedule = f'default_{args.hub}'
    return collect(args)


if __name__ == '__main__':
    sys.exit(main())
