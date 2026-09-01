"""Pause and adjust: the temporary modifiers, and how they lapse.

Run with the venv interpreter: uv run python test_overlays.py

The subtle one is expiry. Overlays are applied when the programme is built, but
the hub only refetches when the generation moves -- so an overlay that lapses
without bumping would keep watering the boosted schedule indefinitely.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

from hozelock import codec, persist, schedule
from hozelock import state as state_mod

SITE = schedule.Site(latitude=51.5, longitude=-0.1)


def check(name, got, want):
    if got == want:
        print(f'  PASS  {name}')
        return True
    print(f'  FAIL  {name}\n        got  {got!r}\n        want {want!r}')
    return False


def fresh(**kw):
    return state_mod.HubState('x', SITE, schedules=[
        schedule.Schedule('Summer', [schedule.Event('06:00', 45),
                                     schedule.Event('21:00', 60)])], **kw)


def durations(state):
    return [d for d, _ in codec.decode_schedule(state.schedule_blob())[1]]


def main():
    ok = True

    print('  -- adjust --')
    st = fresh()
    ok &= check('baseline durations', sorted(set(durations(st))), [45, 60])
    ok &= check('adjust accepted', st.adjust(50, 2), None)
    # The programme starts at the week origin, so its first slots are ones the
    # clock has passed -- they fire next week and must stay unscaled.
    ok &= check('the captured scaling appears (45/60 -> 68/90)',
                {68, 90} <= set(durations(st)), True)
    ok &= check('the untouched durations are still there too',
                {45, 60} <= set(durations(st)), True)
    ok &= check('every duration is one or the other',
                set(durations(st)) <= {45, 60, 68, 90}, True)
    ok &= check('cancel restores', (st.adjust(0), sorted(set(durations(st)))),
                (None, [45, 60]))

    st = fresh()
    st.set_event(0, duration_min=200)
    ok &= check('a boost that would overflow a byte clamps instead',
                (st.adjust(100, 1), max(durations(st))), (None, 255))

    print()
    print('  -- pause --')
    st = fresh()
    before = st.next_watering()
    ok &= check('pause accepted', st.pause(3), None)
    ok &= check('next watering moves past the window',
                st.next_watering() > before + timedelta(days=2), True)
    # A slot the clock has passed fires next week, so a short pause must leave
    # it alone -- deleting it would silently skip a watering seven days out.
    ok &= check('a 3-day pause drops only 3 days of waterings',
                len(durations(fresh())) - len(durations(st)), 6)
    ok &= check('a whole-week pause collapses the programme',
                (st.pause(8), st.schedule_blob()[:4].hex(' ')),
                (None, '00 00 00 cc'))
    ok &= check('and advertises no watering at all', st.next_watering(), None)
    ok &= check('cancel restores', (st.pause(0), st.next_watering()),
                (None, before))
    ok &= check('a negative pause is refused',
                st.pause(-1), 'pause days cannot be negative')

    print()
    print('  -- expiry --')
    st = fresh()
    st.adjust(50, 2)
    gen = st.generation
    ok &= check('nothing expires early', st.expire_overlays(), False)
    ok &= check('  so the generation holds', st.generation, gen)

    st.overlays.adjust_until = datetime.now() - timedelta(seconds=1)
    ok &= check('a lapsed adjust expires', st.expire_overlays(), True)
    ok &= check('  clearing the percent', st.overlays.adjust_percent, 0)
    ok &= check('  and bumping so the hub refetches', st.generation != gen, True)
    ok &= check('  restoring the real durations', sorted(set(durations(st))), [45, 60])
    ok &= check('expiring twice is a no-op', st.expire_overlays(), False)

    st = fresh()
    st.pause(1)
    st.overlays.pause_until = datetime.now() - timedelta(seconds=1)
    ok &= check('a lapsed pause expires too', st.expire_overlays(), True)
    ok &= check('  and watering resumes', st.next_watering() is not None, True)

    print()
    print('  -- the refresher must wake before an overlay lapses --')
    st = fresh()
    now = datetime.now()
    ok &= check('no overlay, no deadline', st.overlays.next_expiry(now), None)
    st.pause(2)
    st.adjust(25, 1)
    nxt = st.overlays.next_expiry(now)
    ok &= check('the sooner of the two is reported',
                abs((nxt - (now + timedelta(days=1))).total_seconds()) < 5, True)
    st.overlays.pause_until = now - timedelta(days=1)
    ok &= check('an already-lapsed overlay is not a future deadline',
                st.overlays.next_expiry(now) > now, True)

    print()
    print('  -- overlays survive a restart with their expiry intact --')
    d = tempfile.mkdtemp()
    path = os.path.join(d, 'state.json')
    st = fresh(state_file=path)
    st.adjust(50, 2)
    st.pause(1)
    want_adj, want_pause = st.overlays.adjust_until, st.overlays.pause_until

    scheds, active, enabled, ov = persist.load(path)
    ok &= check('adjust percent survives', ov.adjust_percent, 50)
    ok &= check('adjust expiry survives to the second', ov.adjust_until, want_adj)
    ok &= check('pause expiry survives to the second', ov.pause_until, want_pause)

    revived = state_mod.HubState('x', SITE, schedules=scheds,
                                 active_schedule=active, overlays=ov)
    ok &= check('the revived server serves the same programme',
                revived.schedule_blob(), st.schedule_blob())

    print('\nOK' if ok else '\nFAILURES')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
