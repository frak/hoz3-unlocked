# Control API

JSON over HTTP, served by the same listener as the hub endpoints, under `/api/`.
It and the MQTT bridge are both adapters over the same `HubState` methods —
neither owns any behaviour, so neither can drift from the other.

The reason it exists alongside MQTT: **it keeps working when the broker is down.**
That is the difference between a stuck tap and a stoppable one.

```bash
curl localhost/api/state
curl -X POST -H 'Content-Type: application/json' localhost/api/water
curl -X PUT  -H 'Content-Type: application/json' \
     -d '{"name": "Winter"}' localhost/api/schedules/active
```

## Routes

| method | route | body | notes |
|---|---|---|---|
| GET | `/api/state` | — | everything the cards show, plus overlays and their expiries |
| GET | `/api/schedules` | — | every schedule with its events |
| PUT | `/api/schedules/active` | `{"name": "Summer"}` | `404` if unknown |
| PUT | `/api/schedules/{name}` | `{"events": [...]}` | replaces the event list |
| PUT | `/api/enabled` | `{"enabled": true}` | the master switch |
| PUT | `/api/pause` | `{"days": 2}` | `0` cancels |
| PUT | `/api/adjust` | `{"percent": 50, "days": 2}` | `0` cancels |
| POST | `/api/water` | — | rides the next programme's flag bytes |
| POST | `/api/stop` | — | |

An event is `{"at": "06:00"|"sunrise"|"sunset", "duration_min": 45,
"days": ["mon", ...], "offset_min": 0}`. Only `at` and `duration_min` are
required.

Errors are `{"error": "..."}` with a reason worth reading: `400` for something
the hub could not be served (an unencodable duration, a programme too long for
218 bytes), `404` for an unknown schedule or route, `415` for the content type.

## Two things that are security controls, not style

Both fail *open* — if either regresses, nothing looks broken until someone else's
web page opens your tap. `test_api.py` asserts them on every route.

**No `Access-Control-*` header, anywhere.** The real service sent them and we
reproduced them until the hub was shown not to read them (see
[protocol.md](protocol.md)). Without them a browser cannot read a cross-origin
response from this API.

**Mutating routes require `Content-Type: application/json`,** or they answer
`415` and do nothing. This is the part that actually stops a cross-origin write.
A form POST is a "simple request": no preflight, so the browser *sends* it and
the side effect happens even though the reply is unreadable. Demanding JSON makes
it non-simple, forcing a preflight that no route answers — so the request is
never sent at all.

Do not relax this to "be helpful to curl users". `curl` sends the header happily;
a malicious page cannot.

## What it deliberately does not do

**No authentication.** Anything on the LAN can drive it — the same exposure the
hub endpoints already have, and the cloud API had none either. If the network is
not trusted, put it behind something that is.

**A custom Lovelace card cannot call it.** With no CORS headers a browser blocks
cross-origin reads from Home Assistant's origin. That is the intended trade:
cards use `hass.callService` and `hass.states` rather than raw fetch, so the card
path goes through MQTT. This API is for server-side consumers — curl, scripts,
and Home Assistant's `rest_command`, which runs inside HA rather than the
browser.
