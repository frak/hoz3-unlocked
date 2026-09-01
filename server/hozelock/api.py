"""JSON control API, mounted under /api/ on the hub's listener.

This and the MQTT bridge are both adapters over HubState; neither owns any
behaviour. Unlike the bridge, this keeps working when the broker is down, which
is the difference between a stuck tap and a stoppable one.

Kept transport-free -- handle() takes and returns plain values, so the routes
are testable without a socket.
"""
import json
import logging

from .schedule import DAYS, Event
from .state import COMMAND_NAMES

log = logging.getLogger('hozelock.api')

PREFIX = '/api/'

JSON_TYPE = 'application/json'
MUTATING = ('POST', 'PUT', 'PATCH', 'DELETE')

EVENT_KEYS = {'at', 'duration_min', 'days', 'offset_min'}


def _iso(dt):
    return dt.isoformat() if dt else None


def _schedule_dict(s):
    return {'name': s.name,
            'events': [{'at': e.at, 'duration_min': e.duration_min,
                        'days': list(e.days), 'offset_min': e.offset_min}
                       for e in s.events]}


def _state_dict(state):
    ov = state.overlays
    return {
        'hub_id': state.hub_id,
        'watering': state.watering,
        'next_watering': _iso(state.next_watering()),
        'last_contact': _iso(state.last_seen),
        'generation': f'{state.generation:04x}',
        'generation_held': (f'{state.generation_held:04x}'
                            if state.generation_held is not None else None),
        'pending_command': COMMAND_NAMES.get(state.pending),
        'schedule_enabled': state.schedule_enabled,
        'active_schedule': state.active_schedule,
        'schedules': [s.name for s in state.schedules],
        'overlays': {
            'paused_until': _iso(ov.pause_until),
            'adjust_percent': ov.adjust_percent,
            'adjust_until': _iso(ov.adjust_until),
        },
    }


def _events_from(payload):
    """-> [Event]. Raises ValueError with something a human can act on."""
    if not isinstance(payload, list):
        raise ValueError('events must be a list')
    out = []
    for i, e in enumerate(payload):
        if not isinstance(e, dict):
            raise ValueError(f'event {i} must be an object')
        unknown = set(e) - EVENT_KEYS
        if unknown:
            raise ValueError(f'event {i} has unknown keys {sorted(unknown)}; '
                             f'expected {sorted(EVENT_KEYS)}')
        if 'at' not in e or 'duration_min' not in e:
            raise ValueError(f'event {i} needs at and duration_min')
        days = e.get('days', list(DAYS))
        bad = [d for d in days if d not in DAYS]
        if bad:
            raise ValueError(f'event {i} has unknown days {bad}; expected {DAYS}')
        out.append(Event(at=str(e['at']), duration_min=int(e['duration_min']),
                         days=list(days), offset_min=int(e.get('offset_min', 0))))
    return out


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

    if method == 'PUT' and route == 'schedules/active':
        name = payload.get('name')
        if not name:
            return 400, {'error': 'expected {"name": ...}'}
        reason = state.select_schedule(name)
        if reason:
            return (404 if 'no schedule named' in reason else 400), {'error': reason}
        return 200, _state_dict(state)

    if method == 'PUT' and route.startswith('schedules/'):
        name = route[len('schedules/'):]
        target = state._find(name)
        if not target:
            return 404, {'error': f'no schedule named {name!r}'}
        events = _events_from(payload.get('events', []))
        if name != state.active_schedule:
            # Only the active schedule reaches the hub, so an inactive one needs
            # no trial encode -- it gets validated when it is selected.
            target.events = events
            state._persist()
            state._notify()
            return 200, _schedule_dict(target)
        reason = state.set_schedule(events)
        if reason:
            return 400, {'error': reason}
        return 200, _schedule_dict(target)

    if method == 'PUT' and route == 'enabled':
        if 'enabled' not in payload:
            return 400, {'error': 'expected {"enabled": true|false}'}
        state.set_enabled(bool(payload['enabled']))
        return 200, _state_dict(state)

    if method == 'PUT' and route == 'pause':
        if 'days' not in payload:
            return 400, {'error': 'expected {"days": n}; 0 cancels'}
        reason = state.pause(payload['days'])
        return (400, {'error': reason}) if reason else (200, _state_dict(state))

    if method == 'PUT' and route == 'adjust':
        if 'percent' not in payload:
            return 400, {'error': 'expected {"percent": n, "days": n}; 0 cancels'}
        reason = state.adjust(payload['percent'], payload.get('days', 1))
        return (400, {'error': reason}) if reason else (200, _state_dict(state))

    if method == 'POST' and route == 'water':
        state.water_now()
        return 200, _state_dict(state)

    if method == 'POST' and route == 'stop':
        state.stop_watering()
        return 200, _state_dict(state)

    return 404, {'error': f'no route {method} {PREFIX}{route}'}
