"""Exercise the control API over a real socket.

Run with the venv interpreter: uv run python test_api.py

Two things here are security controls rather than behaviour, and both fail open:
no route may carry Access-Control-*, and no mutating route may act on a request
that did not declare JSON.
"""
import json
import sys
import threading
import urllib.error
import urllib.request

from hozelock import schedule, server
from hozelock import state as state_mod

HUB = 'ge7si1'


def check(name, got, want):
    if got == want:
        print(f'  PASS  {name}')
        return True
    print(f'  FAIL  {name}\n        got  {got!r}\n        want {want!r}')
    return False


def call(port, method, path, body=None, content_type='application/json'):
    """-> (status, parsed body or raw text)."""
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                                 data=data, method=method)
    if content_type:
        req.add_header('Content-Type', content_type)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw, status, headers = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, headers = e.read(), e.code, e.headers
    try:
        return status, json.loads(raw), headers
    except ValueError:
        return status, raw.decode(errors='replace'), headers


def main():
    st = state_mod.HubState(
        hub_id=HUB,
        site=schedule.Site(latitude=51.5, longitude=-0.1),
        schedules=[
            schedule.Schedule('Summer', [schedule.Event('sunrise', 45),
                                         schedule.Event('sunset', 60)]),
            schedule.Schedule('Winter', [schedule.Event('10:00', 20)]),
            schedule.Schedule('Away', []),
        ],
        active_schedule='Summer')
    httpd = server.serve(st, host='127.0.0.1', port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ok = True

    try:
        status, b, _ = call(port, 'GET', '/api/state')
        ok &= check('GET /api/state', status, 200)
        ok &= check('  reports the active schedule', b['active_schedule'], 'Summer')
        ok &= check('  lists every schedule', b['schedules'],
                    ['Summer', 'Winter', 'Away'])

        status, b, _ = call(port, 'GET', '/api/schedules')
        ok &= check('GET /api/schedules', status, 200)
        ok &= check('  carries the events',
                    [e['at'] for e in b['schedules'][0]['events']],
                    ['sunrise', 'sunset'])

        print()
        print('  -- the editing routes are gone, not merely unused --')
        for method, path, payload in (
                ('PUT', '/api/schedules/active', {'name': 'Winter'}),
                ('PUT', '/api/schedules/Winter',
                 {'events': [{'at': '07:00', 'duration_min': 15}]}),
                ('PUT', '/api/pause', {'days': 2}),
                ('PUT', '/api/adjust', {'percent': 50, 'days': 2})):
            status, b, _ = call(port, method, path, payload)
            ok &= check(f'{method} {path} is 404', status, 404)
        ok &= check('  and the active schedule is untouched',
                    st.active_schedule, 'Summer')
        ok &= check('  as are its events',
                    [(e.at, e.duration_min) for e in st.events],
                    [('sunrise', 45), ('sunset', 60)])

        status, b, _ = call(port, 'POST', '/api/water', {})
        ok &= check('POST water', status, 200)
        ok &= check('  queues the command', b['pending_command'], 'water')
        status, b, _ = call(port, 'POST', '/api/stop', {})
        ok &= check('POST stop', status, 200)
        ok &= check('  queues the command', b['pending_command'], 'stop')

        status, b, _ = call(port, 'PUT', '/api/enabled', {'enabled': False})
        ok &= check('PUT enabled', status, 200)
        ok &= check('  disables', st.schedule_enabled, False)
        call(port, 'PUT', '/api/enabled', {'enabled': True})

        print()
        print('  -- security controls (both fail open) --')

        # The cross-origin case: a form POST is delivered without a preflight,
        # so refusing it here is what stops a web page opening the tap.
        st.pending = state_mod.CMD_NONE
        status, b, _ = call(port, 'POST', '/api/water', b'x=1',
                            content_type='application/x-www-form-urlencoded')
        ok &= check('a form-encoded POST is refused with 415', status, 415)
        ok &= check('  and did not act', st.pending, state_mod.CMD_NONE)

        status, b, _ = call(port, 'POST', '/api/water', b'{}', content_type=None)
        ok &= check('no Content-Type at all is refused', status, 415)
        ok &= check('  and did not act', st.pending, state_mod.CMD_NONE)

        status, b, _ = call(port, 'PUT', '/api/enabled', b'{not json',
                            content_type='application/json')
        ok &= check('malformed JSON is 400, not 500', status, 400)

        for path in ('/api/state', '/api/schedules', '/api/nope'):
            _, _, h = call(port, 'GET', path)
            n = len([k for k in h.keys() if k.lower().startswith('access-control')])
            ok &= check(f'{path} carries no CORS headers', n, 0)

        status, b, _ = call(port, 'GET', '/api/nope')
        ok &= check('unknown route is 404', status, 404)

        print()
        blob = st.schedule_blob()
        ok &= check('the hub is still served after all of that',
                    len(blob), 218)
    finally:
        httpd.shutdown()
        httpd.server_close()

    print('\nOK' if ok else '\nFAILURES')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
