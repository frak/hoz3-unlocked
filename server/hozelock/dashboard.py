"""Generate the Home Assistant Lovelace card.

    uv run python -m hozelock.dashboard config.yaml

Generated rather than pasted from the docs so the entity ids follow the
configured node_id -- a hand-written card that disagrees with it shows nothing
but "Entity not available", which looks like a broker problem.

Core card types only, so there is nothing to install.
"""
import sys

import yaml


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
                 {'entity': f'sensor.{e("active_schedule")}', 'name': 'Schedule'},
                 {'entity': f'switch.{e("schedule")}', 'name': 'Enabled'},
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
        ],
    }


def generate(node='hozelock'):
    return {'status': status_card(node)}


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
    cfg, _ = load(path)
    node = (cfg.get('mqtt') or {}).get('node_id', 'hozelock')

    cards = generate(node)
    print('# Home Assistant > dashboard > Edit > Add card > Manual.')
    for name, card in cards.items():
        print(f'\n# ---- {name} card ----')
        print(yaml.safe_dump(card, sort_keys=False, default_flow_style=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
