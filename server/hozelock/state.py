"""Authoritative state for one hub, and the generation counter that drives it.

The hub has no command channel of its own: it notices the generation moved,
re-fetches the programme, and acts on what it finds (see protocol.md).
"""
import logging
import threading
from datetime import datetime, timedelta

from . import codec, checksums, schedule

log = logging.getLogger('hozelock.state')

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

DEFAULT_FLAG = 0x00

# Checksums are unidentified, so only generations we captured can be reproduced.
# Cycling through these keeps every heartbeat reply byte-accurate.
KNOWN_GENERATIONS = sorted(
    gen for (flag, gen) in checksums.CHECKSUMS if flag == DEFAULT_FLAG)


class HubState:
    def __init__(self, hub_id, site, events, clock_epoch=None,
                 restrict_generations=True, initial_generation=None,
                 corrupt_checksum=False, replay_blob=None):
        self.hub_id = hub_id
        self.site = site
        self.events = events
        self.clock_epoch = clock_epoch or datetime(2026, 1, 1)
        self.restrict_generations = restrict_generations
        self.corrupt_checksum = corrupt_checksum
        # A captured programme, served verbatim. The only way to hand the hub a
        # valid schedule checksum until the algorithm is solved.
        self.replay_blob = codec.b64_decode(replay_blob) if replay_blob else None
        self.generation = initial_generation or KNOWN_GENERATIONS[0]
        self.pending = CMD_NONE
        self.schedule_enabled = True
        self.last_seen = None
        self.watering = 'idle'
        self.generation_held = None
        self.generation_confirmed = None
        self._lock = threading.Lock()
        self._listeners = []

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
                    i = KNOWN_GENERATIONS.index(self.generation)
                except ValueError:
                    i = -1
                self.generation = KNOWN_GENERATIONS[(i + 1) % len(KNOWN_GENERATIONS)]
            else:
                self.generation += 1
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

    def water_now(self):
        self.pending = CMD_WATER
        self.bump()
        self._notify()

    def stop_watering(self):
        self.pending = CMD_STOP
        self.bump()
        self._notify()

    def set_schedule(self, events):
        self.events = events
        self.bump()
        self._notify()

    def set_enabled(self, enabled):
        self.schedule_enabled = enabled
        self.bump()
        self._notify()

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
        ck = checksums.CHECKSUMS.get((DEFAULT_FLAG, self.generation))
        if ck is None:
            log.warning('no captured checksum for generation %04x - sending zeros; '
                        'the hub may ignore this response', self.generation)
            checksum = b'\x00\x00'
        else:
            checksum = bytes([ck >> 8, ck & 0xff])
        return codec.encode_heartbeat_response(minutes, seconds, self.generation,
                                               flag=DEFAULT_FLAG, checksum=checksum)

    def flags(self):
        f = bytearray(5)
        if self.pending == CMD_WATER:
            f[1] = 0x01
        elif self.pending == CMD_STOP:
            f[2] = 0x01
        return bytes(f)

    def schedule_blob(self, now=None):
        if self.replay_blob:
            return self.replay_blob
        epoch = self.schedule_epoch(now)
        events = self.events if self.schedule_enabled else []
        lead, chain = schedule.build(events, self.site, epoch)
        checksum = b'\xff\xff' if self.corrupt_checksum else b'\x00\x00'
        return codec.encode_schedule(lead, chain, flags=self.flags(),
                                     checksum=checksum)

    def next_watering(self, now=None):
        now = now or datetime.now()
        if not self.schedule_enabled:
            return None
        upcoming = [w for w, _ in schedule.resolve(self.events, self.site, now)
                    if w >= now]
        return min(upcoming) if upcoming else None
