"""Durable state: which schedule is active, its events, and any overlay.

Once schedules can be edited from Home Assistant, config.yaml is only a seed --
this file is the source of truth from the first run onward. Delete it to
re-seed from config.
"""
import json
import logging
import os
import tempfile
from datetime import datetime

from .schedule import Event, Overlays, Schedule

log = logging.getLogger('hozelock.persist')


def _iso(dt):
    return dt.isoformat() if dt else None


def _dt(text):
    return datetime.fromisoformat(text) if text else None


def snapshot(state):
    return {
        'active_schedule': state.active_schedule,
        'schedule_enabled': state.schedule_enabled,
        'schedules': [
            {'name': s.name,
             'events': [{'at': e.at, 'duration_min': e.duration_min,
                         'days': list(e.days), 'offset_min': e.offset_min}
                        for e in s.events]}
            for s in state.schedules
        ],
        'overlays': {
            'pause_until': _iso(state.overlays.pause_until),
            'adjust_percent': state.overlays.adjust_percent,
            'adjust_until': _iso(state.overlays.adjust_until),
        },
    }


def parse(data):
    """-> (schedules, active_schedule, schedule_enabled, overlays)."""
    schedules = [
        Schedule(name=s['name'],
                 events=[Event(**e) for e in s.get('events', [])])
        for s in data.get('schedules', [])
    ]
    ov = data.get('overlays') or {}
    return (schedules,
            data.get('active_schedule'),
            data.get('schedule_enabled', True),
            Overlays(pause_until=_dt(ov.get('pause_until')),
                     adjust_percent=ov.get('adjust_percent', 0),
                     adjust_until=_dt(ov.get('adjust_until'))))


def load(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return parse(json.load(f))
    except Exception:
        # Refusing to start would leave the garden unwatered over a bad file,
        # so fall back to the config seed and say so loudly.
        log.exception('%s is unreadable - falling back to config.yaml. Move it '
                      'aside to stop this recurring.', path)
        return None


def save(path, state):
    if not path:
        return
    data = json.dumps(snapshot(state), indent=1)
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    try:
        # Rename rather than write in place: a crash mid-write would otherwise
        # leave truncated JSON and lose every schedule.
        fd, tmp = tempfile.mkstemp(dir=directory, prefix='.state-', suffix='.json')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except Exception:
        log.exception('could not write %s - edits will not survive a restart', path)
