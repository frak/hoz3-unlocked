"""HTTP endpoints the hub talks to.

Stands in for hoz3.com. Plain HTTP on port 80: the hub uses no TLS and no auth
(see protocol.md).
"""
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import codec

log = logging.getLogger('hozelock.server')

HEARTBEAT_PATH = re.compile(r'^/notify/([^/]+)/$')
TAP_PATH = re.compile(r'^/notify/([^/]+)/tap/(\d+)/$')


# Reproduced from the capture, and inert for us: the hub is not a browser.
HSTS_HEADER = ('Strict-Transport-Security', 'max-age=63072000; includeSubDomains')

# Captured too, but these would green-light a cross-origin POST, so they are
# attached per hub route rather than globally. Whether the hub reads them at all
# is untested -- see the hub_cors_headers spike in docs/protocol.md.
CORS_HEADERS = [
    ('Access-Control-Allow-Origin', '*'),
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS, DELETE, PUT'),
    ('Access-Control-Max-Age', '1000'),
    ('Access-Control-Allow-Headers',
     'x-requested-with, Content-Type, origin, authorization, accept, '
     'client-security-token'),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    state = None
    hub_cors_headers = True

    def version_string(self):
        return 'Apache'

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, body, content_type, status=200, headers=()):
        """The only writer. Headers are passed in, never defaulted, so a new
        route cannot inherit the hub's CORS set by forgetting to opt out."""
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def _reply(self, blob):
        headers = [HSTS_HEADER]
        if self.hub_cors_headers:
            headers += CORS_HEADERS
        self._send(codec.wrap(blob).encode('latin1'),
                   'text/plain;charset=ISO-8859-1', headers=headers)

    def do_GET(self):
        url = urlparse(self.path)
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


def serve(state, host='0.0.0.0', port=80, hub_cors_headers=True):
    handler = type('BoundHandler', (Handler,),
                   {'state': state, 'hub_cors_headers': hub_cors_headers})
    httpd = ThreadingHTTPServer((host, port), handler)
    log.info('listening on %s:%s for hub %s (hub CORS headers %s)',
             host, port, state.hub_id, 'on' if hub_cors_headers else 'off')
    return httpd
