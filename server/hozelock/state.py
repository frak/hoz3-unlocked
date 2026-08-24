"""Authoritative state for one hub, and the generation counter that drives it.

The hub has no command channel of its own: it notices the generation moved,
re-fetches the programme, and acts on what it finds (see protocol.md).
"""
import threading
from datetime import datetime, timedelta

from . import codec, schedule

# The hub anchors its programme on a day boundary in the server's own clock
# frame. Captured blobs were consistent with that; it is not proven, so the
# live test should confirm waterings land at the intended times.
CLOCK_DAY_MINUTES = 1440

CMD_NONE = 0
CMD_WATER = 1
CMD_STOP = 2

# Checksums are unidentified, so only generations we have observed can be
# reproduced. Cycling within this range keeps every response byte-accurate.
KNOWN_GENERATION_MIN = 0x08bd
KNOWN_GENERATION_MAX = 0x0914


class HubState:
    def __init__(self, hub_id, site, events, clock_epoch=None,
                 restrict_generations=True):
        self.hub_id = hub_id
        self.site = site
        self.events = events
        self.clock_epoch = clock_epoch or datetime(2026, 1, 1)
        self.restrict_generations = restrict_generations
        self.generation = KNOWN_GENERATION_MIN
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
            self.generation += 1
            if self.restrict_generations and self.generation > KNOWN_GENERATION_MAX:
                self.generation = KNOWN_GENERATION_MIN
        return self.generation

    def clock(self, now=None):
        now = now or datetime.now()
        delta = now - self.clock_epoch
        minutes = int(delta.total_seconds() // 60) & 0xffff
        return minutes, now.second

    def schedule_epoch(self, now=None):
        """Most recent day boundary in the clock frame -- what gaps count from."""
        now = now or datetime.now()
        minutes, _ = self.clock(now)
        return now.replace(second=0, microsecond=0) - timedelta(
            minutes=minutes % CLOCK_DAY_MINUTES)

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
        return codec.encode_heartbeat_response(minutes, seconds, self.generation)

    def flags(self):
        f = bytearray(5)
        if self.pending == CMD_WATER:
            f[1] = 0x01
        elif self.pending == CMD_STOP:
            f[2] = 0x01
        return bytes(f)

    def schedule_blob(self, now=None):
        epoch = self.schedule_epoch(now)
        events = self.events if self.schedule_enabled else []
        lead, chain = schedule.build(events, self.site, epoch)
        return codec.encode_schedule(lead, chain, flags=self.flags())

    def next_watering(self, now=None):
        now = now or datetime.now()
        if not self.schedule_enabled:
            return None
        upcoming = [w for w, _ in schedule.resolve(self.events, self.site, now)
                    if w >= now]
        return min(upcoming) if upcoming else None
