"""HTTP endpoints the hub talks to.

Stands in for hoz3.com. Plain HTTP on port 80: the hub uses no TLS and no auth
(see protocol.md).
"""
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import api, codec

log = logging.getLogger('hozelock.server')

HEARTBEAT_PATH = re.compile(r'^/notify/([^/]+)/$')
TAP_PATH = re.compile(r'^/notify/([^/]+)/tap/(\d+)/$')


# Reproduced from the capture, and inert for us: the hub is not a browser.
#
# The real service also sent Access-Control-* on every response. We do not: the
# hub was served without them on 1 Sep 2026 and behaved identically (fetched
# tap/0, settled `held` on our generation), so it does not read them -- and
# `Allow-Origin: *` with `Allow-Methods: POST` in front of the control API is
# exactly what would let a website you visit drive your tap. See protocol.md.
HSTS_HEADER = ('Strict-Transport-Security', 'max-age=63072000; includeSubDomains')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    state = None

    def version_string(self):
        return 'Apache'

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, body, content_type, status=200, headers=()):
        """The only writer. Headers are passed in, never defaulted, so a new
        route cannot silently inherit another's."""
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def _reply(self, blob):
        self._send(codec.wrap(blob).encode('latin1'),
                   'text/plain;charset=ISO-8859-1', headers=[HSTS_HEADER])

    def _api(self):
        """-> True if this was an /api/ request, answered."""
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else b''
        result = api.handle(self.state, self.command, urlparse(self.path).path,
                            content_type=self.headers.get('Content-Type'),
                            body=body)
        if result is None:
            return False
        status, payload = result
        if status >= 400:
            log.warning('api %s %s -> %s %s', self.command, self.path, status,
                        payload.get('error'))
        self._send(json.dumps(payload, indent=1).encode(), 'application/json',
                   status=status)
        return True

    def do_POST(self):
        if not self._api():
            self.send_error(404)

    do_PUT = do_POST
    do_DELETE = do_POST

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith(api.PREFIX):
            self._api()
            return

        hb = parse_qs(url.query).get('hb', [None])[0]
        if hb:
            try:
                self.state.observe(codec.b64_decode(hb))
            except Exception:
                log.warning('unparseable hb=%s', hb)

        m = TAP_PATH.match(url.path)
        if m and m.group(1) == self.state.hub_id:
            log.info('tap/%s fetch: generation=%04x pending=%s',
                     m.group(2), self.state.generation, self.state.pending)
            self._reply(self.state.schedule_blob())
            return

        m = HEARTBEAT_PATH.match(url.path)
        if m and m.group(1) == self.state.hub_id:
            log.info('heartbeat: state=%s held=%s generation=%04x',
                     self.state.watering, self.state.generation_held,
                     self.state.generation)
            self._reply(self.state.heartbeat_response())
            return

        # Wrong hub_id is the likeliest misconfiguration, and a bare 404 hides it.
        for pattern in (TAP_PATH, HEARTBEAT_PATH):
            m = pattern.match(url.path)
            if m:
                log.error('hub id mismatch: request says %r, config says %r - '
                          'the hub uses the id from its URL path, not the one the '
                          'app shows', m.group(1), self.state.hub_id)
                break
        else:
            log.warning('unrecognised request: %s', self.path)
        self.send_error(404)


def serve(state, host='0.0.0.0', port=80):
    handler = type('BoundHandler', (Handler,), {'state': state})
    httpd = ThreadingHTTPServer((host, port), handler)
    log.info('listening on %s:%s for hub %s', host, port, state.hub_id)
    return httpd
