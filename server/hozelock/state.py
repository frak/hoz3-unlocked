"""Authoritative state for one hub, and the generation counter that drives it.

The hub has no command channel of its own: it notices the generation moved,
re-fetches the programme, and acts on what it finds (see protocol.md).
"""
import logging
import re
import threading
from dataclasses import replace
from datetime import datetime, timedelta

from . import codec, checksums, heartbeat_checksum, schedule

log = logging.getLogger('hozelock.state')

TIME_RE = re.compile(r'^\d{1,2}:\d{2}$')

# The clock is a position within a 7-day week, 0..10079, not a free-running
# counter -- the capture contains the wrap (10078 -> 14). A programme is always
# 2016 units = 10080 minutes = exactly one clock week, and its lead gap is the
# offset from minute zero. Both must share this origin or the hub rejects the
# programme.
CLOCK_WEEK_MINUTES = 7 * 24 * 60

# Hozelock's week began Saturday 22:55 local. The value is arbitrary so long as
# clock and programme agree, but matching it lets captured blobs be replayed.
WEEK_ORIGIN_WEEKDAY = 5
WEEK_ORIGIN_HOUR = 22
WEEK_ORIGIN_MINUTE = 55

CMD_NONE = 0
CMD_WATER = 1
CMD_STOP = 2
COMMAND_NAMES = {CMD_WATER: 'water', CMD_STOP: 'stop'}

# Response byte 5 tells the hub whether demo mode is on: 0x10 switches the
# controller to a 1-minute radio poll and the hub to ~7-second heartbeats,
# against ~16 minutes and 20 minutes normally. The cloud drives this on every
# heartbeat, so the app's setting is overridden the moment we take over.
FLAG_NORMAL = 0x00
FLAG_DEMO = 0x10

# The hub acts when the generation *changes*, not when it grows (the old table
# cycled it non-monotonically and the hub fetched fine), so capping it is free.
# 0x1fff keeps every served heartbeat checksum inside the live-validated range,
# never in the unproven high-byte8 extrapolation.
GENERATION_MAX = 0x1fff


def generations_for(flag):
    """Only generations we hold a captured checksum for, at this flag value."""
    return sorted(gen for (f, gen) in checksums.CHECKSUMS if f == flag)


class HubState:
    def __init__(self, hub_id, site, events=None, clock_epoch=None,
                 restrict_generations=False, initial_generation=None,
                 corrupt_checksum=False, replay_blob=None, demo_mode=False,
                 schedules=None, active_schedule=None, overlays=None,
                 state_file=None):
        self.hub_id = hub_id
        self.site = site
        # `events` stays positional for the single-schedule callers; a bare list
        # is just the degenerate case of one schedule.
        self.schedules = list(schedules) if schedules else [
            schedule.Schedule(name='Default', events=list(events or []))]
        self.active_schedule = active_schedule or self.schedules[0].name
        if not self._find(self.active_schedule):
            log.warning('active_schedule %r is not defined - using %r',
                        active_schedule, self.schedules[0].name)
            self.active_schedule = self.schedules[0].name
        self.overlays = overlays or schedule.Overlays()
        self.state_file = state_file
        self.clock_epoch = clock_epoch or datetime(2026, 1, 1)
        self.restrict_generations = restrict_generations
        self.corrupt_checksum = corrupt_checksum
        self.flag = FLAG_DEMO if demo_mode else FLAG_NORMAL
        self.generations = generations_for(self.flag)
        # A captured programme, served verbatim. The only way to hand the hub a
        # valid schedule checksum until the algorithm is solved.
        self.replay_blob = codec.b64_decode(replay_blob) if replay_blob else None
        self.generation = initial_generation or self.generations[0]
        if self.restrict_generations and self.generation not in self.generations:
            # Checksums are per (flag, generation); a value valid in one mode is
            # usually absent in the other, and the hub ignores unchecksummed replies.
            log.warning('initial_generation %04x has no captured checksum at '
                        'flag %02x - using %04x instead',
                        self.generation, self.flag, self.generations[0])
            self.generation = self.generations[0]
        self.pending = CMD_NONE
        self.schedule_enabled = True
        self.last_seen = None
        self.watering = 'idle'
        self.generation_held = None
        self.generation_confirmed = None
        self._warned_checksum = False
        self._lock = threading.Lock()
        self._listeners = []

    def _find(self, name):
        return next((s for s in self.schedules if s.name == name), None)

    def active(self):
        return self._find(self.active_schedule) or self.schedules[0]

    @property
    def events(self):
        """The active schedule's events. Read-only: mutate via set_event so the
        change is validated, persisted, and picked up by the hub."""
        return self.active().events

    def _persist(self):
        if self.state_file:
            from . import persist
            persist.save(self.state_file, self)

    def _rejects(self, events=None, overlays=None):
        """-> a reason string if this would produce an unservable programme.

        schedule_blob() is on the hub's fetch path, so an edit that cannot encode
        would leave the hub with nothing at all. Trial-build before committing.
        """
        try:
            lead, chain = schedule.build(
                events if events is not None else self.events,
                self.site, self.schedule_epoch(),
                overlays=overlays if overlays is not None else self.overlays)
            codec.encode_schedule(lead, chain, flags=self.flags())
        except Exception as e:
            return str(e)
        return None

    def _commit(self, what):
        self.bump()
        self._persist()
        self._notify()
        log.info('%s', what)

    def on_change(self, fn):
        self._listeners.append(fn)

    def _notify(self):
        for fn in self._listeners:
            fn(self)

    def bump(self):
        """Advance the generation so the hub re-fetches. Nothing happens without this."""
        with self._lock:
            if self.restrict_generations:
                # Step to the next generation we hold a checksum for, wrapping.
                # The hub only needs the value to change, not to increase.
                try:
                    i = self.generations.index(self.generation)
                except ValueError:
                    i = -1
                self.generation = self.generations[(i + 1) % len(self.generations)]
            else:
                # Cycle 1..GENERATION_MAX (never 0, which the hub treats as a
                # fresh boot). Always changes, so the hub always re-fetches.
                self.generation = (self.generation % GENERATION_MAX) + 1
        return self.generation

    def week_origin(self, now=None):
        """Start of the current clock week: the instant the counter reads zero."""
        now = now or datetime.now()
        anchor = now.replace(hour=WEEK_ORIGIN_HOUR, minute=WEEK_ORIGIN_MINUTE,
                             second=0, microsecond=0)
        anchor -= timedelta(days=(anchor.weekday() - WEEK_ORIGIN_WEEKDAY) % 7)
        if anchor > now:
            anchor -= timedelta(days=7)
        return anchor

    def clock(self, now=None):
        now = now or datetime.now()
        minutes = int((now - self.week_origin(now)).total_seconds() // 60)
        return minutes % CLOCK_WEEK_MINUTES, now.second

    def schedule_epoch(self, now=None):
        return self.week_origin(now)

    def _queue(self, command, name):
        # One slot, last write wins. The hub may be offline for minutes, so an
        # undelivered command being replaced is silent data loss unless said.
        if self.pending and self.pending != command:
            log.warning('replacing an undelivered %s command with %s - the hub '
                        'never collected the first one',
                        COMMAND_NAMES.get(self.pending, self.pending), name)
        self.pending = command
        self.bump()
        self._notify()

    def water_now(self):
        self._queue(CMD_WATER, 'water')

    def stop_watering(self):
        self._queue(CMD_STOP, 'stop')

    def set_schedule(self, events):
        """Replace the active schedule's events wholesale."""
        reason = self._rejects(events=events)
        if reason:
            return reason
        self.active().events = list(events)
        self._commit(f'schedule {self.active_schedule!r} updated')
        return None

    def select_schedule(self, name):
        """Point the hub at a different schedule. -> a reason string on refusal."""
        if not self._find(name):
            known = ', '.join(s.name for s in self.schedules)
            return f'no schedule named {name!r} (have: {known})'
        if name == self.active_schedule:
            return None
        previous = self.active_schedule
        self.active_schedule = name
        reason = self._rejects()
        if reason:
            self.active_schedule = previous
            return reason
        self._commit(f'active schedule {previous!r} -> {name!r}')
        return None

    def set_event(self, slot, mode=None, at=None, duration_min=None):
        """Edit one event of the active schedule. -> a reason string on refusal.

        `slot` is 0-based. Modes are 'clock', 'sunrise', 'sunset' and 'unused';
        an unused slot is simply absent from the served programme.
        """
        events = [replace(e) for e in self.events]
        while len(events) <= slot:
            events.append(schedule.Event(at='06:00', duration_min=0))
        ev = events[slot]

        if mode is not None:
            mode = mode.strip().lower()
            if mode == 'unused':
                events[slot] = None
            elif mode in ('sunrise', 'sunset'):
                ev.at = mode
            elif mode == 'clock':
                # Carry the existing clock time across a solar->clock switch;
                # the UI sends mode and time as two separate messages.
                if ev.at in ('sunrise', 'sunset'):
                    ev.at = at or '06:00'
            else:
                return f'unknown mode {mode!r}'
        if at is not None and events[slot] is not None:
            if not TIME_RE.match(at):
                return f'{at!r} is not HH:MM'
            hh, mm = (int(x) for x in at.split(':'))
            if hh > 23 or mm > 59:
                return f'{at!r} is not a valid time'
            if ev.at not in ('sunrise', 'sunset'):
                ev.at = at
        if duration_min is not None and events[slot] is not None:
            duration_min = int(duration_min)
            if not 0 <= duration_min <= 0xff:
                return f'duration {duration_min} is outside 0-255'
            ev.duration_min = duration_min

        events = [e for e in events if e is not None]
        reason = self._rejects(events=events)
        if reason:
            return reason
        self.active().events = events
        self._commit(f'schedule {self.active_schedule!r} slot {slot + 1} edited')
        return None

    def pause(self, days, now=None):
        """Suppress watering for N days. 0 cancels. -> a reason string on refusal."""
        days = float(days)
        if days < 0:
            return 'pause days cannot be negative'
        now = now or datetime.now()
        until = now + timedelta(days=days) if days else None
        reason = self._rejects(overlays=replace(self.overlays, pause_until=until))
        if reason:
            return reason
        self.overlays.pause_until = until
        self._commit(f'paused until {until}' if until else 'pause cancelled')
        return None

    def adjust(self, percent, days=1, now=None):
        """Scale durations by percent for N days. 0 cancels."""
        percent, days = int(percent), float(days)
        if days <= 0 and percent:
            return 'adjust needs a positive number of days'
        now = now or datetime.now()
        trial = (replace(self.overlays, adjust_percent=percent,
                         adjust_until=now + timedelta(days=days))
                 if percent else
                 replace(self.overlays, adjust_percent=0, adjust_until=None))
        reason = self._rejects(overlays=trial)
        if reason:
            return reason
        self.overlays.adjust_percent = trial.adjust_percent
        self.overlays.adjust_until = trial.adjust_until
        self._commit(f'adjust {percent:+d}% until {trial.adjust_until}'
                     if percent else 'adjust cancelled')
        return None

    def expire_overlays(self, now=None):
        """Drop lapsed overlays. -> True if anything changed.

        The hub only refetches when the generation moves, so an expiry that does
        not bump would keep serving the boosted programme indefinitely.
        """
        now = now or datetime.now()
        changed = False
        if self.overlays.pause_until and now >= self.overlays.pause_until:
            self.overlays.pause_until = None
            changed = True
        if self.overlays.adjust_until and now >= self.overlays.adjust_until:
            self.overlays.adjust_percent = 0
            self.overlays.adjust_until = None
            changed = True
        if changed:
            self._commit('overlay expired')
        return changed

    def set_enabled(self, enabled):
        self.schedule_enabled = enabled
        self._commit(f'schedule {"enabled" if enabled else "disabled"}')

    def observe(self, request_blob):
        fields = codec.decode_heartbeat_request(request_blob)
        self.last_seen = datetime.now()
        self.watering = fields['state']
        self.generation_held = fields['generation_held']
        self.generation_confirmed = fields['generation_confirmed']
        # The hub only echoes a command once it has acted on it.
        if self.pending and self.generation_confirmed == self.generation:
            self.pending = CMD_NONE
        self._notify()
        return fields

    def heartbeat_response(self, now=None):
        minutes, seconds = self.clock(now)
        ck = heartbeat_checksum.checksum(self.flag, self.generation)
        checksum = bytes([ck >> 8, ck & 0xff])
        return codec.encode_heartbeat_response(minutes, seconds, self.generation,
                                               flag=self.flag, checksum=checksum)

    def flags(self):
        f = bytearray(5)
        if self.pending == CMD_WATER:
            f[1] = 0x01
        elif self.pending == CMD_STOP:
            f[2] = 0x01
        return bytes(f)

    def schedule_blob(self, now=None):
        if self.replay_blob:
            # Commands ride in the flag bytes, which a replayed blob cannot carry:
            # changing them would invalidate the captured checksum it exists for.
            if self.pending:
                log.warning('replay_blob is set, so the pending %s command cannot '
                            'be sent - the hub will do nothing. Unset replay_blob.',
                            COMMAND_NAMES.get(self.pending, self.pending))
            return self.replay_blob
        epoch = self.schedule_epoch(now)
        events = self.events if self.schedule_enabled else []
        lead, chain = schedule.build(events, self.site, epoch,
                                     overlays=self.overlays)
        blob = codec.encode_schedule(lead, chain, flags=self.flags())
        if self.corrupt_checksum:
            return blob[:216] + b'\xff\xff'
        ck = codec.schedule_checksum(blob)
        if ck is None:
            if not self._warned_checksum:
                self._warned_checksum = True
                log.error('cannot compute the schedule checksum - the hub will '
                          'refuse this programme. Run capture/collect-checksums.py')
            return blob
        return blob[:216] + bytes([ck >> 8, ck & 0xff])

    def next_watering(self, now=None):
        now = now or datetime.now()
        if not self.schedule_enabled:
            return None
        firings = [f for f in schedule.resolve(self.events, self.site, now)
                   if f[0] >= now]
        # Without this a paused system still advertises a watering it will not do.
        firings = self.overlays.apply(firings)
        return min(w for w, _ in firings) if firings else None
