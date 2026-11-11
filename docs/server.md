# Replacement server

Stands in for `hoz3.com` so the hub keeps working after Hozelock shut the service
down at the end of April 2027. Wire format: [protocol.md](protocol.md).

Schedules are configured in `config.yaml`. Home Assistant gets the status and
the two commands over MQTT discovery, with a [JSON API](api.md) alongside it that
keeps working when the broker does not. The Hozelock phone app is not supported
and will stop working when the real service dies.

Editing schedules from Home Assistant — a picker, per-event slots, and pause /
adjust overlays — was built and then cut back to this while the hardware side is
still being proven. It is tagged `ha-controls` if it is wanted again.

## Running

```bash
cp config.example.yaml config.yaml   # then edit it
docker compose up -d                 # serves port 80
```

Then point `hoz3.com` at it with a DNS override on the router. The hub resolves via
the DHCP-supplied resolver, so nothing else is needed.

Locally, without a container:

```bash
cd server
uv sync
uv run hozelock-server config.yaml
```

Python 3.14, dependencies managed by `uv` via `pyproject.toml`.

## Layout

| file | role |
|---|---|
| `hozelock/codec.py` | wire codec — framing, base64url, both blob types. Shared with `../capture/decode.py` so analysis and server cannot drift |
| `hozelock/schedule.py` | human schedule → 7-day interval chain, with solar resolution via `astral` |
| `hozelock/{schedule,heartbeat}_checksum.py` | the solved checksums — [checksum.md](checksum.md) |
| `hozelock/api.py` | the JSON control API — [api.md](api.md) |
| `hozelock/dashboard.py` | generates the Home Assistant card |
| `hozelock/state.py` | hub state and the generation counter |
| `hozelock/server.py` | the two HTTP endpoints |
| `hozelock/mqtt_bridge.py` | Home Assistant entities over MQTT discovery |
| `hozelock/__main__.py` | entry point, config, daily refresh |

## Schedules

Defined in `config.yaml` and read once at startup. Several named schedules can be
listed with one named in `active_schedule`; switching means editing that line and
restarting. **The hub never learns there is more than one** — it is handed the
active schedule's resolved 218-byte programme and nothing else, so there is no
limit on how many you define. The real ceiling is inside one schedule: 211 bytes
of chain at ~2 bytes a watering, so about 100 a week.

An event is a time (`HH:MM`, `sunrise` or `sunset`), a duration in minutes, and
optionally `offset_min` and `days`. Solar times are resolved for the site's
coordinates and re-resolved hourly as they drift.

Startup trial-builds and encodes the active schedule, and logs an error if it
cannot: `schedule_blob()` is on the hub's fetch path, where an unencodable
programme means the hub is served nothing and there is nobody watching.

## Home Assistant

Generate the card rather than copying it, so the entity ids cannot fall behind
the configured `node_id`:

```bash
uv run python -m hozelock.dashboard config.yaml
```

Paste it into a Manual card: watering, next watering, hub contact, which schedule
is running, the enable switch, and water/stop buttons. Core card types only —
nothing to install.

## How control actually works

There is no command channel. Everything hangs off the generation counter:

1. Something changes — a button in HA, or the daily solar refresh.
2. `state.bump()` advances the generation.
3. The hub's next heartbeat shows the server's generation ahead of its own.
4. The hub re-fetches `tap/0` and acts on what it finds.
5. It confirms by echoing the generation back, which clears any pending command.

Manual watering rides in the schedule blob's flag bytes: offset 212 starts, 213
stops. So "water now" is not a separate request — it is a flag on the next
programme the hub collects.

**Nothing happens without a bump.** Any new state that should reach the hub must
call `bump()`, or the hub will never come and look.

## Entities in Home Assistant

Published by discovery, so they appear without any YAML:

- `binary_sensor` — watering, with the trigger (manual/scheduled) as an attribute
- `sensor` — next watering, last hub contact, active schedule
- `button` — water now, stop
- `switch` — schedule enabled

The bridge also publishes an empty retained config for every entity the editable
version announced. Discovery configs are retained, so without that an install
upgraded from `ha-controls` would keep them forever as unavailable entities.

Every entity declares an `object_id`, so ids are stable (`sensor.hozelock_active_schedule`)
and survive renaming the device — without it Home Assistant derives them from the
device name and a rename silently breaks every card that names one.

Availability uses MQTT LWT, so HA marks them unavailable if the container dies. The
broker is not a dependency of watering: if it is down the server keeps answering the
hub, and reconnects when it returns.

## Testing

```bash
cd server
uv run python test_codec.py      # round-trips all 24 captured blobs byte-for-byte
uv run python test_schedule.py   # rebuilds a captured programme from a schedule
uv run python test_server.py     # replays ~200 real hub requests at the server
uv run python test_checksum.py   # both checksums against every captured sample
uv run python test_api.py        # routes, validation, and the two security controls
uv run python test_mqtt.py       # discovery payloads and command handling
uv run python test_dashboard.py  # the card only names entities that exist
```

The corpus in `../data/captures/hb-raw.tsv` is the ground truth, and it cannot be
regenerated after April 2027 — the tests are worth more than usual for that reason.

## Known limits

- **The programme epoch is inferred.** Gaps are counted from a day boundary in the
  server's own clock frame, which fits the captured blobs but is not proven. If
  waterings land at a consistent offset from the intended time, this is why.
- **Manual watering duration is unknown** — one captured sample, no duration field
  identified. The server issues a start and relies on an explicit stop.
- **No battery or signal sensors.** Request bytes 11–13 are undecoded. Decoding them
  needs the real service to compare against, so it must happen before April 2027 if
  those readings matter.
