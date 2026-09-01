"""Generate the Home Assistant Lovelace cards.

    uv run python -m hozelock.dashboard config.yaml

Generated rather than pasted from the docs because the slot count follows the
longest schedule -- a hand-written card goes stale the moment one grows, and the
failure is silent: the extra waterings simply have no controls.

Core card types only, so there is nothing to install.
"""
import sys

import yaml

from . import mqtt_bridge


def _entity(node, name):
    return f'{node}_{name}'


def status_card(node='hozelock'):
    e = lambda n: _entity(node, n)  # noqa: E731
    return {
        'type': 'vertical-stack',
        'cards': [
            {'type': 'entities',
             'title': 'Hozelock',
             'entities': [
                 {'entity': f'binary_sensor.{e("watering")}', 'name': 'Watering'},
                 {'entity': f'sensor.{e("next_watering")}', 'name': 'Next watering'},
                 {'entity': f'sensor.{e("last_contact")}', 'name': 'Hub last seen'},
             ]},
            {'type': 'horizontal-stack',
             'cards': [
                 {'type': 'button', 'name': 'Water now', 'icon': 'mdi:watering-can',
                  'tap_action': {'action': 'toggle'},
                  'entity': f'button.{e("water_now")}'},
                 {'type': 'button', 'name': 'Stop', 'icon': 'mdi:stop',
                  'tap_action': {'action': 'toggle'},
                  'entity': f'button.{e("stop")}'},
             ]},
            {'type': 'entities',
             'title': 'Temporarily',
             'entities': [
                 {'entity': f'number.{e("pause_days")}', 'name': 'Pause for (days)'},
                 {'type': 'conditional',
                  'conditions': [{'entity': f'sensor.{e("paused_until")}',
                                  'state_not': 'unknown'}],
                  'row': {'entity': f'sensor.{e("paused_until")}',
                          'name': 'Paused until'}},
                 {'entity': f'number.{e("adjust_percent")}', 'name': 'Adjust (%)'},
                 {'entity': f'number.{e("adjust_days")}', 'name': 'Adjust for (days)'},
                 {'type': 'conditional',
                  'conditions': [{'entity': f'sensor.{e("adjust_until")}',
                                  'state_not': 'unknown'}],
                  'row': {'entity': f'sensor.{e("adjust_until")}',
                          'name': 'Adjust until'}},
             ]},
        ],
    }


def schedule_card(slots, node='hozelock'):
    e = lambda n: _entity(node, n)  # noqa: E731
    cards = [
        {'type': 'entities',
         'title': 'Schedule',
         'entities': [
             {'entity': f'select.{e("active_schedule")}', 'name': 'Active schedule'},
             {'entity': f'switch.{e("schedule")}', 'name': 'Enabled'},
         ]},
    ]
    for i in range(1, slots + 1):
        mode = f'select.{e(f"event{i}_mode")}'
        cards.append({
            'type': 'conditional',
            # An unused slot is real but empty; showing it would imply the
            # schedule has more waterings than it does.
            'conditions': [{'entity': mode,
                            'state_not': mqtt_bridge.MODE_UNUSED}],
            'card': {
                'type': 'entities',
                'title': f'Event {i}',
                'entities': [
                    {'entity': mode, 'name': 'When'},
                    {'type': 'conditional',
                     'conditions': [{'entity': mode,
                                     'state': mqtt_bridge.MODE_CLOCK}],
                     'row': {'entity': f'text.{e(f"event{i}_time")}',
                             'name': 'Time'}},
                    {'entity': f'number.{e(f"event{i}_duration")}',
                     'name': 'Duration (min)'},
                ],
            },
        })
    return {'type': 'vertical-stack', 'cards': cards}


def generate(slots, node='hozelock'):
    return {'status': status_card(node), 'schedule': schedule_card(slots, node)}


def entity_ids(cards):
    """Every entity the cards name, for checking the bridge announces them all."""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get('entity'), str):
                found.add(node['entity'])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(cards)
    return found


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else 'config.yaml'
    from .__main__ import load
    cfg, st = load(path)
    node = (cfg.get('mqtt') or {}).get('node_id', 'hozelock')
    slots = mqtt_bridge.slot_count(
        st, cfg.get('max_event_slots', mqtt_bridge.DEFAULT_SLOTS))

    cards = generate(slots, node)
    print('# Home Assistant > dashboard > Edit > Add card > Manual.')
    print(f'# Two cards, for {slots} event slots. Regenerate after adding events.')
    for name, card in cards.items():
        print(f'\n# ---- {name} card ----')
        print(yaml.safe_dump(card, sort_keys=False, default_flow_style=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
