"""Check the Home Assistant discovery payloads without needing a broker.

Verifies the topics and configs HA relies on to create entities, and that
commands coming back the other way reach the hub state.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

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
        'homeassistant/button/hozelock/water_now/config',
        'homeassistant/button/hozelock/stop/config',
        'homeassistant/switch/hozelock/schedule/config',
        'homeassistant/select/hozelock/active_schedule/config',
        'homeassistant/sensor/hozelock/schedule_detail/config',
        'homeassistant/number/hozelock/pause_days/config',
        'homeassistant/sensor/hozelock/paused_until/config',
        'homeassistant/number/hozelock/adjust_percent/config',
        'homeassistant/number/hozelock/adjust_days/config',
        'homeassistant/sensor/hozelock/adjust_until/config',
        'homeassistant/select/hozelock/event1_mode/config',
        'homeassistant/text/hozelock/event1_time/config',
        'homeassistant/number/hozelock/event1_duration/config',
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

    ok &= check('every command topic is subscribed',
                {f'hozelock/hozelock/{t}/set' for t in (
                    'water_now', 'stop', 'schedule', 'active_schedule',
                    'pause_days', 'adjust_percent', 'adjust_days',
                    'event1_mode', 'event1_time', 'event1_duration')}
                <= set(c.subscribed), True)
    # Every entity a command can arrive on must also publish its state back, or
    # the card shows a blank control that silently works.
    commanded = {t.rsplit('/', 2)[-2] for t in c.subscribed}
    published = {t.rsplit('/', 1)[-1] for t in c.published
                 if t.startswith('hozelock/hozelock/')}
    ok &= check('every commandable entity publishes state',
                sorted(commanded - published - {'water_now', 'stop'}), [])
    ok &= check('announced online', c.published['hozelock/hozelock/available'], 'online')

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

    ok &= check('object_id pins the entity id against a device rename',
                json.loads(c.published[
                    'homeassistant/select/hozelock/active_schedule/config'
                ])['object_id'], 'hozelock_active_schedule')

    sel = json.loads(c.published[
        'homeassistant/select/hozelock/active_schedule/config'])
    ok &= check('the picker offers every schedule', sel['options'],
                ['Summer', 'Winter'])
    dur = json.loads(c.published[
        'homeassistant/number/hozelock/event1_duration/config'])
    ok &= check('duration is capped at one byte', (dur['min'], dur['max']), (0, 255))

    print()
    print('  -- switching repaints the slots --')
    ok &= check('slot 1 shows the active schedule',
                c.published['hozelock/hozelock/event1_duration'], 30)
    ok &= check('a solar slot publishes no clock time',
                c.published['hozelock/hozelock/event2_time'], '')
    ok &= check('an empty slot reads Unused',
                c.published['hozelock/hozelock/event3_mode'],
                mqtt_bridge.MODE_UNUSED)

    bridge._on_message(c, None, FakeMessage(
        'hozelock/hozelock/active_schedule/set', 'Winter'))
    ok &= check('switching schedule changes the active sensor',
                c.published['hozelock/hozelock/active_schedule'], 'Winter')
    ok &= check('  and repaints slot 1',
                c.published['hozelock/hozelock/event1_duration'], 20)
    ok &= check('  and blanks the slot the new schedule lacks',
                c.published['hozelock/hozelock/event2_mode'],
                mqtt_bridge.MODE_UNUSED)

    detail = json.loads(c.published['hozelock/hozelock/schedule_detail'])
    ok &= check('schedule_detail carries the whole schedule',
                (detail['name'], len(detail['events'])), ('Winter', 1))

    print()
    print('  -- edits and overlays --')
    bridge._on_message(c, None, FakeMessage(
        'hozelock/hozelock/event1_duration/set', '35'))
    ok &= check('a duration edit reaches the schedule',
                st.events[0].duration_min, 35)
    bridge._on_message(c, None, FakeMessage(
        'hozelock/hozelock/event1_mode/set', 'Sunrise'))
    ok &= check('a mode edit reaches the schedule', st.events[0].at, 'sunrise')

    before = st.generation
    bridge._on_message(c, None, FakeMessage('hozelock/hozelock/pause_days/set', '2'))
    ok &= check('pause sets an expiry', bool(st.overlays.pause_until), True)
    ok &= check('  and bumps so the hub refetches', st.generation != before, True)
    bridge._on_message(c, None, FakeMessage('hozelock/hozelock/pause_days/set', '0'))
    ok &= check('pause 0 cancels', st.overlays.pause_until, None)

    bridge._on_message(c, None, FakeMessage('hozelock/hozelock/adjust_days/set', '3'))
    bridge._on_message(c, None, FakeMessage(
        'hozelock/hozelock/adjust_percent/set', '50'))
    ok &= check('adjust records the percent', st.overlays.adjust_percent, 50)
    ok &= check('  over the configured days',
                round((st.overlays.adjust_until - datetime.now()).days), 2)

    print()
    print('  -- a refused command must not leave HA out of step --')
    st.set_event(0, duration_min=40)
    bridge._on_message(c, None, FakeMessage(
        'hozelock/hozelock/event1_duration/set', '900'))
    ok &= check('an impossible duration is refused', st.events[0].duration_min, 40)
    ok &= check('  and state is republished to snap the card back',
                c.published['hozelock/hozelock/event1_duration'], 40)
    bridge._on_message(c, None, FakeMessage(
        'hozelock/hozelock/active_schedule/set', 'Nope'))
    ok &= check('an unknown schedule is refused', st.active_schedule, 'Winter')

    print()
    print('  -- slots auto-size to the longest schedule --')
    big = state_mod.HubState(
        'x', schedule.Site(51.5, -0.1),
        schedules=[schedule.Schedule('Many', [schedule.Event(f'0{h}:00', 5)
                                              for h in range(1, 7)])])
    b2 = mqtt_bridge.Bridge(big, host='127.0.0.1')
    ok &= check('six events get six slots, not four', b2.slots, 6)
    b2._on_connect(b2.client, None, None, 'ok')
    ok &= check('  every event is editable',
                'homeassistant/number/hozelock/event6_duration/config'
                in b2.client.published, True)
    ok &= check('  and removed slots are cleared from HA',
                b2.client.published[
                    'homeassistant/number/hozelock/event7_duration/config'], '')

    print('\nOK' if ok else '\nFAILURES')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
