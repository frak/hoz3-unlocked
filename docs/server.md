# Replacement server

Stands in for `hoz3.com` so the hub keeps working after Hozelock shut the service
down at the end of April 2027. Wire format: [protocol.md](protocol.md).

Control lives in Home Assistant over MQTT discovery, with a
[JSON API](api.md) alongside it that keeps working when the broker does not. The
Hozelock phone app is not supported and will stop working when the real service
dies.

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
| `hozelock/persist.py` | `state.json`: schedules, selection and overlays across restarts |
| `hozelock/dashboard.py` | generates the Home Assistant cards |
| `hozelock/state.py` | hub state and the generation counter |
| `hozelock/server.py` | the two HTTP endpoints |
| `hozelock/mqtt_bridge.py` | Home Assistant entities over MQTT discovery |
| `hozelock/__main__.py` | entry point, config, daily refresh |

## Schedules

Several named schedules, one active at a time, switchable from Home Assistant or
the API. **The hub never learns there is more than one** — it is handed the
active schedule's resolved 218-byte programme and nothing else, so there is no
limit on how many you define. The real ceiling is inside one schedule: 211 bytes
of chain at ~2 bytes a watering, so about 100 a week.

Two temporary overlays sit on top, as the app offers them:

- **Pause for N days** — suppress waterings in that window.
- **Adjust ±X% for N days** — scale durations. `+50%` turns 45/60 into 68/90,
  matching the capture exactly. Clamped to 0–255: a duration is one byte.

Both carry an absolute expiry, so they survive a restart and still lapse on time.
When one lapses the generation is bumped — without that the hub would never
refetch and would keep watering the boosted programme indefinitely.

The programme is a repeating week anchored at an origin in the past, so a slot
the clock has already gone by fires when the cycle comes round again, not now.
Overlays compare against that real occurrence; it is why a 2-day pause leaves
next week's slots alone and an 8-day one reaches them.

### config.yaml seeds it; state.json owns it

Once schedules can be edited from Home Assistant, `config.yaml` cannot stay the
source of truth. So:

- no `state.json` → seed from `config.yaml` and write it
- `state.json` present → **it wins**, and `config.yaml`'s `schedules:` is ignored
- re-seed by deleting `state.json`

The server logs which it used at startup. Editing `config.yaml` on a running
install and seeing nothing change is the expected trap, so it says so.

`days` and `offset_min` stay config-only — the card edits when and how long. An
event can still be `sunrise` with `offset_min: 30`; the card simply shows it as
`Sunrise` with no offset control.

## Home Assistant

Generate the two cards rather than copying them, so they cannot fall behind the
entities:

```bash
uv run python -m hozelock.dashboard config.yaml
```

Paste each into a Manual card. A **status** card (watering, next watering, hub
contact, water/stop, pause and adjust) and a **schedule** card (the picker, the
enable switch, and one block per event slot). Core card types only — nothing to
install.

Event slots auto-size to the longest schedule, so the card can never edit fewer
waterings than the hub is served; `max_event_slots` is a minimum, not a cap.
Unused slots hide themselves, and the time row only appears in Clock mode.

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

Every mutation goes through `HubState`, which trial-builds the programme before
committing: an edit that could not be encoded is refused with a reason rather
than leaving the hub with nothing to fetch. Home Assistant is republished on a
refusal so the card snaps back instead of showing a value the server rejected.

**Nothing happens without a bump.** Any new state that should reach the hub must
call `bump()`, or the hub will never come and look.

## Entities in Home Assistant

Published by discovery, so they appear without any YAML:

- `binary_sensor` — watering, with the trigger (manual/scheduled) as an attribute
- `sensor` — next watering, last hub contact, paused until, adjust until
- `sensor` — schedule detail, carrying the whole active schedule as attributes
- `button` — water now, stop
- `switch` — schedule enabled
- `select` — active schedule, and a mode per event slot
- `text` — a clock time per event slot
- `number` — a duration per event slot, plus pause days, adjust percent and days

Every entity declares an `object_id`, so ids are stable (`select.hozelock_active_schedule`)
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
uv run python test_overlays.py   # pause and adjust: scaling, clamping, expiry
uv run python test_api.py        # routes, validation, and the two security controls
uv run python test_mqtt.py       # discovery payloads and command handling
uv run python test_dashboard.py  # the cards only name entities that exist
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
