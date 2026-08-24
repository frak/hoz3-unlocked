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


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    state = None

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _reply(self, blob):
        body = codec.wrap(blob).encode('latin1')
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain;charset=ISO-8859-1')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

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

        log.warning('unrecognised request: %s', self.path)
        self.send_error(404)


def serve(state, host='0.0.0.0', port=80):
    handler = type('BoundHandler', (Handler,), {'state': state})
    httpd = ThreadingHTTPServer((host, port), handler)
    log.info('listening on %s:%s for hub %s', host, port, state.hub_id)
    return httpd
