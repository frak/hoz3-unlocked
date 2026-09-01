# Live test: running the hub against our server

The cutover runbook: the hub stops talking to Hozelock and talks to us instead.
The server runs **on the Pi, on port 80**, and the hub sits on its own network
segment behind the Pi so nothing else is affected.

```
hub —— [eth1] Pi —— [eth0] router
              │
              └── dnsmasq: hoz3.com → the Pi
                  hozelock-server on :80
```

Two things to know before starting:

- **The Hozelock phone app stops being useful.** It reports what the hub tells
  *Hozelock*, and the hub will be talking to us. Expected, and reversible.
- **Use demo mode throughout.** The tap controller polls the hub every 20 minutes
  normally, 1 minute in demo mode. It expires after an hour; turn it back on as
  needed. Without it every test costs 20 minutes.

Work through the steps in order. Each ends with something to check before moving on.

---

## 1. Install what the Pi needs

```bash
sudo apt update
sudo apt install -y dnsmasq iptables mosquitto-clients
curl -LsSf https://astral.sh/uv/install.sh | sh
```

`mosquitto-clients` is the CLI tools only — the broker stays where it is.
`iptables` is not installed by default on Debian 13; the routing script needs it.

**Check:** `uv --version`, `dnsmasq --version` and `iptables --version` all respond.

---

## 2. Get the code onto the Pi

```bash
git clone <your-repo> hozelock
cd hozelock/server
uv sync
```

`uv` fetches Python 3.14 itself, so the Pi's system Python does not matter.

**Check:** `uv run python -V` prints `Python 3.14.x`.

---

## 3. Configure

Copy the example and edit it — `config.yaml` is untracked, so your
credentials and coordinates stay out of the repo:

```bash
cp server/config.example.yaml server/config.yaml
```

```yaml
hub_id: ge7si1
port: 80

site:
  latitude: 51.5          # your actual coordinates - sunrise/sunset depend on them
  longitude: -0.1
  timezone: Europe/London

restrict_generations: false   # both checksums are computed; see docs/checksum.md

schedule:
  - at: sunrise
    duration_min: 45
  - at: sunset
    duration_min: 60

mqtt:
  host: 192.168.1.6       # your Mosquitto container
  port: 1883
  username: xxxx
  password: xxxx
```

`hub_id` is **not** the ID the app shows — it is the one the hub puts in its URL
path. Get it from the capture:

```bash
grep -o '/notify/[^/]*/' ../data/captures/hb.tsv | sort -u
```

Getting this wrong means every request 404s and the hub never gets an answer. The
server logs `hub id mismatch` if it happens.

**Check:** `hub_id` matches that output, and the coordinates are yours rather than
the placeholder London ones.

---

## 4. Start the server and prove it works locally

Install it as a service so it survives a closed terminal and comes back after a reboot.
Running it in the foreground is the easiest way to lose an evening to "the hub cannot
connect" when the real answer is that the terminal closed.

```bash
sudo cp hozelock-server.service /etc/systemd/system/
sudoedit /etc/systemd/system/hozelock-server.service   # check User= and the paths
sudo systemctl daemon-reload
sudo systemctl enable --now hozelock-server
```

Follow the log in its own terminal — you want this for the rest of the test:

```bash
journalctl -u hozelock-server -f
```

`AmbientCapabilities=CAP_NET_BIND_SERVICE` lets it bind port 80 without running as
root.

```bash
curl "http://localhost/notify/ge7si1/tap/0/?hb=AgEIG-IAAgAAAACUhAUAAAi9AAAIvUlC"
```

**Check:** `sudo ss -lntp | grep :80` shows it listening, the curl returns
`<html>...#!hb=AF...` — a long base64 blob ending in `.` — and the log shows a
`tap/0 fetch` line.

If port 80 is empty nothing else here works: the hub gets a DHCP lease, fails to reach
anything, and restarts its network every 45 seconds.

---

## 5. Prove the MQTT side works

```bash
mosquitto_sub -h 192.168.1.6 -u xxxx -P xxxx -v -t 'homeassistant/+/hozelock/#' -t 'hozelock/#'
```

**Check:** six retained `.../config` messages and `hozelock/hozelock/available` reading
`online`. In Home Assistant a device called **Hozelock Cloud Controller** appears with
six entities. If the topics are there but HA shows nothing, the problem is HA's MQTT
integration, not the server.

Leave this subscription running — it is the clearest view of what the server is doing.

---

## 6. Stop the old capture rig

The bridge services conflict with the routing setup and must go first.

```bash
sudo systemctl disable --now hozelock-capture.service hozelock-bridge.service
sudo ip link del br0
```

Stopping a `oneshot` unit does not undo its work, hence the explicit `br0` delete.

**Check:** `ip link show br0` reports "does not exist", and
`ip -4 addr show eth0` shows a normal `192.168.1.x` address.

---

## 7. Rewire and route

Physically: hub into the Pi's USB NIC (`eth1`), Pi's onboard NIC (`eth0`) to the
router. Same cabling as the capture bridge.

```bash
cd ~/hozelock
sudo ./capture/hozelock-route.sh eth0 eth1 local
```

This gives the hub its own subnet with the Pi as gateway and resolver, NATs it out
to the LAN, and points `hoz3.com` at the Pi.

**Check:** the script prints `hoz3.com -> 10.0.9.1:80 -> 10.0.9.1:80` and exits 0.
If it complains that `eth0` has no address, step 6 was skipped.

---

## 8. Start capturing

Worth recording — this is the run you will want to look back at when something behaves
oddly.

```bash
sudo mkdir -p /var/captures
sudo tcpdump -i eth1 -s 0 -U -w /var/captures/livetest-%Y%m%d-%H%M%S.pcap \
  -G 3600 'port 80 or port 53' &
```

**Check:** a `livetest-*.pcap` file appears and grows.

---

## 9. Cut over

**Power-cycle the hub.**

This matters more than it looks. A freshly booted hub reports `held=0000`, so whatever
generation we serve differs from what it holds and it fetches immediately. Cut over
without rebooting and the hub still holds a generation from the real service —
`0x0914` or higher, above ours — and it may sit there doing nothing, looking exactly
like a protocol failure.

---

## 10. Confirm the hub accepted us

Watch the server log. Within a minute or two of the hub booting:

```
heartbeat: state=idle held=None generation=08bd     ← hub found us
tap/0 fetch: generation=08bd pending=0              ← hub collected the programme
heartbeat: state=idle held=2237 generation=08bd     ← hub is holding our programme
```

**Check:** `held` settles at our generation (2237 decimal = `0x08bd`). That is the
hub telling us it accepted the blob.

If it fetches `tap/0` over and over and `held` never settles, it is rejecting what
we send. The hub does validate both checksums, so that is the first thing to
suspect — but they are computed rather than replayed now, and
`uv run python test_checksum.py` will say whether the algorithm still reproduces
every captured sample.

---

## 11. Test manual watering

Put the hub in demo mode first. Then:

```bash
mosquitto_pub -h 192.168.1.6 -u xxxx -P xxxx -t hozelock/hozelock/water_now/set -m PRESS
```

**Check:** within about a minute the tap opens, and the server log shows
`state=manual`. Then stop it:

```bash
mosquitto_pub -h 192.168.1.6 -u xxxx -P xxxx -t hozelock/hozelock/stop/set -m PRESS
```

Do this via MQTT before trying the Home Assistant button: it separates "the server
works" from "the HA integration works", so a failure tells you which half to look at.

---

## 12. Test a scheduled watering

Set a schedule a few minutes ahead in `config.yaml`, restart the server, and wait.

```yaml
schedule:
  - at: "18:30"
    duration_min: 2
```

**Check:** the tap opens at 18:30, and the log shows `state=scheduled` rather than
`manual`.

This is the only step that tests the **programme epoch**, which was inferred from the
captures rather than proven. A consistent offset from the intended time — always 25
minutes late, say — is that, and it is a fixable constant rather than a broken design.

---

## Rollback

Unplug the hub from the Pi and connect it back to the router; it returns to the real
service, which exists until April 2027. To undo the Pi's side as well:

```bash
sudo rm /etc/dnsmasq.d/hozelock.conf
sudo systemctl restart dnsmasq
```

---

## If something does not work

| symptom | most likely cause |
|---|---|
| hub never appears in the log | DNS — check `hoz3.com` resolves to `10.0.9.1` from the hub's segment |
| hub appears but never fetches `tap/0` | cut over without a power cycle (step 9) |
| `tap/0` fetched repeatedly, `held` never settles | hub is rejecting the blob — run `test_checksum.py` |
| tap opens and never closes | manual watering has no duration field; send Stop explicitly |
| watering fires at a consistent offset | programme epoch is wrong — see step 12 |
| entities missing in HA, topics present | HA's MQTT integration, not the server |
| hub gets a DHCP lease then loops every ~45s | nothing listening on port 80 — check the service is running |
| server exits on start | port 80 already in use |
