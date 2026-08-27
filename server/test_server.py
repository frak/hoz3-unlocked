"""Replay real captured hub requests at the server and check what comes back.

Run with the venv interpreter: ./.venv/bin/python test_server.py
"""
import re
import sys
from pathlib import Path
import threading
import urllib.request
from datetime import datetime

from hozelock import codec, schedule, server
from hozelock import state as state_mod

DATA = Path(__file__).resolve().parents[1] / 'data'

PORT = 18080
HUB = 'ge7si1'
REAL_TAP_URI = '/notify/ge7si1/tap/0/?hb=AgEIG-IAAgAAAACUhAUAAAi9AAAIvUlC'


def captured_requests(path=DATA / 'hb.tsv', limit=200):
    """Real request URIs the hub sent, both endpoints."""
    out = []
    for line in open(path):
        f = line.rstrip('\n').split('\t')
        f += [''] * (4 - len(f))
        if not f[0] or not f[1].startswith('192.168.') or '/notify/' not in f[2]:
            continue
        out.append(f[2])
        if len(out) >= limit:
            break
    return out


def check(name, got, want):
    if got == want:
        print(f'  PASS  {name}')
        return True
    print(f'  FAIL  {name}\n        got  {got!r}\n        want {want!r}')
    return False


def main():
    st = state_mod.HubState(
        hub_id=HUB,
        site=schedule.Site(latitude=51.5, longitude=-0.1),
        events=[schedule.Event('06:00', 30), schedule.Event('21:00', 45)])
    httpd = server.serve(st, host='127.0.0.1', port=PORT)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ok = True

    try:
        reqs = captured_requests()
        heartbeats = [r for r in reqs if '/tap/' not in r]
        taps = [r for r in reqs if '/tap/' in r]
        print(f'replaying {len(heartbeats)} heartbeats and {len(taps)} tap fetches')

        bad = 0
        for uri in heartbeats:
            body = urllib.request.urlopen(
                f'http://127.0.0.1:{PORT}{uri}', timeout=5).read().decode('latin1')
            blob = codec.unwrap(body)
            if blob is None or len(blob) != codec.HEARTBEAT_LEN:
                bad += 1
        ok &= check('every heartbeat gets a well-formed 24-byte reply', bad, 0)

        body = urllib.request.urlopen(
            f'http://127.0.0.1:{PORT}' + REAL_TAP_URI,
            timeout=5).read().decode('latin1')
        blob = codec.unwrap(body)
        ok &= check('tap fetch returns 218 bytes', len(blob), codec.SCHEDULE_LEN)
        lead, chain = codec.decode_schedule(blob)
        ok &= check('programme satisfies the cycle invariant',
                    lead + sum(g for _, g in chain), codec.CYCLE_UNITS)
        ok &= check('durations are the configured ones',
                    sorted({d for d, _ in chain}), [30, 45])

        ok &= check('framing carries the trailing dot', body.endswith('.</p></body></html>'), True)
        ok &= check('sentinel present', '#!hb=' in body, True)

        # The hub acts only when the generation moves, so a command must bump it.
        before = st.generation
        st.water_now()
        ok &= check('water_now bumps the generation', st.generation, before + 1)
        blob = codec.unwrap(urllib.request.urlopen(
            f'http://127.0.0.1:{PORT}/notify/{HUB}/tap/0/', timeout=5).read().decode('latin1'))
        ok &= check('water_now sets flag offset 212', blob[211:216].hex(' '),
                    '00 01 00 00 00')
        st.stop_watering()
        blob = codec.unwrap(urllib.request.urlopen(
            f'http://127.0.0.1:{PORT}/notify/{HUB}/tap/0/', timeout=5).read().decode('latin1'))
        ok &= check('stop sets flag offset 213', blob[211:216].hex(' '),
                    '00 00 01 00 00')

        st.set_enabled(False)
        blob = codec.unwrap(urllib.request.urlopen(
            f'http://127.0.0.1:{PORT}/notify/{HUB}/tap/0/', timeout=5).read().decode('latin1'))
        ok &= check('disabling collapses the programme', blob[:4].hex(' '), '00 00 00 cc')
        st.set_enabled(True)

        code = 0
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{PORT}/notify/wronghub/', timeout=5)
        except urllib.error.HTTPError as e:
            code = e.code
        ok &= check('unknown hub id is rejected', code, 404)

        st.observe(codec.b64_decode('AgEIG-IAAgEQAAAAAgAAAAAIwAAAAAjA'))
        ok &= check('watering state parsed from a real request', st.watering, 'manual')

        # A zero checksum is what the real service never sends, and the hub may
        # discard the response entirely if we get it wrong.
        from hozelock import checksums, state as sm
        body = urllib.request.urlopen(
            f'http://127.0.0.1:{PORT}/notify/{HUB}/', timeout=5).read().decode('latin1')
        blob = codec.unwrap(body)
        want = checksums.CHECKSUMS[(st.flag, st.generation)]
        ok &= check('heartbeat carries the captured checksum',
                    (blob[22] << 8) | blob[23], want)
        ok &= check('every generation we serve has a checksum',
                    all((st.flag, g) in checksums.CHECKSUMS
                        for g in st.generations), True)
        demo = sm.HubState('x', schedule.Site(51.5, -0.1), [], demo_mode=True)
        ok &= check('demo mode sets flag 0x10', demo.heartbeat_response()[5], 0x10)
        ok &= check('demo mode has captured checksums too',
                    all((demo.flag, g) in checksums.CHECKSUMS
                        for g in demo.generations), True)
        ok &= check('demo mode has more usable generations',
                    len(demo.generations) > len(st.generations), True)
    finally:
        httpd.shutdown()
        httpd.server_close()

    print('\nOK' if ok else '\nFAILURES')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
