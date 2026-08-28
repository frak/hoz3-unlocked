"""Check the schedule engine against blobs Hozelock actually produced.

Run with the venv interpreter: ./.venv/bin/python test_schedule.py
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

from hozelock import codec, schedule

DATA = Path(__file__).resolve().parents[1] / 'data'

# Epoch implied by the captured blobs: every one taken on 22-23 Aug 2026 places
# its first event this far ahead, so the server's programme was anchored here.
EPOCH = datetime(2026, 8, 22, 22, 55)
SITE = schedule.Site(latitude=51.5, longitude=-0.1)

def captured(lead_units, path=DATA / 'captures' / 'hb-raw.tsv'):
    """The real blob whose lead gap matches, straight from the capture."""
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
        if blob and len(blob) == codec.SCHEDULE_LEN:
            lead, events = codec.decode_schedule(blob)
            if lead == lead_units and events:
                return bytes(blob)
    raise LookupError(f'no captured blob with lead {lead_units}')


def check(name, got, want):
    if got == want:
        print(f'  PASS  {name}')
        return True
    print(f'  FAIL  {name}\n        got  {got}\n        want {want}')
    return False


def main():
    ok = True

    lead, chain = schedule.build(
        [schedule.Event('06:00', 30), schedule.Event('21:00', 45)], SITE, EPOCH)
    want = captured(85)
    blob = codec.encode_schedule(lead, chain, checksum=want[216:218])
    ok &= check('fixed-time schedule reproduces captured blob',
                blob.hex(' '), want.hex(' '))
    ok &= check('cycle invariant', lead + sum(g for _, g in chain), codec.CYCLE_UNITS)

    lead, chain = schedule.build(
        [schedule.Event('sunrise', 45), schedule.Event('sunset', 60)], SITE, EPOCH)
    ok &= check('solar cycle invariant', lead + sum(g for _, g in chain),
                codec.CYCLE_UNITS)
    ok &= check('solar durations alternate 45/60',
                [d for d, _ in chain[:4]], [45, 60, 45, 60])
    # Late August: days shorten, so the sunrise->sunset gap must exceed the
    # sunset->sunrise one, and both must close a day.
    ok &= check('day/night gaps close 24h', chain[0][1] + chain[1][1], 288)
    ok &= check('daylight gap is the longer', chain[0][1] > chain[1][1], True)

    weekly = [schedule.Event('16:30', 30, days=['tue'])]
    lead, chain = schedule.build(weekly, SITE, EPOCH)
    ok &= check('weekly has one event', len(chain), 1)
    ok &= check('weekly cycle invariant', lead + chain[0][1], codec.CYCLE_UNITS)
    blob = codec.encode_schedule(lead, chain)
    ok &= check('weekly blob is well formed', len(blob), codec.SCHEDULE_LEN)

    lead2, chain2 = schedule.build(weekly, SITE, EPOCH + timedelta(minutes=5))
    ok &= check('shifting the epoch 5 min moves the lead gap by one unit',
                lead - lead2, 1)

    # The clock and the programme must share an origin, or the hub rejects the
    # programme -- this is what made the first live attempt fail.
    from hozelock import state as sm
    st = sm.HubState('x', SITE, [schedule.Event('06:00', 30),
                                 schedule.Event('21:00', 45)])
    origin = st.week_origin(datetime(2026, 8, 26, 19, 30))
    ok &= check('week origin is Hozelock\'s Saturday 22:55', origin, EPOCH)
    ok &= check('clock reproduces the captured wrap',
                [st.clock(datetime(2026, 8, 22, 22, 53))[0],
                 st.clock(datetime(2026, 8, 22, 23, 9))[0]], [10078, 14])
    ok &= check('clock never exceeds one week',
                max(st.clock(EPOCH + timedelta(minutes=m))[0]
                    for m in range(0, 10080, 97)) < sm.CLOCK_WEEK_MINUTES, True)
    lead, chain = schedule.build(st.events, SITE, st.schedule_epoch(
        datetime(2026, 8, 26, 19, 30)))
    ok &= check('programme built on the week origin still closes the cycle',
                lead + sum(g for _, g in chain), codec.CYCLE_UNITS)

    # Every programme must stay in one shape: a longer one runs into bytes whose
    # checksum bits cannot be measured, and the hub would refuse it.
    st2 = sm.HubState('x', SITE, [])
    solar = [schedule.Event('sunrise', 45), schedule.Event('sunset', 60)]
    ends, bad = set(), 0
    for n in range(366):
        day = datetime(2026, 9, 1) + timedelta(days=n)
        lead, chain = schedule.build(solar, SITE, st2.week_origin(day))
        blob = codec.encode_schedule(lead, chain)
        ends.add(len(blob[:210].rstrip(b'\xff')))
        if lead + sum(g for _, g in chain) != codec.CYCLE_UNITS:
            bad += 1
    ok &= check('a year of solar programmes keeps one payload length',
                sorted(ends), [32])
    ok &= check('every one closes the weekly cycle', bad, 0)
    ok &= check('no gap needs a continuation byte',
                max(g for _, g in chain) <= schedule.MAX_GAP_UNITS, True)

    print('\nOK' if ok else '\nFAILURES')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
