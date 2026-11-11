"""Turn a human watering schedule into the hub's 7-day interval programme.

The hub understands only durations and gaps against an epoch (see protocol.md),
so solar times must be resolved here and re-pushed as they drift.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import codec

DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
HORIZON_DAYS = 7


@dataclass
class Event:
    """One watering. `at` is 'HH:MM', 'sunrise' or 'sunset', optionally offset."""
    at: str
    duration_min: int
    days: list = field(default_factory=lambda: list(DAYS))
    offset_min: int = 0

    def matches(self, day):
        return DAYS[day.weekday()] in self.days


@dataclass
class Schedule:
    """A named set of waterings. Only ever one is active; the hub is told the
    resolved programme and never learns the others exist."""
    name: str
    events: list = field(default_factory=list)


@dataclass
class Site:
    latitude: float
    longitude: float
    timezone: str = 'Europe/London'


def _solar(site, day, which):
    from astral import LocationInfo
    from astral.sun import sun
    loc = LocationInfo(latitude=site.latitude, longitude=site.longitude,
                       timezone=site.timezone)
    s = sun(loc.observer, date=day.date(), tzinfo=loc.tzinfo)
    return s[which].replace(tzinfo=None)


def resolve(events, site, start):
    """-> [(datetime, duration_min)] for the horizon, in chronological order.

    `start` anchors the horizon; the hub's programme is a rolling week from there.
    """
    out = []
    # One day past the horizon: a week from a late-evening epoch still contains
    # the final day's waterings.
    for n in range(HORIZON_DAYS + 1):
        day = (start + timedelta(days=n)).replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
        for ev in events:
            if not ev.matches(day):
                continue
            if ev.at in ('sunrise', 'sunset'):
                when = _solar(site, day, ev.at)
            else:
                hh, mm = ev.at.split(':')
                when = day.replace(hour=int(hh), minute=int(mm))
            when += timedelta(minutes=ev.offset_min)
            out.append((when, ev.duration_min))
    return sorted(out)


def _to_units(delta):
    """Gaps are whole 5-minute units; the hub has no finer resolution."""
    return round(delta.total_seconds() / 60 / codec.GAP_UNIT_MIN)


# A gap of 200 units or more needs a continuation byte, which lengthens the
# programme into byte positions whose checksum bits cannot be measured (they
# only ever hold a terminator or padding). Keeping every gap below that costs a
# few minutes' shift for about a week around midsummer.
MAX_GAP_UNITS = 199


def clamp_gaps(firings, horizon_end):
    """Pull events earlier so no gap needs a continuation byte."""
    out = list(firings)
    for i in range(len(out)):
        nxt = out[i + 1][0] if i + 1 < len(out) else horizon_end
        over = _to_units(nxt - out[i][0]) - MAX_GAP_UNITS
        if over > 0 and i + 1 < len(out):
            when, duration = out[i + 1]
            out[i + 1] = (when - timedelta(minutes=over * codec.GAP_UNIT_MIN),
                          duration)
    return out


def build(events, site, epoch):
    """-> (lead_gap_units, [(duration_min, gap_units)]) covering exactly one week.

    The final gap is closed against epoch + 7 days so the chain totals
    CYCLE_UNITS, which is the invariant the hub's programme always satisfied.
    """
    firings = [f for f in resolve(events, site, epoch) if f[0] >= epoch]
    horizon_end = epoch + timedelta(days=HORIZON_DAYS)
    firings = [f for f in firings if f[0] < horizon_end]
    if not firings:
        return 0, []

    firings = clamp_gaps(firings, horizon_end)
    lead = _to_units(firings[0][0] - epoch)
    out = []
    for i, (when, duration) in enumerate(firings):
        nxt = firings[i + 1][0] if i + 1 < len(firings) else horizon_end
        out.append((duration, _to_units(nxt - when)))

    total = lead + sum(g for _, g in out)
    drift = codec.CYCLE_UNITS - total
    if drift:
        # Rounding each gap independently can lose a unit; absorb it in the last.
        duration, gap = out[-1]
        out[-1] = (duration, gap + drift)
    return lead, out


def encode(events, site, epoch, flags=b'\x00' * 5, checksum=b'\x00\x00'):
    lead, chain = build(events, site, epoch)
    return codec.encode_schedule(lead, chain, flags=flags, checksum=checksum)


# The cloud is the only source of the app's schedules and shuts down at the end
# of April 2027, so importing them is a now-or-never job.

# Negative startTimes are sentinels, not clock offsets.
CLOUD_SOLAR = {-2000: 'sunrise', -1000: 'sunset'}

# Ordering hints only -- they keep exported YAML diffable and change no
# behaviour; real firing order comes from resolve().
_ORDER_HINT = {'sunrise': 6 * 60, 'sunset': 21 * 60}


def _cloud_time(ms):
    if ms in CLOUD_SOLAR:
        return CLOUD_SOLAR[ms]
    if ms < 0:
        # Guessing a clock time here would water at the wrong hour, silently.
        raise ValueError(f'unknown solar sentinel {ms}; expected one of '
                         f'{sorted(CLOUD_SOLAR)}')
    total = round(ms / 60000)
    return f'{total // 60 % 24:02d}:{total % 60:02d}'


def _order(at):
    if at in _ORDER_HINT:
        return _ORDER_HINT[at]
    hh, mm = at.split(':')
    return int(hh) * 60 + int(mm)


def from_cloud_schedules(payload):
    """Cloud REST schedules -> [Schedule].

    Accepts the whole hub object, a bare schedules list, or one schedule (the
    shape in data/schedule-snapshot.json).
    """
    if isinstance(payload, dict):
        scheds = payload.get('schedules')
        if scheds is None:
            scheds = (payload.get('hub') or {}).get('schedules')
        if scheds is None:
            scheds = [payload]
    else:
        scheds = payload or []

    out = []
    for sch in scheds:
        grouped = {}
        for key, day in (sch.get('scheduleDays') or {}).items():
            short = (day.get('dayOfWeek') or key).strip()[:3].lower()
            if short not in DAYS:
                continue
            for ev in day.get('wateringEvents') or []:
                # Disabled events are the app's way of holding an empty day.
                if not ev.get('enabled', True):
                    continue
                at = _cloud_time(ev.get('startTime', 0))
                minutes = round(ev.get('duration', 0) / 60000)
                grouped.setdefault((at, minutes), set()).add(short)

        events = []
        for (at, minutes), days in grouped.items():
            events.append(Event(at=at, duration_min=minutes,
                                days=[d for d in DAYS if d in days]))
        events.sort(key=lambda e: (_order(e.at), e.duration_min))
        out.append(Schedule(
            name=sch.get('name') or sch.get('scheduleID') or 'Schedule',
            events=events))
    return out


def to_config(schedules):
    """-> plain dicts for config.yaml, omitting `days` when it is every day."""
    out = []
    for sch in schedules:
        events = []
        for ev in sch.events:
            e = {'at': ev.at, 'duration_min': ev.duration_min}
            if list(ev.days) != DAYS:
                e['days'] = list(ev.days)
            if ev.offset_min:
                e['offset_min'] = ev.offset_min
            events.append(e)
        out.append({'name': sch.name, 'events': events})
    return out
