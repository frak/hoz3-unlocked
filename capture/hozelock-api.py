#!/usr/bin/env python3
"""Drive the Hozelock cloud API, to vary the schedule without using the app.

Used to collect samples for solving the programme checksum: each change makes
the real service emit a new blob with a valid checksum, which the capture rig
records. See docs/protocol.md.

The hub must be on the REAL service for this (bridge mode, not routed).

    ./hozelock-api.py show
    ./hozelock-api.py demo on
    ./hozelock-api.py adjust 50 --days 2

The hub id is read from $HOZELOCK_HUB or capture/.hubid (both untracked). It is
kept out of the repo because this API has no authentication of any kind: anyone
who knows the id can rewrite schedules or open the tap.
"""
import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

BASE = 'https://hoz3.com/restful/support/hubs'
HUBID_FILE = pathlib.Path(__file__).with_name('.hubid')


def resolve_hub(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get('HOZELOCK_HUB')
    if env:
        return env.strip()
    if HUBID_FILE.exists():
        return HUBID_FILE.read_text().strip()
    sys.exit(f'no hub id: set $HOZELOCK_HUB or write it to {HUBID_FILE}')


def call(hub, path='', method='GET', payload=None):
    url = f'{BASE}/{hub}{path}'
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


# startTime is a sentinel for solar events rather than a clock offset.
SOLAR = {-2000: 'sunrise', -1000: 'sunset'}


def when(ms):
    if ms in SOLAR:
        return SOLAR[ms]
    if ms < 0:
        return f'solar? ({ms})'
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}'


def show(args):
    status, body = call(args.hub)
    print(f'GET /hubs/{args.hub} -> {status}')
    if not isinstance(body, dict):
        print(body)
        return 1
    if args.raw:
        print(json.dumps(body, indent=2))
        return 0
    hub = body.get('hub', body)
    print(f"hub={hub.get('hubID')} name={hub.get('name')!r} mode={hub.get('mode')!r}")
    loc = hub.get('location', {})
    print(f"location={loc.get('city')} {loc.get('timezone')}")

    print('\n--- controllers ---')
    for c in hub.get('controllers', []) or []:
        print(f'  {json.dumps(c)}')
    if not hub.get('controllers'):
        print('  (none returned)')

    print('\n--- schedules ---')
    for sch in hub.get('schedules', []) or []:
        days = sch.get('scheduleDays', {})
        print(f"  {sch.get('scheduleID')!r}: {sch.get('name')!r}")
        for day, d in days.items():
            events = [f"{when(e['startTime'])} for {e['duration'] // 60000}min"
                      + ('' if e.get('enabled', True) else ' (off)')
                      for e in d.get('wateringEvents', [])]
            if events:
                print(f'      {day:<10} ' + ', '.join(events))
    return 0


def get_schedule(hub, sid):
    _, body = call(hub, '/schedules')
    scheds = body.get('schedules', body) if isinstance(body, dict) else body
    for sch in scheds or []:
        if (sch.get('scheduleID') or sch.get('id')) == sid:
            return sch
    return None


def put_schedule(hub, sid, schedule):
    """The write the app uses. The payload must be wrapped in a 'schedule' key."""
    return call(hub, f'/schedules/{sid}', 'PUT', {'schedule': schedule})


def set_events(schedule, events, days=None):
    """Replace the watering events. `events` is [(start_ms, duration_min)]."""
    for day, d in schedule['scheduleDays'].items():
        if days and day not in days:
            continue
        d['wateringEvents'] = [
            {'startTime': start, 'endTime': start + minutes * 60_000 - 2000,
             'duration': minutes * 60_000, 'enabled': True}
            for start, minutes in events
        ]
    return schedule


def probe_write(args):
    """Is there an undocumented schedule write? The app must use something."""
    status, body = call(args.hub, '/schedules')
    print(f'GET /schedules -> {status}')
    scheds = body.get('schedules', body) if isinstance(body, dict) else body
    if not isinstance(scheds, list) or not scheds:
        print(json.dumps(body, indent=2)[:1000] if body else body)
        return 1
    s = scheds[0]
    sid = s.get('scheduleID') or s.get('id')
    print(f'\nschedule {sid} as returned:\n{json.dumps(s, indent=2)[:1500]}')
    code, body = put_schedule(args.hub, sid, s)
    print(f'\nPUT (wrapped, unchanged payload) -> {code}')
    if isinstance(body, str):
        print(f'   {body[:300]}')
    elif body:
        print(f'   {json.dumps(body)[:300]}')
    return 0


def action(hub, name, payload):
    code, body = call(hub, f'/controllers/actions/{name}', 'POST', payload)
    print(f'POST {name} {payload} -> {code} {body if isinstance(body, str) else ""}')
    return code


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--hub', help='hub id (default: $HOZELOCK_HUB or capture/.hubid)')
    p.add_argument('--controller', type=int, help='controller id (default: first found)')
    sub = p.add_subparsers(dest='cmd', required=True)
    sh = sub.add_parser('show'); sh.add_argument('--raw', action='store_true')
    sub.add_parser('probe-write')
    a = sub.add_parser('adjust'); a.add_argument('percent', type=int)
    a.add_argument('--days', type=int, default=1)
    sub.add_parser('unadjust')
    d = sub.add_parser('demo'); d.add_argument('state', choices=['on', 'off'])
    w = sub.add_parser('water'); w.add_argument('--minutes', type=int, default=1)
    sub.add_parser('stop')
    st = sub.add_parser('set', help='overwrite a schedule\'s events')
    st.add_argument('--schedule', help='default: the hub default schedule')
    st.add_argument('--event', action='append', required=True,
                    metavar='START:MINUTES',
                    help="e.g. 'sunrise:45', 'sunset:60', '06:00:30'")
    args = p.parse_args()
    args.hub = resolve_hub(args.hub)
    if getattr(args, 'schedule', None) is None:
        args.schedule = f'default_{args.hub}'

    if args.cmd == 'show':
        return show(args)
    if args.cmd == 'probe-write':
        return probe_write(args)
    if args.cmd == 'set':
        sch = get_schedule(args.hub, args.schedule)
        if not sch:
            print(f'no schedule {args.schedule!r}', file=sys.stderr)
            return 1
        events = []
        for spec in args.event:
            when_, _, mins = spec.rpartition(':')
            if when_ == 'sunrise':
                start = -2000
            elif when_ == 'sunset':
                start = -1000
            else:
                hh, mm = when_.split(':')
                start = (int(hh) * 60 + int(mm)) * 60_000
            events.append((start, int(mins)))
        code, body = put_schedule(args.hub, args.schedule, set_events(sch, events))
        print(f'PUT {args.schedule} {events} -> {code}')
        if isinstance(body, str):
            print(f'   {body[:300]}')
        return 0 if code < 300 else 1

    cid = args.controller
    if cid is None:
        _, body = call(args.hub)
        hub = body.get('hub', body) if isinstance(body, dict) else {}
        controllers = hub.get('controllers', []) or []
        if not controllers:
            print('no controller found; pass --controller', file=sys.stderr)
            return 1
        cid = controllers[0].get('id') or controllers[0].get('controllerID')
    ids = {'controllerIDs': [cid]}

    if args.cmd == 'adjust':
        return action(args.hub, 'adjust',
                      {**ids, 'duration': args.days, 'wateringAdjustment': args.percent})
    if args.cmd == 'unadjust':
        return action(args.hub, 'unadjust', ids)
    if args.cmd == 'demo':
        return action(args.hub, 'setMode',
                      {**ids, 'mode': 'demo' if args.state == 'on' else 'normal'})
    if args.cmd == 'water':
        return action(args.hub, 'waterNow', {**ids, 'duration': args.minutes * 60_000})
    if args.cmd == 'stop':
        return action(args.hub, 'stopWatering', ids)


if __name__ == '__main__':
    sys.exit(main())
