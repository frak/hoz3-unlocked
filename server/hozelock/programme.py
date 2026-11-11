"""Print what the hub will actually do, decoded from the programme we serve.

    uv run python -m hozelock.programme config.yaml

`next_watering` resolves the schedule directly and never consults the encoded
blob, so it cannot see an encoding fault -- it reported 20:45 while the hub held
a programme that said 15:05. This decodes the chain itself.
"""
import sys
from datetime import timedelta

from . import codec, schedule


def firings(st, now=None):
    """-> [(datetime, duration_min)] as the hub will run them."""
    epoch = st.schedule_epoch(now)
    lead, chain = schedule.build(st.events, st.site, epoch)
    out, t = [], epoch + timedelta(minutes=lead * codec.GAP_UNIT_MIN)
    for duration, gap in chain:
        out.append((t, duration))
        t += timedelta(minutes=gap * codec.GAP_UNIT_MIN)
    return out


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else 'config.yaml'
    from .__main__ import load
    _, st = load(path)

    blob = st.schedule_blob()
    print(f'schedule     {st.active_schedule!r}')
    print(f'week origin  {st.schedule_epoch()}')
    print(f'blob         {len(blob)} bytes, checksum {blob[216:218].hex()}')
    intended = [w for w, _ in schedule.resolve(st.events, st.site,
                                               st.schedule_epoch())]
    print()
    worst = 0
    for when, duration in firings(st):
        # Gaps are whole 5-minute units and each one rounds independently, so an
        # event can sit a unit plus a rounding minute from the time asked for.
        # Beyond that, the encoding has moved it.
        off = min((abs((when - w).total_seconds()) / 60 for w in intended),
                  default=None)
        worst = max(worst, off or 0)
        mark = '' if off is not None and off <= codec.GAP_UNIT_MIN + 1 else \
            f'   <-- {off:.0f} min from anything scheduled'
        print(f'  {when:%a %d %b %H:%M}  {duration:3d} min{mark}')
    print()
    print(f'largest shift from the schedule: {worst:.0f} min '
          f'(a few minutes is the {codec.GAP_UNIT_MIN}-minute gap unit rounding)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
