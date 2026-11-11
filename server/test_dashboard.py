"""Check the generated card against what the bridge actually announces.

Run with the venv interpreter: uv run python test_dashboard.py

The point of generating the card is that it cannot drift from the entities.
That only holds if something checks, so this walks every entity id the card
names and asserts the bridge publishes a discovery config for it.
"""
import json
import sys

import yaml

from hozelock import dashboard, mqtt_bridge, schedule
from hozelock import state as state_mod

from test_mqtt import FakeClient


def check(name, got, want):
    if got == want:
        print(f'  PASS  {name}')
        return True
    print(f'  FAIL  {name}\n        got  {got!r}\n        want {want!r}')
    return False


def announced(state, node='hozelock'):
    """-> the entity ids the bridge would create, as HA derives them."""
    mqtt_bridge.mqtt.Client = FakeClient
    bridge = mqtt_bridge.Bridge(state, host='127.0.0.1', node_id=node)
    bridge._on_connect(bridge.client, None, None, 'ok')
    out = set()
    for topic, payload in bridge.client.published.items():
        if not topic.startswith('homeassistant/') or not payload:
            continue
        component = topic.split('/')[1]
        out.add(f'{component}.{json.loads(payload)["object_id"]}')
    return out


def main():
    st = state_mod.HubState(
        'ge7si1', schedule.Site(51.5, -0.1),
        schedules=[
            schedule.Schedule('Summer', [schedule.Event('sunrise', 45),
                                         schedule.Event('21:00', 60)]),
            schedule.Schedule('Winter', [schedule.Event('10:00', 20)]),
        ],
        active_schedule='Summer')
    ok = True

    cards = dashboard.generate()
    text = yaml.safe_dump(cards)
    ok &= check('the generated YAML parses', bool(yaml.safe_load(text)), True)

    have = announced(st)
    want = dashboard.entity_ids(cards)
    ok &= check('every entity the card names is announced by the bridge',
                sorted(want - have), [])
    ok &= check('the card offers water and stop',
                {'button.hozelock_water_now', 'button.hozelock_stop'} <= want, True)
    ok &= check('it shows which schedule is running',
                'sensor.hozelock_active_schedule' in want, True)
    ok &= check('it names no editing entity',
                sorted(e for e in want
                       if 'event' in e or 'pause' in e or 'adjust' in e
                       or e.startswith('select.')), [])

    print()
    print('  -- the card follows node_id, not a constant --')
    cards = dashboard.generate('garden')
    want = dashboard.entity_ids(cards)
    ok &= check('entity ids take the configured node',
                'button.garden_water_now' in want, True)
    ok &= check('  and all of them are announced',
                sorted(want - announced(st, 'garden')), [])

    print('\nOK' if ok else '\nFAILURES')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
