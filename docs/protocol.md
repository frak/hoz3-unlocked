# Hozelock Cloud Controller — hub ↔ hoz3.com protocol

What the hub actually says to Hozelock, decoded from packet captures taken 21–23 August 2026 while the real service was
still running. The service shuts down at the end of April 2027; this document is the input to building a replacement.

Rig and capture instructions: [capture-setup.md](capture-setup.md). Analysis tooling: [decode.py](../capture/decode.py).

Evidence base: 44.8 hours, 507 heartbeat requests and 14 schedule fetches, including two sessions of deliberate
app-driven state changes logged in
[events.log](../data/events.log).

## Transport

**Cleartext HTTP on port 80. No authentication, no TLS, no client certificate.**
The hub's entire cloud conversation is GET requests; it never POSTs. All state travels in the query string, all commands
in the response body.

```
GET /notify/ge7si1/?hb=AgEIG-IAAgAAAAACAAAAAAi9AAAAAObU HTTP/1.1
Host: hoz3.com
Connection: Close
```

```
HTTP/1.1 200 OK
Content-Type: text/plain;charset=ISO-8859-1

<html><head><title>Hozelock</title></head><body><p>#!hb=IH4FAgEAAAAIvQAAAAAAAAAAAAAAAIox.</p></body></html>
```

Three things a replacement server must honour:

- **The response is sentinel-framed, not HTML.** The hub scans for `#!hb=` and stops at the `.`. The markup is
  decoration and can be anything, but reproduce the sentinel and the trailing dot exactly.
- **`hb` is base64url** — `-` and `_`, not `+` and `/`.
- **The request arrives in many tiny TCP segments** — 12, 6, 5, 32, 9 bytes, one write per token, no coalescing. Any
  real HTTP server handles this; a hand-rolled socket listener must not assume one `read()` yields a whole request line.

One connection per poll, closed after the response.

### Cadence

|                          | interval                                                           |
|--------------------------|--------------------------------------------------------------------|
| heartbeat, normal        | 16 min (median), up to 49 min                                      |
| heartbeat, demo mode     | 7 s                                                                |
| schedule fetch (`tap/0`) | bursty — clusters around changes and reboots, otherwise up to 14 h |

Demo mode speeds up the controller↔hub radio *and* the hub↔cloud heartbeat. It expires by itself after an hour.

## Endpoints

| path                     | response  | purpose                                      |
|--------------------------|-----------|----------------------------------------------|
| `/notify/{hubId}/`       | 24 bytes  | heartbeat: state up, commands and clock down |
| `/notify/{hubId}/tap/0/` | 218 bytes | the tap's watering programme                 |

`{hubId}` is `ge7si1` on this unit. **This is not the ID the app shows** — that is a different, longer identifier. The
only value that matters here is the one the hub puts in the URL path, which you can only get from a capture:

```bash
grep -o '/notify/[^/]*/' data/hb.tsv | sort -u
``` 

Both requests carry the *same* 24-byte heartbeat blob in `hb`; only the responses differ. `tap/1` has not been observed
(single-tap installation).

## Heartbeat, hub → cloud (24 bytes)

```
02 01 08 1b e2 00 02 00 00 00 00 02 00 00 00 00 08 bd 00 00 00 00 e6 d4
```

| offset | meaning                                                        |
|--------|----------------------------------------------------------------|
| 0–1    | `02 01` — constant, version or message type                    |
| 2–4    | `08 1b e2` — constant, hub identity                            |
| 7–8    | **watering state** (below)                                     |
| 11–13  | radio link metrics — fluctuate continuously, no decision value |
| 16–17  | generation the hub currently holds                             |
| 20–21  | generation the hub has confirmed                               |
| 22–23  | checksum (unidentified)                                        |

Watering state, offsets 7–8:

| value   | meaning                      |
|---------|------------------------------|
| `00 00` | idle                         |
| `01 10` | watering, manually triggered |
| `01 04` | watering, schedule triggered |

The distinction matters: the hub reports *why* it is watering, so the server can tell its own commands from schedule
firings.

## Heartbeat, cloud → hub (24 bytes)

```
20 7e 05 02 01 00 00 00 08 bd 00 00 00 00 00 00 00 00 00 00 00 00 8a 31
```

| offset | meaning                                                        |
|--------|----------------------------------------------------------------|
| 0–1    | clock: **minutes within a 7-day week**, 0–10079 (below)        |
| 2      | clock: seconds, matches wall clock (252/273 samples)           |
| 5      | flag, `0x10` / `0x00` — meaning unknown                        |
| 8–9    | **generation counter**, server side                            |
| 22–23  | checksum (unidentified)                                        |

### The clock is a weekly counter, and it anchors everything

Not a free-running counter: it runs 0–10079 and wraps every seven days. The capture contains the wrap — `10078` at
22:53 on 22 August, then `14` at 23:09. Minute zero fell on **Saturday 22:55** local.

That is exactly one week, which is exactly what a programme spans: 2016 gap units = 10080 minutes. **The programme is
indexed by this clock.** Its lead gap is the offset from minute zero — for the captured 06:00/21:00 schedule, 85 units
= 425 minutes after Saturday 22:55, which is Sunday 06:00.

A replacement server generates both the clock and the programme, so the origin is a free choice — but the two must
agree. Serving a clock in one frame and a programme in another gets the programme **rejected**: the hub fetches it,
refuses it, and re-fetches in a loop without ever confirming. Matching Hozelock's Saturday 22:55 origin has the added
benefit that captured blobs can be replayed verbatim.

## The generation counter is the whole control mechanism

There is no command channel. The server increments a 16-bit counter (response bytes 8–9) **once per app-side change**.
The hub sees its own value (request 16–17) is behind, acts, and confirms (request 20–21).

Every logged action during the 22 August session bumped it by exactly one:

```
18:59 water now 08c3 → 08c4 19:05 duration +50% 08c7 → 08c8 19:00 stop watering 08c4 → 08c5 19:07 cancel adjust 08c8 →
08c9 19:01 pause 2 days 08c5 → 08c6 19:09 enable schedule 08c9 → 08ca 19:03 unpause 08c6 → 08c7 19:11 enable schedule
08ca → 08cb
```

Observed range across the capture: `0x08bd`–`0x08da`, monotonic.

So the replacement server needs one monotonic counter, incremented on every state change. That is the entire signalling
design.

## Schedules — `tap/0`, 218 bytes

```
00 | <gap> | <duration> | <gap> | <duration> | … | 00 cc | ff…ff | 00 00 00 00 00 | ck
```

A leading `0x00`, then an alternating chain of **gaps** and **durations**, terminated by `00 cc`, padded with `0xff` to
a fixed 218 bytes, then **five** flag bytes (offsets 211–215) and a two-byte checksum (216–217).

### The flag bytes are the manual command channel

There is no separate "water now" endpoint. The commands ride in the schedule blob's flag field, and the generation
counter is what makes the hub come and collect them.

The flags are `00 00 00 00 00` in 22 of the 24 captured blobs. The two exceptions line up exactly with the only two
manual commands ever logged:

| time     | flags            | logged action                    |
|----------|------------------|----------------------------------|
| 18:59:36 | `00 01 00 00 00` | "water now for 1 minute" (18:59) |
| 19:00:13 | `00 00 01 00 00` | "stop watering" (19:00)          |

So offset 212 starts a manual watering and offset 213 stops one. The hub then reports `01 10` — watering, manually
triggered — in its heartbeat, which is how the server confirms the command landed.

**The duration of a manual watering is not encoded here.** The one sample was a 1-minute run and offsets 214–215 were
both zero, so either manual runs use a default the hub already holds, or the duration lives somewhere not yet seen. A
server should issue the command and then stop it explicitly rather than assume a duration.

- **Durations** are one byte, in minutes.
- **Gaps** are in 5-minute units and are *variable length*: a gap too large for one byte continues across several.
  `0xc9` and `0xff` mark "keep going", as does a
  `0x00` immediately after one. Sum the run to get the gap.
- **The chain always totals exactly 2016 units — 168 hours — one week.** Verified across all 13 distinct blobs captured,
  covering daily, twice-daily, weekly, duration-adjusted and disabled schedules. This is the strongest invariant in the
  protocol and the best sanity check on a generated blob.
- The leading gap positions the first event relative to the programme epoch.

`0xc9` contributes **200**, not 201 — that is what makes every blob total exactly

2016. It is possible the true split is `0xc9`=201 with `0xff`=254; the samples cannot distinguish those, only their sum
      (455 per pair).

### Worked examples

Twice daily — 06:00 for 30 min and 21:00 for 45 min:

```
00 55 1e b4 2d 6c 1e b4 … └gap┘ └d┘└gap┘└d┘
```

Gap 0x55=85, duration 0x1e=30, gap 0xb4=180 (15h00), duration 0x2d=45, gap 0x6c=108 (9h00). The two gaps sum to 288
units = 24h, repeated seven times.

Weekly — Tuesday 16:30 for 30 min:

```
00 c9 ff 00 c9 84 | 1e | c9 ff 00 c9 ff 00 c9 77 | 00 cc └─── gap 787 ────┘ 30 └────── gap 1229 ──────┘
```

787 + 1229 = 2016. A weekly schedule is not a different format — it is the same chain with gaps long enough to need
continuation bytes.

### Corroboration

Three independent checks, all from deliberate experiments:

- **+50% for 2 days** rewrote durations 45/60 → 68/90 (45×1.5=67.5, 60×1.5=90) and left every other byte alone.
- **Moving a schedule 5 minutes later** (Tue 16:30 → 16:35) moved the gap before the event +1 and the gap after it −1,
  conserving the 2016 total. That pins the unit at 5 minutes.
- **Solar drift**: between 21 and 22 August the sunrise→sunset gap shrank one unit and sunset→sunrise grew one. Five
  minutes each way, in late August, is the days shortening. The encoding predicts real astronomy.

### Consequences for the replacement server

**There are no absolute times in the blob** — only durations and intervals against a programme epoch. Three things
follow:

1. The heartbeat clock is the anchor for everything. Drift there becomes watering drift.
2. **The server must do the solar calculation itself.** The cloud resolves sunrise/sunset to concrete intervals; the hub
   has no notion of "sunrise", and intervals must be recomputed and re-pushed daily for the site's latitude.
3. Schedule edits must bump the generation counter, or the hub will not re-fetch.

Because your server generates *both* the clock and the programme, the epoch is a free parameter — it only has to be
internally consistent. That is why the one remaining unknown below does not block implementation.

Times are **local** (BST at capture), confirmed by the 21:00 schedule firing at 21:01.

Disabling all schedules collapses the payload to `00 00 00 cc` plus padding.

## Deployment

The hub resolves `hoz3.com` through the DHCP-supplied resolver (the router,
`192.168.1.1`) — not a hardcoded public one. **A DNS override at the router is enough to redirect it**: no NAT
interception, no port-53 hijack, and the capture bridge is not needed in the final deployment.

The app-facing side is different. The real service sends
`Strict-Transport-Security: max-age=63072000; includeSubDomains`, so clients have
`hoz3.com` pinned to HTTPS for two years and a replacement needs a genuinely trusted certificate. The hub side needs
none. Home Assistant can sidestep this entirely by pointing its REST sensors straight at the replacement's address.

## Open questions

- **The checksum algorithm.** See below — heavily characterised, not identified.
- **Response byte 5** (`0x10`/`0x00`) and **request bytes 11–13**.
- **Other endpoints.** Only `/notify/{hubId}/` and `/notify/{hubId}/tap/0/` have been seen, and `hoz3.com` was the only
  hostname resolved. A firmware-update endpoint that only appears at boot would be easy to miss — worth re-checking DNS
  across a hub reboot before cutting the hub off from the internet.

None of these block building the server. All of them can be resolved by experiment against your own implementation once
it is standing up — which matters, because the real service is the resource with the deadline.

## The checksums

Both message types end in two bytes that behave like a checksum. Characterised on 23 August from ~900 heartbeats and 24
schedule blobs, including deliberate single-byte sweeps.

**They cover far less than the message.** Determined by finding sample pairs that differ in exactly one byte:

| message            | checksum depends on                       | independent of                                |
|--------------------|-------------------------------------------|-----------------------------------------------|
| heartbeat request  | bytes 20–21 (confirmed generation) *only* | watering state, link metrics, held generation |
| heartbeat response | byte 5 and bytes 8–9 (generation) *only*  | the clock, bytes 0–2                          |
| schedule blob      | the payload                               | —                                             |

The request checksum being a pure function of the generation was verified across 109 distinct request blobs, the
response across 615.

**They are GF (2)-affine.** Verified on both the heartbeat and the schedule blob: for message differences `a` and `b`,
`ck(a) xor ck(b) = ck(a xor b)`. So each is a linear map plus a constant.

**They are not CRCs.** Given a linear map, consecutive bits of an input byte must be related by a single LFSR shift.
Measured across seven independent bit vectors, no 16-bit polynomial reproduces that relation — under either shift
direction, both bit orderings, and bit-reversed or byte-swapped output. The map is dense, so it is not a word-XOR scheme
either.

Measured linear map, byte 9 of the heartbeat response (bits 0–6):

```
bit0 db16 bit1 9a2f bit2 ef14 bit3 5f04 bit4 dd31 bit5 0221 bit6 3db4
```

Measured linear map, offset 6 (duration) of a 218-byte schedule blob:

```
L (02) baca L (04) bdb9 L (08) 46a6 L (10) d768 L (03) c522 L (36) a9a5 L (4e) acaa L (9c) 872e
```

### What this means for the server

For the **heartbeat**, you may not need the algorithm at all: the response checksum depends only on the flag byte and
the generation, and your server chooses which generation values it uses. A lookup table of observed pairs — 74
generations (`0x08bd`–`0x0914`) are captured — lets the server cycle through known-good values indefinitely. The hub
only needs the generation to *change*, not to increase.

For the **schedule blob** that does not work, since the payload is generated fresh each time. Options, in order of
preference:

1. **Test whether the hub validates it at all.** Serve a blob with a deliberately wrong checksum and see whether the
   schedule still takes effect. This needs only your own server, not Hozelock's, so it is not deadline-bound — and if
   the hub ignores the field, the whole problem disappears.
2. Solve the linear map at the byte offsets that actually vary in practice. For a fixed schedule shape the positions are
   fixed, so this is bounded work rather than 1744 unknowns.
3. Recover the algorithm from firmware.

Do (1) first. It is a few minutes' work against your own implementation and may make (2) and (3) unnecessary.
