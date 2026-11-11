"""Entry point: serve the hub, bridge to Home Assistant.

    hozelock-server config.yaml
"""
import logging
import sys
import threading
import time

import yaml

from . import mqtt_bridge, schedule, server
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
    schedules = schedules_from_config(cfg)

    st = state_mod.HubState(
        hub_id=cfg['hub_id'],
        site=site,
        schedules=schedules,
        active_schedule=cfg.get('active_schedule'),
        restrict_generations=cfg.get('restrict_generations', False),
        initial_generation=cfg.get('initial_generation'),
        corrupt_checksum=cfg.get('corrupt_checksum', False),
        replay_blob=cfg.get('replay_blob'),
        demo_mode=cfg.get('demo_mode', False),
    )
    log.info('schedule %r loaded from %s', st.active_schedule, path)
    return cfg, st


def refresher(st):
    last = None
    while True:
        time.sleep(REFRESH_INTERVAL_S)
        try:
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

    reason = st.unservable()
    if reason:
        # Nothing to be done about this on the hub's fetch path, so say it here
        # rather than silently serving an empty programme.
        log.error('the active schedule cannot be encoded, so the hub will be '
                  'served nothing: %s', reason)

    bridge = None
    if cfg.get('mqtt'):
        bridge = mqtt_bridge.Bridge(st, **cfg['mqtt'])
        bridge.start()

    threading.Thread(target=refresher, args=(st,), daemon=True).start()

    httpd = server.serve(st, port=cfg.get('port', 80))
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
