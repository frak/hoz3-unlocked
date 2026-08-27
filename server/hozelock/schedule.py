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


def build(events, site, epoch, now=None):
    """-> (lead_gap_units, [(duration_min, gap_units)]) covering exactly one week.

    The final gap is closed against epoch + 7 days so the chain totals
    CYCLE_UNITS, which is the invariant the hub's programme always satisfied.
    """
    now = now or epoch
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
