#!/usr/bin/env python3
"""Decode Hozelock hub <-> hoz3.com traffic extracted from the capture pcaps.

Input is the TSV produced by (see capture-setup.md):

    mergecap -w - /var/captures/hub-*.pcap | tshark -r - -Y http \
      -T fields -e frame.time_epoch -e ip.src -e http.request.uri -e http.file_data
"""
import argparse, re, sys
from datetime import datetime
from pathlib import Path

# Shared with the server so the wire format has exactly one implementation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from hozelock import codec

HB_RE = re.compile(r'hb=([A-Za-z0-9_\-=]+)')

STATES = {
    (0x00, 0x00): 'idle',
    (0x01, 0x10): 'watering (manual)',
    (0x01, 0x04): 'watering (scheduled)',
}

TAP_UNIT_MIN = codec.GAP_UNIT_MIN
TAP_CYCLE_UNITS = codec.CYCLE_UNITS

b64 = codec.b64_decode
decode_tap = codec.decode_schedule


def hexs(b):
    return ' '.join(f'{x:02x}' for x in b)


def load(path):
    """-> list of (epoch, 'REQ'|'RES', 'base'|'tap', bytes)"""
    out = []
    for line in open(path):
        f = line.rstrip('\n').split('\t')
        f += [''] * (4 - len(f))
        ts, src, uri, data = f[0], f[1], f[2], f[3]
        if not ts:
            continue
        ep = 'tap' if '/tap/' in uri else 'base'
        if src.startswith('192.168.'):
            m = HB_RE.search(uri)
            if m:
                out.append((float(ts), 'REQ', ep, b64(m.group(1))))
        elif data:
            blob = codec.unwrap(bytes.fromhex(data).decode('latin1'))
            if blob:
                out.append((float(ts), 'RES', ep, blob))
    out.sort(key=lambda r: r[0])
    return out


def load_events(path):
    ev = []
    for line in open(path):
        m = re.match(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})\s+(.*)', line)
        if m:
            ts = datetime.strptime(f'{m.group(1)} {m.group(2)}', '%Y-%m-%d %H:%M').timestamp()
            ev.append((ts, m.group(3).strip()))
    return sorted(ev)


def fmt_hb_req(b):
    state = STATES.get((b[7], b[8]), f'? {b[7]:02x} {b[8]:02x}')
    return (f'state={state:<22} held={b[16]:02x}{b[17]:02x} '
            f'confirmed={b[20]:02x}{b[21]:02x} link={hexs(b[11:14])}')


def fmt_hb_res(b):
    return (f'clock={(b[0] << 8) | b[1]}m{b[2]:02d}s  gen={b[8]:02x}{b[9]:02x}  '
            f'flag5={b[5]:02x}')




def watering_runs(rows):
    """Start times of scheduled waterings, from the heartbeat stream.

    Lengths are quantised by the heartbeat interval (~16 min outside demo mode),
    so only the start times are trustworthy.
    """
    runs, start, last = [], None, None
    for ts, kind, ep, b in rows:
        if kind != 'REQ' or ep != 'base':
            continue
        active = (b[7], b[8]) == (0x01, 0x04)
        if active and start is None:
            start = ts
        elif not active and start is not None:
            runs.append((start, (last - start) / 60))
            start = None
        last = ts
    return runs


def cmd_tap(rows, args):
    runs = watering_runs(rows)
    first_seen = {}
    for ts, kind, ep, b in rows:
        if ep == 'tap' and kind == 'RES':
            first_seen.setdefault(bytes(b), ts)
    seen = set()
    for ts, kind, ep, b in rows:
        if ep != 'tap' or kind != 'RES':
            continue
        lead, events = decode_tap(b)
        flags, ck = b[211:216], b[216:218]
        key = bytes(b)
        tag = '' if key not in seen else '   (unchanged)'
        seen.add(key)
        print(f'\n{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M:%S}  {len(b)} bytes{tag}')
        total = lead + sum(g for _, g in events)
        if not events and total == 0:
            ok = 'no schedule'
        else:
            ok = 'OK' if total == TAP_CYCLE_UNITS else f'!! expected {TAP_CYCLE_UNITS}'
        print(f'  lead gap={lead} units ({lead * 5 / 60:.2f}h)  flags={hexs(flags)}  '
              f'checksum={hexs(ck)}')
        print(f'  cycle total={total} units = {total * 5 / 60:.0f}h  [{ok}]')
        if not events:
            if any(x != 0xff for x in b[2:16]):
                # weekly/sparse schedules use a different, not-yet-decoded layout
                print('  UNRECOGNISED PAYLOAD (not the daily interval format):')
                print(f'    {hexs(b[:20])}')
            else:
                print('  no events — all schedules disabled')
            continue
        # The blob carries no absolute time and is identical across fetches, so
        # event[0] cannot be placed on the clock from the blob alone. Supply
        # --anchor to resolve it; otherwise offsets are relative to event[0].
        anchor = None
        if args.anchor:
            hh, mm = args.anchor.split(':')
            anchor = datetime.fromtimestamp(ts).replace(
                hour=int(hh), minute=int(mm), second=0, microsecond=0).timestamp()
        cursor, offset = anchor, 0
        for n, (dur, gapu) in enumerate(events):
            gap = gapu * TAP_UNIT_MIN
            when = (f'{datetime.fromtimestamp(cursor):%H:%M}' if cursor
                    else f'+{offset // 60:3d}h{offset % 60:02d}')
            print(f'  [{n:2d}] {when}  water {dur:3d} min   then +{gap // 60}h{gap % 60:02d}')
            if cursor:
                cursor += gap * 60
            offset += gap

        if anchor:
            print(f'  anchored on --anchor {args.anchor}')
        else:
            nxt = [rt for rt, _ in runs if rt > first_seen[key]]
            if nxt:
                print(f'  (no --anchor; next observed watering was '
                      f'{datetime.fromtimestamp(nxt[0]):%m-%d %H:%M})')


def cmd_timeline(rows, args):
    events = load_events(args.events) if args.events else []
    ei = 0
    prev = {}
    for ts, kind, ep, b in rows:
        while ei < len(events) and events[ei][0] <= ts:
            print(f'\n>>> {datetime.fromtimestamp(events[ei][0]):%m-%d %H:%M}  '
                  f'*** {events[ei][1]} ***')
            ei += 1
        key = (kind, ep)
        if args.diff and prev.get(key) == bytes(b):
            continue
        changed = ''
        if args.diff and key in prev:
            old = prev[key]
            idx = [i for i in range(len(b)) if b[i] != old[i] and i < len(b) - 2]
            changed = f'  changed@{idx}' if idx else ''
        prev[key] = bytes(b)
        stamp = f'{datetime.fromtimestamp(ts):%m-%d %H:%M:%S}'
        if ep == 'tap':
            print(f'{stamp} {kind} tap/0  ({len(b)} bytes){changed}')
        elif kind == 'REQ':
            print(f'{stamp} REQ  {fmt_hb_req(b)}{changed}')
        else:
            print(f'{stamp} res  {fmt_hb_res(b)}{changed}')


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('tsv')
    p.add_argument('--timeline', action='store_true', help='annotated exchange timeline')
    p.add_argument('--tap', action='store_true', help='decode tap/0 schedule blobs')
    p.add_argument('--diff', action='store_true', help='only show changed blobs')
    p.add_argument('--events', metavar='FILE', help='interleave an events.log')
    p.add_argument('--anchor', metavar='HH:MM', help='clock time of the first event')
    args = p.parse_args()

    rows = load(args.tsv)
    if not rows:
        sys.exit(f'no usable rows in {args.tsv}')
    if args.tap:
        cmd_tap(rows, args)
    else:
        cmd_timeline(rows, args)


if __name__ == '__main__':
    main()
