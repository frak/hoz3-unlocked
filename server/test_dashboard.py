"""Check the generated cards against what the bridge actually announces.

Run with the venv interpreter: uv run python test_dashboard.py

The point of generating the cards is that they cannot drift from the entities.
That only holds if something checks, so this walks every entity id the cards
name and asserts the bridge publishes a discovery config for it.
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


def announced(state, slots):
    """-> the entity ids the bridge would create, as HA derives them."""
    mqtt_bridge.mqtt.Client = FakeClient
    bridge = mqtt_bridge.Bridge(state, host='127.0.0.1', max_event_slots=slots)
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

    slots = mqtt_bridge.slot_count(st)
    cards = dashboard.generate(slots)

    text = yaml.safe_dump(cards)
    ok &= check('the generated YAML parses', bool(yaml.safe_load(text)), True)

    have = announced(st, slots)
    want = dashboard.entity_ids(cards)
    ok &= check('every entity the cards name is announced by the bridge',
                sorted(want - have), [])

    ok &= check('both cards are produced', sorted(cards), ['schedule', 'status'])
    ok &= check('the status card offers water and stop',
                {'button.hozelock_water_now', 'button.hozelock_stop'} <=
                dashboard.entity_ids(cards['status']), True)
    ok &= check('the status card carries the overlay controls',
                {'number.hozelock_pause_days', 'number.hozelock_adjust_percent'} <=
                dashboard.entity_ids(cards['status']), True)
    ok &= check('the schedule card carries the picker',
                'select.hozelock_active_schedule' in
                dashboard.entity_ids(cards['schedule']), True)

    # A slot with no event must not render, or the card implies waterings that
    # do not exist.
    slot_cards = cards['schedule']['cards'][1:]
    ok &= check('every event slot is conditional on not being Unused',
                all(c['type'] == 'conditional'
                    and c['conditions'][0]['state_not'] == mqtt_bridge.MODE_UNUSED
                    for c in slot_cards), True)
    rows = slot_cards[0]['card']['entities']
    time_row = next(r for r in rows if isinstance(r, dict)
                    and r.get('type') == 'conditional')
    ok &= check('the time row only shows in Clock mode',
                time_row['conditions'][0]['state'], mqtt_bridge.MODE_CLOCK)

    print()
    print('  -- the card follows the schedules, not a constant --')
    big = state_mod.HubState(
        'x', schedule.Site(51.5, -0.1),
        schedules=[schedule.Schedule('Many', [schedule.Event(f'0{h}:00', 5)
                                              for h in range(1, 7)])])
    n = mqtt_bridge.slot_count(big)
    big_cards = dashboard.generate(n)
    ok &= check('six events produce six slots', n, 6)
    ok &= check('  and six slot cards', len(big_cards['schedule']['cards']) - 1, 6)
    ok &= check('  all of them announced',
                sorted(dashboard.entity_ids(big_cards) - announced(big, n)), [])

    print('\nOK' if ok else '\nFAILURES')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
