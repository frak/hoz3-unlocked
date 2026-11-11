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
| Replacement server | built and passing tests; the hub fetches from it and confirms what it is served |
| Control | Home Assistant cards over MQTT, plus a [JSON API](docs/api.md) that survives a broker outage |
| Next step | run a full week on the replacement — [docs/live-test.md](docs/live-test.md) |

## Layout

```
docs/       protocol spec, capture rig, server design, control API, live test
capture/    the packet-capture rig: bridge script, systemd units, analysis tool
server/     the replacement server (Python 3.14, uv, containerised)
data/       captured traffic — the ground truth behind every claim in the docs
```

Start with **[docs/protocol.md](docs/protocol.md)** — it is the document everything
else depends on.

## Two network modes, and they are mutually exclusive

The Pi is either watching the hub talk to Hozelock, or standing in for Hozelock. It
cannot do both, and each mode holds the same two interfaces.

| | **bridge** — `capture/hozelock-bridge.sh` | **routed** — `capture/hozelock-route.sh` |
|---|---|---|
| hub talks to | the real `hoz3.com` | our server |
| the Pi is | an invisible L2 tap, no IP | gateway and resolver on `10.0.9.x` |
| use it for | capture, collecting checksum samples | the live test, and anything testing our responses |
| set up in | [capture-setup.md](docs/capture-setup.md) | [live-test.md](docs/live-test.md) steps 6–7 |

Switching to routed means stopping the bridge units first, or they reclaim the
interface on the next boot:

```bash
sudo systemctl disable --now hozelock-capture.service hozelock-bridge.service
sudo ip link del br0
```

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

Schedules live in `config.yaml`. **Home Assistant over MQTT discovery** shows the
status and offers water/stop — entities appear automatically, and
`python -m hozelock.dashboard config.yaml` prints the card. A
[JSON API](docs/api.md) covers the same ground for scripts, and keeps working when
the broker does not.

The Hozelock phone app is not supported and dies with the service.

## Tests

```bash
cd server
uv sync
uv run python test_codec.py     # round-trips 24 captured blobs byte-for-byte
uv run python test_schedule.py
uv run python test_server.py
uv run python test_checksum.py  # 560 captured programmes + the worked example
uv run python test_api.py
uv run python test_mqtt.py
uv run python test_dashboard.py
```

The tests run against `data/`, which holds real traffic recorded while the service
was still alive. **That data cannot be regenerated after April 2027**, which makes
it the most valuable thing in this repo.

## What is still unknown

- **The programme epoch** — inferred from the captures, not proven.
- **Battery and signal** — request bytes 11–13 are undecoded, so those sensors are
  not available. Decoding them needs the real service for comparison.

See `docs/protocol.md` for the evidence behind each.
