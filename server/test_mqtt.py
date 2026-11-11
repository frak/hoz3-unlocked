"""Check the Home Assistant discovery payloads without needing a broker.

Verifies the topics and configs HA relies on to create entities, and that the
two commands coming back the other way reach the hub state.
"""
import json
import sys

from hozelock import mqtt_bridge, schedule
from hozelock import state as state_mod


class FakeClient:
    def __init__(self, *a, **k):
        self.published = {}
        self.subscribed = []
        self.will = None

    def username_pw_set(self, *a):
        pass

    def will_set(self, topic, payload, retain=False):
        self.will = (topic, payload)

    def publish(self, topic, payload=None, retain=False):
        self.published[topic] = payload

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def connect_async(self, *a):
        pass

    def loop_start(self):
        pass


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


def check(name, got, want):
    if got == want:
        print(f'  PASS  {name}')
        return True
    print(f'  FAIL  {name}\n        got  {got!r}\n        want {want!r}')
    return False


def main():
    mqtt_bridge.mqtt.Client = FakeClient
    st = state_mod.HubState(
        hub_id='ge7si1',
        site=schedule.Site(latitude=51.5, longitude=-0.1),
        schedules=[
            schedule.Schedule('Summer', [schedule.Event('06:00', 30),
                                         schedule.Event('sunset', 45)]),
            schedule.Schedule('Winter', [schedule.Event('10:00', 20)]),
        ],
        active_schedule='Summer')
    bridge = mqtt_bridge.Bridge(st, host='127.0.0.1')
    c = bridge.client
    ok = True

    ok &= check('LWT set so HA marks entities unavailable if we die',
                c.will, ('hozelock/hozelock/available', 'offline'))

    bridge._on_connect(c, None, None, 'ok')

    expected = {
        'homeassistant/binary_sensor/hozelock/watering/config',
        'homeassistant/sensor/hozelock/next_watering/config',
        'homeassistant/sensor/hozelock/last_contact/config',
        'homeassistant/sensor/hozelock/active_schedule/config',
        'homeassistant/button/hozelock/water_now/config',
        'homeassistant/button/hozelock/stop/config',
        'homeassistant/switch/hozelock/schedule/config',
    }
    ok &= check('all discovery topics published',
                expected <= set(c.published), True)

    cfg = json.loads(c.published['homeassistant/button/hozelock/water_now/config'])
    ok &= check('water_now has a command topic',
                cfg['command_topic'], 'hozelock/hozelock/water_now/set')
    ok &= check('entities carry a unique_id so HA can register them',
                cfg['unique_id'], 'hozelock_water_now')
    ok &= check('entities share one device', cfg['device']['identifiers'], ['hozelock'])
    ok &= check('availability wired to the LWT topic',
                cfg['availability_topic'], 'hozelock/hozelock/available')
    ok &= check('object_id pins the entity id against a device rename',
                cfg['object_id'], 'hozelock_water_now')

    ok &= check('every command topic is subscribed',
                {f'hozelock/hozelock/{t}/set'
                 for t in ('water_now', 'stop', 'schedule')}
                <= set(c.subscribed), True)
    # Every entity a command can arrive on must also publish its state back, or
    # the card shows a blank control that silently works.
    commanded = {t.rsplit('/', 2)[-2] for t in c.subscribed}
    published = {t.rsplit('/', 1)[-1] for t in c.published
                 if t.startswith('hozelock/hozelock/')}
    ok &= check('every commandable entity publishes state',
                sorted(commanded - published - {'water_now', 'stop'}), [])
    ok &= check('announced online', c.published['hozelock/hozelock/available'], 'online')
    ok &= check('the active schedule is reported, read-only',
                c.published['hozelock/hozelock/active_schedule'], 'Summer')
    ok &= check('  with no command topic to edit it with',
                'hozelock/hozelock/active_schedule/set' in c.subscribed, False)

    print()
    print('  -- commands reach the hub --')
    # A command from HA must reach the hub, which means bumping the generation.
    before = st.generation
    bridge._on_message(c, None, FakeMessage('hozelock/hozelock/water_now/set', 'PRESS'))
    ok &= check('water_now command bumps the generation', st.generation, before + 1)
    ok &= check('water_now sets the pending command', st.pending, state_mod.CMD_WATER)

    bridge._on_message(c, None, FakeMessage('hozelock/hozelock/stop/set', 'PRESS'))
    ok &= check('stop command sets the pending command', st.pending, state_mod.CMD_STOP)

    bridge._on_message(c, None, FakeMessage('hozelock/hozelock/schedule/set', 'OFF'))
    ok &= check('schedule switch disables the programme', st.schedule_enabled, False)
    ok &= check('state republished after a command',
                c.published['hozelock/hozelock/schedule'], 'OFF')

    print()
    print('  -- the retired editing entities are cleared from HA --')
    # Retained configs outlive the code that published them, so an install
    # upgraded from the editable bridge must have them explicitly emptied.
    for component, object_id in (('select', 'active_schedule'),
                                 ('number', 'pause_days'),
                                 ('select', 'event1_mode'),
                                 ('number', 'event12_duration')):
        topic = f'homeassistant/{component}/hozelock/{object_id}/config'
        ok &= check(f'{component}.{object_id} retired',
                    c.published.get(topic), '')

    print('\nOK' if ok else '\nFAILURES')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
