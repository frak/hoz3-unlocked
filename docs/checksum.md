# The checksums

Both message types end in two bytes the hub validates — it silently refuses
anything that gets them wrong, confirmed live on 27 August 2026. Both are the
same bespoke GF(2) byte hash. Both are solved and computed in closed form.

Implementations: [`schedule_checksum.py`](../server/hozelock/schedule_checksum.py)
and [`heartbeat_checksum.py`](../server/hozelock/heartbeat_checksum.py).

## What each one covers

| message | checksum depends on | independent of |
|---|---|---|
| heartbeat request (22–23) | bytes 20–21, the confirmed generation | watering state, link metrics, held generation |
| heartbeat response (22–23) | byte 5 (demo flag) and bytes 8–9 (generation) | the clock in bytes 0–2 |
| schedule programme (216–217) | bytes 1–213: the programme and the command flags | the `0xff` padding |

Established from 109 distinct request blobs, 615 response blobs and 560 schedule
samples, with no counterexamples.

## The algorithm

It is GF(2)-affine — `ck(a) xor ck(b) = ck(a xor b)` — and byte-oriented. For
each input byte position `p` there is a 16×8 linear map `M_p`, and adjacent
positions differ by one fixed 16×16 operator `X8`:

    M_{p+1} = X8 · M_p          (zero residual over 266 measured pairs)

which gives the closed form

    ck(blob) = BASE  XOR  sum over p=1..213 of  X8^(p-1) · T(blob[p])

`T` is a fixed linear byte table (its columns are `T1`) and `X8` advances one byte
position. The `0xff` padding is constant, so it folds into `BASE`. The command
flag bytes 212–213 obey the same rule — `X8^211·T1` reproduces their image.

Because `M_p` is *propagated* by `X8` rather than measured per position, the form
holds at any offset and any length. That is what lets it compute the long
midsummer programmes, and it is why the server needs no measured span and no
constraint on the schedule shapes it can express.

It is not a standard CRC: no 16-bit polynomial reproduces the byte relation under
either shift direction, either bit ordering, or bit-reversed and byte-swapped
output — and the output span is only 15 of the 16 bits.

## The heartbeat is the same field

The response checksum's images fall inside that same 15-dimensional span, and
byte 8 sits one position earlier than byte 9:

    byte8 map = X8^340 · byte9 map

`X8` has order 341 on the span, so `X8^340 = X8^-1` — the schedule's
`M_{p+1} = X8·M_p` law again, over a different byte range. Byte 9 is fully
measured, so byte 8's whole table follows:

    ck(flag, gen) = BASE ^ sum byte9-bits·L9 ^ sum byte8-bits·L8 ^ (flag ? Lf : 0)

That covers all 65536 generations, replacing the captured lookup table that once
capped the server at 30 usable generations in normal mode and 45 in demo.
`checksums.py` is retained only as captured reference data; nothing depends on it.

## Verification

| check | result |
|---|---|
| 560 captured schedule programmes | all reproduced |
| 75 captured heartbeat pairs | all reproduced |
| live, novel programme shapes the basis never produced | 7/7, including a 44-byte payload |
| live, generations `0x0c00`–`0x103c` (byte 8 reached only by extrapolation) | 16/16 |

Worked example — the 218-byte programme

```
00 54 2d ac 3c 74 2d ac 3c 74 2d ac 3c 74 2d ac 3c 74 2d ac …
```

padded with `0xff`, flags `00 00 00 00 00`, has checksum `9b f6`.

Re-verify everything:

```bash
cd server && uv run python test_checksum.py
```

## Where the evidence lives

| what | where |
|---|---|
| schedule samples, one JSON object per line | `data/checksum-samples.jsonl` |
| live heartbeat pairs that broke the byte-8/9 coupling | `data/hb_samples_live.jsonl` |
| live validation of the heartbeat form | `data/hb_validate.json` |
| captured heartbeat pairs | `server/hozelock/checksums.py` |
| original captures and pcaps | `data/captures/` |
| collection and solving tool | `capture/collect-checksums.py` |
| cloud API driver | `capture/hozelock-api.py` |

The constants were solved from these samples, and the real service — the only
oracle that can produce more — shuts down at the end of April 2027. The captured
data cannot be regenerated after that.
