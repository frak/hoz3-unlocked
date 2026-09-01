"""Home Assistant bridge over MQTT discovery.

Entities appear in HA automatically; no custom component and no YAML.
"""
import json
import logging

import paho.mqtt.client as mqtt

log = logging.getLogger('hozelock.mqtt')

DISCOVERY_PREFIX = 'homeassistant'

MODE_UNUSED = 'Unused'
MODE_CLOCK = 'Clock'
MODES = [MODE_CLOCK, 'Sunrise', 'Sunset', MODE_UNUSED]

# Editable slots the card exposes. A minimum, not a cap -- see slot_count().
DEFAULT_SLOTS = 4


def slot_count(state, minimum=DEFAULT_SLOTS):
    """Never fewer slots than the longest schedule has events.

    Otherwise the card would edit four waterings while the hub was served six,
    with no way to see or reach the rest.
    """
    longest = max((len(s.events) for s in state.schedules), default=0)
    return max(minimum, longest)


def mode_of(event):
    if event.at in ('sunrise', 'sunset'):
        return event.at.capitalize()
    return MODE_CLOCK


class Bridge:
    def __init__(self, state, host, port=1883, username=None, password=None,
                 node_id='hozelock', max_event_slots=DEFAULT_SLOTS):
        self.state = state
        self.node = node_id
        self.slots = slot_count(state, max_event_slots)
        # Discovery configs are retained, so a slot that disappears between runs
        # would linger in HA as a permanently unavailable entity.
        self.stale_slots = range(self.slots + 1, self.slots + 9)
        self.base = f'hozelock/{node_id}'
        self.availability = f'{self.base}/available'
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f'{node_id}-bridge')
        if username:
            self.client.username_pw_set(username, password)
        # LWT so HA shows the entities as unavailable if this process dies.
        self.client.will_set(self.availability, 'offline', retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.host, self.port = host, port
        # Not hub state: it only means anything paired with a percent, and the
        # expiry it produces is what actually gets persisted.
        self.adjust_days = 1
        state.on_change(lambda _: self.publish_state())

    @staticmethod
    def _days_left(until):
        if not until:
            return 0
        from datetime import datetime
        left = (until - datetime.now()).total_seconds() / 86400
        return max(0, round(left))

    def _device(self):
        return {
            'identifiers': [self.node],
            'name': 'Hozelock Cloud Controller',
            'manufacturer': 'Hozelock',
            'model': 'Cloud Controller (local replacement)',
        }

    def _discovery(self, component, object_id, config):
        config.update({
            'device': self._device(),
            'unique_id': f'{self.node}_{object_id}',
            # Without object_id HA derives the entity_id from the device name,
            # so renaming the device silently breaks every card that names one.
            'object_id': f'{self.node}_{object_id}',
            'availability_topic': self.availability,
        })
        self.client.publish(
            f'{DISCOVERY_PREFIX}/{component}/{self.node}/{object_id}/config',
            json.dumps(config), retain=True)

    def _undiscover(self, component, object_id):
        self.client.publish(
            f'{DISCOVERY_PREFIX}/{component}/{self.node}/{object_id}/config',
            '', retain=True)

    def _announce(self):
        self._discovery('binary_sensor', 'watering', {
            'name': 'Watering',
            'state_topic': f'{self.base}/watering',
            'json_attributes_topic': f'{self.base}/attributes',
            'device_class': 'moisture',
        })
        self._discovery('sensor', 'next_watering', {
            'name': 'Next watering',
            'state_topic': f'{self.base}/next_watering',
            'device_class': 'timestamp',
        })
        self._discovery('sensor', 'last_contact', {
            'name': 'Last hub contact',
            'state_topic': f'{self.base}/last_contact',
            'device_class': 'timestamp',
        })
        self._discovery('button', 'water_now', {
            'name': 'Water now',
            'command_topic': f'{self.base}/water_now/set',
        })
        self._discovery('button', 'stop', {
            'name': 'Stop watering',
            'command_topic': f'{self.base}/stop/set',
        })
        self._discovery('switch', 'schedule', {
            'name': 'Schedule enabled',
            'state_topic': f'{self.base}/schedule',
            'command_topic': f'{self.base}/schedule/set',
        })
        self._discovery('select', 'active_schedule', {
            'name': 'Active schedule',
            'state_topic': f'{self.base}/active_schedule',
            'command_topic': f'{self.base}/active_schedule/set',
            'options': [s.name for s in self.state.schedules],
        })
        # Attributes rather than a text entity: an MQTT text state caps at 255
        # characters, which a few events already exceed. This is also the hook a
        # custom card would read.
        self._discovery('sensor', 'schedule_detail', {
            'name': 'Schedule detail',
            'state_topic': f'{self.base}/active_schedule',
            'json_attributes_topic': f'{self.base}/schedule_detail',
        })
        self._discovery('number', 'pause_days', {
            'name': 'Pause for (days)',
            'state_topic': f'{self.base}/pause_days',
            'command_topic': f'{self.base}/pause_days/set',
            'min': 0, 'max': 14, 'step': 1, 'mode': 'box',
        })
        self._discovery('sensor', 'paused_until', {
            'name': 'Paused until',
            'state_topic': f'{self.base}/paused_until',
            'device_class': 'timestamp',
        })
        self._discovery('number', 'adjust_percent', {
            'name': 'Adjust (%)',
            'state_topic': f'{self.base}/adjust_percent',
            'command_topic': f'{self.base}/adjust_percent/set',
            'min': -50, 'max': 100, 'step': 5, 'mode': 'box',
        })
        self._discovery('number', 'adjust_days', {
            'name': 'Adjust for (days)',
            'state_topic': f'{self.base}/adjust_days',
            'command_topic': f'{self.base}/adjust_days/set',
            'min': 1, 'max': 14, 'step': 1, 'mode': 'box',
        })
        self._discovery('sensor', 'adjust_until', {
            'name': 'Adjust until',
            'state_topic': f'{self.base}/adjust_until',
            'device_class': 'timestamp',
        })

        for i in range(1, self.slots + 1):
            self._discovery('select', f'event{i}_mode', {
                'name': f'Event {i} when',
                'state_topic': f'{self.base}/event{i}_mode',
                'command_topic': f'{self.base}/event{i}_mode/set',
                'options': MODES,
            })
            self._discovery('text', f'event{i}_time', {
                'name': f'Event {i} time',
                'state_topic': f'{self.base}/event{i}_time',
                'command_topic': f'{self.base}/event{i}_time/set',
                'pattern': r'^([01]\d|2[0-3]):[0-5]\d$',
            })
            self._discovery('number', f'event{i}_duration', {
                'name': f'Event {i} duration',
                'state_topic': f'{self.base}/event{i}_duration',
                'command_topic': f'{self.base}/event{i}_duration/set',
                'unit_of_measurement': 'min',
                # A duration is one byte on the wire.
                'min': 0, 'max': 255, 'step': 1, 'mode': 'box',
            })
        for i in self.stale_slots:
            for component, name in (('select', f'event{i}_mode'),
                                    ('text', f'event{i}_time'),
                                    ('number', f'event{i}_duration')):
                self._undiscover(component, name)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info('connected to broker (%s)', reason_code)
        client.publish(self.availability, 'online', retain=True)
        self._announce()
        topics = ['water_now', 'stop', 'schedule', 'active_schedule',
                  'pause_days', 'adjust_percent', 'adjust_days']
        for i in range(1, self.slots + 1):
            topics += [f'event{i}_mode', f'event{i}_time', f'event{i}_duration']
        for topic in topics:
            client.subscribe(f'{self.base}/{topic}/set')
        self.publish_state()

    def _on_message(self, client, userdata, msg):
        topic = msg.topic.rsplit('/', 2)[-2]
        payload = msg.payload.decode().strip()
        log.info('command %s = %s', topic, payload)
        try:
            reason = self._apply(topic, payload)
        except Exception as e:
            reason = str(e)
        if reason:
            # The entity already shows the rejected value, so republish to snap
            # it back rather than leaving HA disagreeing with the hub.
            log.warning('rejected %s = %s: %s', topic, payload, reason)
            self.publish_state()

    def _apply(self, topic, payload):
        """-> a reason string if the command was refused."""
        if topic == 'water_now':
            self.state.water_now()
        elif topic == 'stop':
            self.state.stop_watering()
        elif topic == 'schedule':
            self.state.set_enabled(payload.upper() == 'ON')
        elif topic == 'active_schedule':
            return self.state.select_schedule(payload)
        elif topic == 'pause_days':
            return self.state.pause(float(payload))
        elif topic == 'adjust_percent':
            return self.state.adjust(int(float(payload)), self.adjust_days)
        elif topic == 'adjust_days':
            # Only meaningful alongside a percent, so store it and re-apply if
            # an adjustment is already running.
            self.adjust_days = max(1, int(float(payload)))
            if self.state.overlays.adjust_percent:
                return self.state.adjust(self.state.overlays.adjust_percent,
                                         self.adjust_days)
            self.publish_state()
        elif topic.startswith('event'):
            slot, _, field = topic[len('event'):].partition('_')
            slot = int(slot) - 1
            if field == 'mode':
                return self.state.set_event(slot, mode=payload)
            if field == 'time':
                return self.state.set_event(slot, at=payload)
            if field == 'duration':
                return self.state.set_event(slot, duration_min=int(float(payload)))
        return None

    def publish_state(self):
        s = self.state
        nxt = s.next_watering()
        ov = s.overlays
        events = s.events
        self.client.publish(f'{self.base}/active_schedule', s.active_schedule)
        self.client.publish(f'{self.base}/schedule_detail', json.dumps({
            'name': s.active_schedule,
            'events': [{'at': e.at, 'duration_min': e.duration_min,
                        'days': list(e.days), 'offset_min': e.offset_min}
                       for e in events],
        }))
        self.client.publish(f'{self.base}/pause_days',
                            self._days_left(ov.pause_until))
        self.client.publish(f'{self.base}/paused_until',
                            ov.pause_until.astimezone().isoformat()
                            if ov.pause_until else '')
        self.client.publish(f'{self.base}/adjust_percent', ov.adjust_percent)
        self.client.publish(f'{self.base}/adjust_days', self.adjust_days)
        self.client.publish(f'{self.base}/adjust_until',
                            ov.adjust_until.astimezone().isoformat()
                            if ov.adjust_until else '')
        for i in range(1, self.slots + 1):
            ev = events[i - 1] if i <= len(events) else None
            self.client.publish(f'{self.base}/event{i}_mode',
                                mode_of(ev) if ev else MODE_UNUSED)
            self.client.publish(
                f'{self.base}/event{i}_time',
                ev.at if ev and mode_of(ev) == MODE_CLOCK else '')
            self.client.publish(f'{self.base}/event{i}_duration',
                                ev.duration_min if ev else 0)
        self.client.publish(f'{self.base}/watering',
                            'ON' if s.watering != 'idle' else 'OFF')
        self.client.publish(f'{self.base}/attributes', json.dumps({
            'trigger': s.watering,
            'generation': f'{s.generation:04x}',
            'generation_held': (f'{s.generation_held:04x}'
                                if s.generation_held is not None else None),
            'pending_command': s.pending,
        }))
        self.client.publish(f'{self.base}/next_watering',
                            nxt.astimezone().isoformat() if nxt else '')
        self.client.publish(f'{self.base}/last_contact',
                            s.last_seen.astimezone().isoformat() if s.last_seen else '')
        self.client.publish(f'{self.base}/schedule',
                            'ON' if s.schedule_enabled else 'OFF')

    def start(self):
        # Async connect with auto-retry: a broker outage must never stop the
        # server answering the hub, or the garden stops being watered.
        self.client.connect_async(self.host, self.port)
        self.client.loop_start()

    def stop(self):
        self.client.publish(self.availability, 'offline', retain=True)
        self.client.loop_stop()
        self.client.disconnect()
