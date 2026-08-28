# Hozelock Cloud Controller — local replacement

Hozelock will shut the Cloud Controller service down at the end of **April 2027**. The
hub talks only to `hoz3.com`, so without a replacement the watering stops. The tap
controller's radio link is proprietary and not replaceable, which means the hub has
to stay in the system and be convinced that our server is Hozelock.

This repo contains the traffic capture that decoded the protocol, the protocol
specification itself, and a replacement server.

## Status

| | |
|---|---|
| Protocol | fully decoded, including both checksums |
| Replacement server | built, passing tests against captured traffic; run against the hub, but not yet serving a computed programme end to end |
| Next step | serve a generated programme to the hub — [docs/live-test.md](docs/live-test.md) |

## Layout

```
docs/       protocol specification, capture rig build, server design, live test
capture/    the packet-capture rig: bridge script, systemd units, analysis tool
server/     the replacement server (Python 3.14, uv, containerised)
data/       captured traffic — the ground truth behind every claim in the docs
```

Start with **[docs/protocol.md](docs/protocol.md)** — it is the document everything
else depends on.

## The protocol, in one paragraph

Cleartext HTTP on port 80, no auth, no TLS. The hub GETs
`/notify/{hubId}/` every ~16 minutes carrying a 24-byte status blob, and gets back a
24-byte reply containing a clock and a **generation counter**. When that counter
moves, the hub re-fetches `/notify/{hubId}/tap/0/` and receives a 218-byte watering
programme: a chain of durations and gaps that always totals exactly seven days.
There is no command channel — "water now" is a flag byte in that programme.

## Running the server

```bash
cd server
docker compose up -d       # serves port 80, reads ./config.yaml
```

Then point `hoz3.com` at it with a DNS override on the router. The hub resolves via
the DHCP-supplied resolver, so no NAT interception is needed.

Control is via **Home Assistant over MQTT discovery** — entities appear
automatically. The Hozelock phone app is not supported and dies with the service.

## Tests

```bash
cd server
uv sync
uv run python test_codec.py     # round-trips 24 captured blobs byte-for-byte
uv run python test_schedule.py
uv run python test_server.py
uv run python test_checksum.py  # 560 captured programmes + the worked example
```

The tests run against `data/`, which holds real traffic recorded while the service
was still alive. **That data cannot be regenerated after April 2027**, which makes
it the most valuable thing in this repo.

## What is still unknown

- **The programme epoch** — inferred from the captures, not proven.
- **Battery and signal** — request bytes 11–13 are undecoded, so those sensors are
  not available. Decoding them needs the real service for comparison.

See `docs/protocol.md` for the evidence behind each.
