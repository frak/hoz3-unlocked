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
from datetime import datetime, timedelta

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

# The payload runs to about byte 38 once a gap needs continuation bytes -- the
# original corpus only ever showed 31 because those programmes were all short.
# Modelling too few bytes makes distinct programmes look identical to the solver
# and the rank silently stalls, so allow headroom.
# Real programmes usually end between bytes 32 and 35, but around midsummer the
# sunrise-to-sunset gap exceeds 200 units, needs a continuation byte, and runs
# to 38. Model to 39 so those days are covered too.
PAYLOAD_END = 40

# Share of days given a gap long enough to need a continuation byte. Raising it
# lengthens the payload, which is how the tail bytes get exercised.
WIDE_SHARE = 0.35
# Minutes between the two events on a "wide" day. Midsummer sunrise-to-sunset in
# London is ~998, just over the 200-unit threshold, which is what makes those
# programmes longer than the rest of the year.
WIDE_SPAN = (1000, 1180)
VARYING = list(range(1, PAYLOAD_END)) + [212, 213]
# Schedule changes never move the command-flag bytes; those come from --flags.
SCHEDULE_RANK = (PAYLOAD_END - 1) * 8


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
        # Two events a day, spaced so both gaps stay under 200 units. A wider
        # spacing needs continuation bytes, which lengthens the payload past
        # anything a real schedule produces.
        t1 = rng.randrange(0, 24 * 60)
        # A wider spacing needs a continuation byte, which lengthens the payload.
        # Real programmes do this at some times of year, so a share of samples
        # must too or the tail bytes never move.
        wide = rng.random() < WIDE_SHARE
        span = (rng.randrange(*WIDE_SPAN) if wide
                else rng.randrange(450, 991))
        t2 = (t1 + span) % (24 * 60)
        per_day[day] = sorted([(min(t1, t2) * 60_000, rng.randrange(1, 181)),
                               (max(t1, t2) * 60_000, rng.randrange(1, 181))])
    return per_day


def collect(args):
    global WIDE_SHARE, WIDE_SPAN
    if args.long:
        WIDE_SHARE = 0.85
        print('long-payload mode: targeting the tail bytes')
    if args.span:
        WIDE_SPAN = (args.span[0], args.span[1] + 1)
        WIDE_SHARE = 1.0
        print(f'every day spanning {args.span[0]}-{args.span[1]} minutes')
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
    total = len(load_samples(all_lengths=True))
    print(f'{total} samples on file, {len(seen)} usable (payload within '
          f'{PAYLOAD_END} bytes); rank {rank_of(seen)}/{SCHEDULE_RANK}')

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
                    still = needed_missing(seen)
                    if not still:
                        print('every bit the server needs is now spanned')
                        break
                    if len(still) <= 24:
                        by_byte = sorted({p for p, _ in still})
                        print(f'      {len(still)} needed bits left, in bytes {by_byte}')
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


def collect_flags(args):
    """Capture the two command blobs.

    Schedule changes never move bytes 212-213, so a waterNow and a stop are the
    only way to see those bits. The server only ever sets 0x01 in one of them,
    so two vectors are enough -- the full 16 bits are not needed.
    """
    sniffer = Sniffer(args.iface)
    ids = {'controllerIDs': [args.controller]}
    try:
        with open(SAMPLES, 'a') as out:
            for name, path, payload in (
                    ('waterNow', 'waterNow', {**ids, 'duration': 60_000}),
                    ('stopWatering', 'stopWatering', ids)):
                previous = sniffer.latest
                code, _ = api.call(args.hub, f'/controllers/actions/{path}',
                                   'POST', payload)
                print(f'{name} -> {code}')
                blob = sniffer.wait_for_new(previous, args.timeout)
                if blob is None:
                    print(f'  no programme seen after {name}')
                    continue
                flags = blob[211:216].hex(" ")
                print(f'  captured, flags = {flags}')
                if not any(blob[211:216]):
                    print('  WARNING: flags are all zero - the command was not in '
                          'this programme; the hub may have fetched too early')
                out.write(json.dumps({
                    'at': datetime.now().isoformat(timespec='seconds'),
                    'events': name,
                    'blob': base64.b64encode(blob).decode(),
                }) + '\n')
                out.flush()
                time.sleep(5)
    finally:
        sniffer.stop()
    blobs = load_samples()
    print(f'{len(blobs)} samples, rank {rank_of(blobs)}')
    return 0


def load_samples(all_lengths=False):
    if not SAMPLES.exists():
        return []
    out = []
    for line in open(SAMPLES):
        try:
            blob = base64.b64decode(json.loads(line)['blob'])
        except Exception:
            continue
        # A longer payload carries bits outside the model; including it makes
        # distinct programmes look identical to the solver and stalls the rank.
        if all_lengths or all(b == 0xff for b in blob[PAYLOAD_END:210]):
            out.append(blob)
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
    raw = SAMPLES.parent / 'captures' / 'hb-raw.tsv'
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


def build_basis(blobs):
    """-> (reference, {leading_bit: (vector, checksum_delta)}, inconsistencies)"""
    ref = blobs[0]
    basis = {}
    bad = 0
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
                bad += 1
    return ref, basis, bad


def checksum_with(ref, basis, blob):
    """Compute a programme's checksum, or None if it is outside the span."""
    v = bits_of(blob, ref)
    img = (ref[216] << 8) | ref[217]
    while v:
        h = v.bit_length() - 1
        if h not in basis:
            return None
        bv, bi = basis[h]
        v ^= bv
        img ^= bi
    return img


# Bytes 212-213 only ever hold 0x00 or 0x01, so bits 1-7 can never be measured
# and are never needed: the server sets no other value.
UNREACHABLE = {(212, b) for b in range(1, 8)} | {(213, b) for b in range(1, 8)}


def missing_bits(blobs):
    """Which (byte, bit) positions the samples still do not span."""
    _, basis, _ = build_basis(blobs)
    reduced = {h: v for h, (v, _) in basis.items()}
    gaps = []
    for i, pos in enumerate(VARYING):
        for bit in range(8):
            v = 1 << (i * 8 + bit)
            while v:
                h = v.bit_length() - 1
                if h in reduced:
                    v ^= reduced[h]
                else:
                    gaps.append((pos, bit))
                    break
    return gaps


def needed_missing(blobs):
    return [g for g in missing_bits(blobs) if g not in UNREACHABLE]


def stats(blobs):
    """Which byte positions actually move, and how much."""
    gaps = missing_bits(blobs)
    if gaps:
        by_byte = {}
        for pos, bit in gaps:
            by_byte.setdefault(pos, []).append(bit)
        needed = len([g for g in gaps if g not in UNREACHABLE])
        print(f'{len(gaps)} bits unspanned, {needed} of them actually needed '
              f'(bytes 212-213 bits 1-7 never vary and are not required):')
        for pos in sorted(by_byte):
            print(f'  byte {pos:3d}: bits {sorted(by_byte[pos])}')
        print()
    print('per-byte variation across all samples:')
    for pos in range(0, 218):
        vals = {b[pos] for b in blobs}
        if len(vals) > 1:
            tag = ''
            if pos not in VARYING:
                tag = '   <-- OUTSIDE the modelled set'
            print(f'  byte {pos:3d}: {len(vals):3d} distinct values{tag}')
    unchanged = [p for p in VARYING if len({b[p] for b in blobs}) == 1]
    if unchanged:
        print(f'modelled bytes that never moved: {unchanged}')


def verify_deployment(blobs, args):
    """The only test that matters: can we checksum what the server will serve?

    Chasing full rank is futile -- bytes near the terminator only ever hold
    0x00, 0xcc or 0xff, so most of their bits cannot vary at all. What counts is
    whether a year of real programmes falls inside the measured span.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
    from hozelock import schedule as sched_mod, state as state_mod
    try:
        import yaml
        cfg = yaml.safe_load(open(args.config)) if pathlib.Path(args.config).exists() else {}
    except ImportError:
        # PyYAML lives in the server venv; the sunrise/sunset default is what
        # matters here anyway.
        cfg = {}
    site = sched_mod.Site(**cfg['site']) if cfg.get('site') else sched_mod.Site(51.5, -0.1)
    events = ([sched_mod.Event(**e) for e in cfg['schedule']] if cfg.get('schedule')
              else [sched_mod.Event('sunrise', 45), sched_mod.Event('sunset', 60)])
    st = state_mod.HubState('x', site, events)

    ref, basis, _ = build_basis(blobs)
    covered, misses = 0, []
    for n in range(0, 366):
        day = datetime(2026, 9, 1) + timedelta(days=n)
        origin = st.week_origin(day)
        lead, chain = sched_mod.build(events, site, origin)
        blob = codec.encode_schedule(lead, chain)
        if checksum_with(ref, basis, blob) is None:
            misses.append((day, blob))
        else:
            covered += 1
    print(f'a year of real programmes: {covered} computable, {len(misses)} '
          f'outside the measured span')

    # A pending command sets byte 212 or 213, giving a different blob. Without
    # these, manual watering fails at runtime with everything else working.
    day = datetime(2026, 9, 1)
    lead, chain = sched_mod.build(events, site, st.week_origin(day))
    for name, flags in (('water now', b'\x00\x01\x00\x00\x00'),
                        ('stop', b'\x00\x00\x01\x00\x00')):
        blob = codec.encode_schedule(lead, chain, flags=flags)
        got = checksum_with(ref, basis, blob)
        print(f'  {name} programme: '
              f'{"computable" if got is not None else "OUTSIDE THE SPAN"}')
        if got is None:
            misses.append((name, blob))
    for day, blob in misses[:8]:
        end = len(blob[:210].rstrip(b'\xff'))
        print(f'    {day:%d %b}: payload ends at {end}, '
              f'bytes 30-35 = {blob[30:36].hex(" ")}')
    return not misses


def solve(args):
    blobs = load_samples()
    if len(blobs) < 2:
        print('not enough samples')
        return 1
    if args.stats:
        stats(blobs)
        return 0
    ref, basis, bad = build_basis(blobs)
    print(f'{len(blobs)} samples, rank {len(basis)}/{SCHEDULE_RANK} '
          f'(schedule bits), {bad} inconsistencies')
    if bad:
        print('INCONSISTENT: the checksum depends on bytes outside VARYING')
        return 1

    # A random 90/10 split: training on the first half leaves too low a rank to
    # predict anything, which looks like a pass but tests nothing.
    rng = random.Random(0)
    ok = miss = wrong = 0
    for _ in range(5):
        shuffled = blobs[:]
        rng.shuffle(shuffled)
        cut = len(shuffled) * 9 // 10
        train_ref, train_basis, _ = build_basis(shuffled[:cut])
        for blob in shuffled[cut:]:
            got = checksum_with(train_ref, train_basis, blob)
            want = (blob[216] << 8) | blob[217]
            if got is None:
                miss += 1
            elif got == want:
                ok += 1
            else:
                wrong += 1
    print(f'held-out check (5x 90/10 split): {ok} predicted correctly, '
          f'{wrong} WRONG, {miss} outside the training span')
    if wrong:
        print('the model is not linear over these bytes - stop and reconsider')
        return 1

    ok = verify_deployment(blobs, args)
    still = needed_missing(blobs)
    if still:
        by_byte = sorted({p for p, _ in still})
        print(f'({len(still)} bits unspanned in bytes {by_byte} - only a problem '
              f'if the year check above failed)')
    if not ok:
        return 2
    if args.write:
        out = pathlib.Path(__file__).resolve().parents[1] / 'server' / 'hozelock'
        out = out / 'checksum_map.py'
        lines = ['"""Solved schedule-checksum map (generated by',
                 'capture/collect-checksums.py --solve --write).',
                 '',
                 'The algorithm is unidentified but GF(2)-affine, so the map was',
                 'measured: each entry is a difference vector over the varying bytes',
                 'and the checksum delta it produces. See docs/protocol.md.',
                 '"""',
                 f'VARYING = {VARYING!r}',
                 f'REFERENCE = bytes.fromhex({ref.hex()!r})',
                 'BASIS = {']
        for h in sorted(basis):
            v, img = basis[h]
            lines.append(f'    {h}: (0x{v:x}, 0x{img:04x}),')
        lines += ['}', '']
        out.write_text('\n'.join(lines))
        print(f'wrote {out}')
    else:
        print('solved - rerun with --write to generate the server module')
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--solve', action='store_true', help='analyse what is on file')
    p.add_argument('--config',
                   default=str(pathlib.Path(__file__).resolve().parents[1]
                               / 'server' / 'config.yaml'),
                   help='schedule to verify coverage against')
    p.add_argument('--stats', action='store_true',
                   help='with --solve, show which bytes actually vary')
    p.add_argument('--write', action='store_true',
                   help='with --solve, generate the server module')
    p.add_argument('--flags', action='store_true',
                   help='capture the two command-flag samples')
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
    p.add_argument('--long', action='store_true',
                   help='bias towards long payloads, to cover the tail bytes')
    p.add_argument('--span', nargs=2, type=int, metavar=('MIN', 'MAX'),
                   help='minutes between the two daily events, for every day - '
                        'use to reproduce a particular programme shape')
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
    if args.flags:
        return collect_flags(args)
    if args.schedule is None:
        args.schedule = f'default_{args.hub}'
    return collect(args)


if __name__ == '__main__':
    sys.exit(main())
