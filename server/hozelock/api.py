"""JSON control API, mounted under /api/ on the hub's listener.

Read-only but for the two commands and the schedule switch: schedules come from
config.yaml, so there is nothing here to edit them with.

This and the MQTT bridge are both adapters over HubState; neither owns any
behaviour. Unlike the bridge, this keeps working when the broker is down, which
is the difference between a stuck tap and a stoppable one.

Kept transport-free -- handle() takes and returns plain values, so the routes
are testable without a socket.
"""
import json
import logging

from .state import COMMAND_NAMES

log = logging.getLogger('hozelock.api')

PREFIX = '/api/'

JSON_TYPE = 'application/json'
MUTATING = ('POST', 'PUT', 'PATCH', 'DELETE')


def _iso(dt):
    return dt.isoformat() if dt else None


def _schedule_dict(s):
    return {'name': s.name,
            'events': [{'at': e.at, 'duration_min': e.duration_min,
                        'days': list(e.days), 'offset_min': e.offset_min}
                       for e in s.events]}


def _state_dict(state):
    return {
        'hub_id': state.hub_id,
        'watering': state.watering,
        'next_watering': _iso(state.next_watering()),
        'last_contact': _iso(state.last_seen),
        'generation': f'{state.generation:04x}',
        'generation_held': (f'{state.generation_held:04x}'
                            if state.generation_held is not None else None),
        'pending_command': COMMAND_NAMES.get(state.pending),
        'link': state.link.hex(' ') if state.link else None,
        'resyncing': state.resyncing,
        'controller_seen': state.controller_seen,
        'schedule_enabled': state.schedule_enabled,
        'active_schedule': state.active_schedule,
        'schedules': [s.name for s in state.schedules],
    }


def handle(state, method, path, content_type=None, body=b''):
    """-> (status, payload). payload is JSON-serialisable, or None for 404."""
    if not path.startswith(PREFIX):
        return None
    route = path[len(PREFIX):].rstrip('/')

    if method in MUTATING:
        # A cross-origin form POST is a "simple request" -- delivered, and acted
        # on, even though the browser cannot read the reply. Demanding JSON
        # forces a preflight that no route answers. This is the CSRF defence,
        # not fussiness.
        if (content_type or '').split(';')[0].strip().lower() != JSON_TYPE:
            return 415, {'error': f'{method} requires Content-Type: {JSON_TYPE}'}
        try:
            payload = json.loads(body or b'{}')
        except ValueError as e:
            return 400, {'error': f'invalid JSON: {e}'}
        if not isinstance(payload, dict):
            return 400, {'error': 'body must be a JSON object'}
    else:
        payload = {}

    try:
        return _dispatch(state, method, route, payload)
    except ValueError as e:
        return 400, {'error': str(e)}
    except Exception:
        log.exception('unhandled error on %s %s', method, path)
        return 500, {'error': 'internal error'}


def _dispatch(state, method, route, payload):
    if method == 'GET' and route == 'state':
        return 200, _state_dict(state)

    if method == 'GET' and route == 'schedules':
        return 200, {'active': state.active_schedule,
                     'schedules': [_schedule_dict(s) for s in state.schedules]}

    if method == 'PUT' and route == 'enabled':
        if 'enabled' not in payload:
            return 400, {'error': 'expected {"enabled": true|false}'}
        state.set_enabled(bool(payload['enabled']))
        return 200, _state_dict(state)

    if method == 'POST' and route == 'water':
        state.water_now()
        return 200, _state_dict(state)

    if method == 'POST' and route == 'stop':
        state.stop_watering()
        return 200, _state_dict(state)

    return 404, {'error': f'no route {method} {PREFIX}{route}'}
