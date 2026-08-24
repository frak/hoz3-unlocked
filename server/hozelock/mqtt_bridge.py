"""Home Assistant bridge over MQTT discovery.

Entities appear in HA automatically; no custom component and no YAML.
"""
import json
import logging

import paho.mqtt.client as mqtt

log = logging.getLogger('hozelock.mqtt')

DISCOVERY_PREFIX = 'homeassistant'


class Bridge:
    def __init__(self, state, host, port=1883, username=None, password=None,
                 node_id='hozelock'):
        self.state = state
        self.node = node_id
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
        state.on_change(lambda _: self.publish_state())

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
            'availability_topic': self.availability,
        })
        self.client.publish(
            f'{DISCOVERY_PREFIX}/{component}/{self.node}/{object_id}/config',
            json.dumps(config), retain=True)

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

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info('connected to broker (%s)', reason_code)
        client.publish(self.availability, 'online', retain=True)
        self._announce()
        for topic in ('water_now', 'stop', 'schedule'):
            client.subscribe(f'{self.base}/{topic}/set')
        self.publish_state()

    def _on_message(self, client, userdata, msg):
        topic = msg.topic.rsplit('/', 2)[-2]
        payload = msg.payload.decode().strip()
        log.info('command %s = %s', topic, payload)
        if topic == 'water_now':
            self.state.water_now()
        elif topic == 'stop':
            self.state.stop_watering()
        elif topic == 'schedule':
            self.state.set_enabled(payload.upper() == 'ON')

    def publish_state(self):
        s = self.state
        nxt = s.next_watering()
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
