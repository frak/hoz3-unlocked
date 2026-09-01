"""Entry point: serve the hub, bridge to Home Assistant.

    hozelock-server config.yaml
"""
import logging
import os
import sys
import threading
import time
from datetime import datetime

import yaml

from . import mqtt_bridge, persist, schedule, server
from . import state as state_mod

log = logging.getLogger('hozelock')

# Solar times drift daily, so the programme must be regenerated and the
# generation bumped or the hub keeps watering to yesterday's sunrise.
REFRESH_INTERVAL_S = 3600


def schedules_from_config(cfg):
    """Config's `schedules:`, or the older single `schedule:` wrapped as one."""
    if cfg.get('schedules'):
        return [schedule.Schedule(name=s['name'],
                                  events=[schedule.Event(**e)
                                          for e in s.get('events', [])])
                for s in cfg['schedules']]
    return [schedule.Schedule(name='Default',
                              events=[schedule.Event(**e)
                                      for e in cfg.get('schedule', [])])]


def load(path):
    cfg = yaml.safe_load(open(path))
    site = schedule.Site(**cfg['site'])

    state_file = cfg.get('state_file', 'state.json')
    if state_file and not os.path.isabs(state_file):
        state_file = os.path.join(os.path.dirname(os.path.abspath(path)), state_file)

    seeded = persist.load(state_file)
    if seeded:
        schedules, active, enabled, overlays = seeded
        # Edits made in HA live here, not in config.yaml. Saying so is the
        # difference between "my config change did nothing" and a bug report.
        log.info('schedules loaded from %s (config.yaml `schedules:` ignored; '
                 'delete the file to re-seed from config)', state_file)
    else:
        schedules = schedules_from_config(cfg)
        active, enabled, overlays = cfg.get('active_schedule'), True, None
        log.info('seeding schedules from %s', path)

    st = state_mod.HubState(
        hub_id=cfg['hub_id'],
        site=site,
        schedules=schedules,
        active_schedule=active,
        overlays=overlays,
        state_file=state_file,
        restrict_generations=cfg.get('restrict_generations', False),
        initial_generation=cfg.get('initial_generation'),
        corrupt_checksum=cfg.get('corrupt_checksum', False),
        replay_blob=cfg.get('replay_blob'),
        demo_mode=cfg.get('demo_mode', False),
    )
    st.schedule_enabled = enabled
    if not seeded:
        st._persist()
    return cfg, st


def refresher(st):
    last = None
    while True:
        # An overlay lapses at an arbitrary time, and the hub only refetches when
        # the generation moves -- so sleep to the expiry rather than past it.
        now = datetime.now()
        expiry = st.overlays.next_expiry(now)
        delay = REFRESH_INTERVAL_S
        if expiry:
            delay = max(1, min(delay, (expiry - now).total_seconds()))
        time.sleep(delay)
        try:
            if st.expire_overlays():
                last = st.next_watering()
                continue
            nxt = st.next_watering()
            if nxt != last:
                last = nxt
                st.bump()
                log.info('programme refreshed; next watering %s', nxt)
        except Exception:
            log.exception('refresh failed')


def main(path=None):
    path = path or (sys.argv[1] if len(sys.argv) > 1 else 'config.yaml')
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-7s %(name)s  %(message)s')
    cfg, st = load(path)

    bridge = None
    if cfg.get('mqtt'):
        bridge = mqtt_bridge.Bridge(st, **cfg['mqtt'])
        bridge.start()

    threading.Thread(target=refresher, args=(st,), daemon=True).start()

    httpd = server.serve(st, port=cfg.get('port', 80),
                         hub_cors_headers=cfg.get('hub_cors_headers', True))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if bridge:
            bridge.stop()
        httpd.server_close()


if __name__ == '__main__':
    main()
