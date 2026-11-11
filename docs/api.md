# Control API

JSON over HTTP, served by the same listener as the hub endpoints, under `/api/`.
It and the MQTT bridge are both adapters over the same `HubState` methods —
neither owns any behaviour, so neither can drift from the other.

The reason it exists alongside MQTT: **it keeps working when the broker is down.**
That is the difference between a stuck tap and a stoppable one.

```bash
curl localhost/api/state
curl -X POST -H 'Content-Type: application/json' localhost/api/water
```

## Routes

| method | route | body | notes |
|---|---|---|---|
| GET | `/api/state` | — | everything the card shows |
| GET | `/api/schedules` | — | every configured schedule with its events |
| PUT | `/api/enabled` | `{"enabled": true}` | the master switch |
| POST | `/api/water` | — | rides the next programme's flag bytes |
| POST | `/api/stop` | — | |

Schedules are read-only here: they come from `config.yaml`. The routes that
edited them (`PUT /api/schedules/...`, `/api/pause`, `/api/adjust`) were removed
with the rest of the editing layer and now answer `404`; they are in the
`ha-controls` tag if they are wanted back.

Errors are `{"error": "..."}` with a reason worth reading: `400` for a malformed
body, `404` for an unknown route, `415` for the content type.

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
