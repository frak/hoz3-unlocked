"""Wire codec for the Hozelock hub <-> hoz3.com protocol.

Shared by the analysis tool (decode.py) and the replacement server, so the two
cannot drift apart. Format is documented in protocol.md.
"""
import base64
import re

SENTINEL_RE = re.compile(r'#!hb=([A-Za-z0-9_\-=]+)\.')

HEARTBEAT_LEN = 24
SCHEDULE_LEN = 218
TERMINATOR = b'\x00\xcc'
PAD = 0xff

GAP_UNIT_MIN = 5
CYCLE_UNITS = 2016          # every programme spans exactly 7 days

# Continuation bytes in a gap run. 0xc9 contributes 200 rather than its face
# value; that is what makes every captured blob total exactly CYCLE_UNITS.
CONT_C9 = 0xc9
CONT_C9_VALUE = 200
CONT_FF_VALUE = 255

WATERING_STATES = {
    (0x00, 0x00): 'idle',
    (0x01, 0x10): 'manual',
    (0x01, 0x04): 'scheduled',
    # Seen once on the real service, 18:05-19:09 on 21 Aug, with no watering and
    # nothing unusual around it. Not "waiting for the controller": it persists in
    # demo mode where the controller polls every minute. Meaning unknown.
    (0x02, 0x00): 'state-02',
}


def b64_decode(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def b64_encode(b):
    # Padding must be kept: the hub's decoder needs it. A 24-byte heartbeat
    # needs none, but a 218-byte programme does, and without it the hub silently
    # refuses the programme and re-fetches forever.
    return base64.urlsafe_b64encode(b).decode()


def unwrap(body):
    """Pull the blob out of a response body, or None if the sentinel is absent."""
    m = SENTINEL_RE.search(body)
    return b64_decode(m.group(1)) if m else None


def wrap(blob):
    """Frame a blob the way the hub expects: sentinel, payload, trailing dot.

    The hub scans for '#!hb=' and stops at the '.'; the markup is decoration.
    """
    return (f'<html><head><title>Hozelock</title></head><body><p>'
            f'#!hb={b64_encode(blob)}.</p></body></html>')


def encode_gap(units):
    """Emit a gap as the server does: alternate 0xc9 and 'ff 00', then remainder.

    Verified against all 140 gap encodings in the capture corpus.
    """
    if units < 0:
        raise ValueError(f'negative gap: {units}')
    out = bytearray()
    use_c9 = True
    while units >= CONT_C9_VALUE:
        if use_c9:
            out.append(CONT_C9)
            units -= CONT_C9_VALUE
        else:
            out.extend((0xff, 0x00))
            units -= CONT_FF_VALUE
        use_c9 = not use_c9
    out.append(units)
    return bytes(out)


def _is_continuation(blob, i):
    return blob[i] in (CONT_C9, 0xff) or (blob[i] == 0x00 and blob[i - 1] in (CONT_C9, 0xff))


def _cont_value(byte):
    return CONT_C9_VALUE if byte == CONT_C9 else (CONT_FF_VALUE if byte == 0xff else 0)


def decode_schedule(blob):
    """-> (lead_gap_units, [(duration_min, gap_units)])

    Layout: 00 <gap> <duration> <gap> <duration> ... 00 cc <ff pad> <flags> <ck>
    """
    events = []
    lead = None
    acc = 0
    i = 1
    while i < len(blob) - 1 and blob[i:i + 2] != TERMINATOR:
        if _is_continuation(blob, i):
            acc += _cont_value(blob[i])
            i += 1
            continue
        acc += blob[i]
        i += 1
        if lead is None:
            lead = acc
        elif events:
            events[-1] = (events[-1][0], acc)
        acc = 0
        if i < len(blob) - 1 and blob[i:i + 2] != TERMINATOR:
            events.append((blob[i], 0))
            i += 1
    return lead or 0, events


def encode_schedule(lead_units, events, flags=b'\x00' * 5, checksum=b'\x00\x00'):
    """Build the 218-byte programme.

    `events` is [(duration_min, gap_units)] and, with lead_units, must total
    CYCLE_UNITS -- the invariant that held across every captured blob.
    """
    total = lead_units + sum(g for _, g in events)
    if events and total != CYCLE_UNITS:
        raise ValueError(f'programme totals {total} units, expected {CYCLE_UNITS}')

    out = bytearray([0x00])
    out += encode_gap(lead_units)
    for duration, gap in events:
        if not 0 <= duration <= 0xff:
            raise ValueError(f'duration out of range: {duration}')
        out.append(duration)
        out += encode_gap(gap)
    out += TERMINATOR

    tail = len(flags) + len(checksum)
    if len(out) > SCHEDULE_LEN - tail:
        raise ValueError(f'programme too long: {len(out)} bytes')
    out += bytes([PAD]) * (SCHEDULE_LEN - tail - len(out))
    out += flags + checksum
    return bytes(out)


def schedule_checksum(blob):
    """Compute a programme's checksum from the solved algorithm.

    Byte-hash structure recovered in docs/checksum-problem.md; the map is
    X8-propagated rather than measured, so it holds for any programme shape and
    never falls outside a span. Kept returning an int (never None) so callers
    can serve generated programmes unconditionally.
    """
    from . import schedule_checksum as solved
    return solved.checksum(blob)


def decode_heartbeat_request(blob):
    return {
        'state': WATERING_STATES.get((blob[7], blob[8]), f'unknown {blob[7]:02x}{blob[8]:02x}'),
        'generation_held': (blob[16] << 8) | blob[17],
        'generation_confirmed': (blob[20] << 8) | blob[21],
        'link': bytes(blob[11:14]),
        'checksum': bytes(blob[22:24]),
    }


def decode_heartbeat_response(blob):
    return {
        'clock_minutes': (blob[0] << 8) | blob[1],
        'clock_seconds': blob[2],
        'flag': blob[5],
        'generation': (blob[8] << 8) | blob[9],
        'checksum': bytes(blob[22:24]),
    }


def encode_heartbeat_response(clock_minutes, clock_seconds, generation,
                              flag=0x00, checksum=b'\x00\x00'):
    blob = bytearray(HEARTBEAT_LEN)
    blob[0] = (clock_minutes >> 8) & 0xff
    blob[1] = clock_minutes & 0xff
    blob[2] = clock_seconds
    blob[3] = 0x02
    blob[4] = 0x01
    blob[5] = flag
    blob[8] = (generation >> 8) & 0xff
    blob[9] = generation & 0xff
    blob[22:24] = checksum
    return bytes(blob)
