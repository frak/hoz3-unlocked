# Hozelock Cloud Controller — traffic capture rig

Hozelock shuts the Cloud Controller service down at the end of April 2027. This
document covers step 1 of replacing it: recording what the hub actually says to
Hozelock, while the real service is still alive to answer.

## Background: what talks to what

Three hops, only one of which is documented:

1. **Tap controller ↔ hub** — proprietary sub-GHz radio (~868 MHz). The controller
   wakes and polls the hub every 20 minutes (1 minute in "demo" mode). Not
   practically replaceable, so **the hub has to stay in the system**.
2. **Hub ↔ hoz3.com** — the hub sits on Ethernet at the router. **Undocumented.**
   This is what we're capturing.
3. **App ↔ hoz3.com** — `https://hoz3.com/restful/support/hubs/{hubId}/...`, no
   auth at all. Documented at <https://github.com/martynjsimpson/HozelockAPI>, and
   what the Home Assistant REST sensors already drive.

Replacing the cloud means making the hub believe our box is hoz3.com, so hop 2 is
what has to be understood — and nobody has published a capture of it.
[Reference discussion.](https://community.home-assistant.io/t/having-hozelock-cloud-controller-kit-intergration/55694/8)

## Hardware

- Raspberry Pi with onboard Ethernet (3B+ or later; a Zero 2 W with two USB NICs
  also works)
- **One USB Ethernet adapter** — two NICs are required. 100 Mbit is fine, the hub
  isn't gigabit.
- A second Ethernet cable
- A USB stick or roomy SD card for the pcaps

Manage the Pi over **Wi-Fi**. The bridge deliberately carries no IP address, so
SSHing in over the wired path locks you out the instant the script runs.

## Physical rewiring

Before: `hub —— router`

After: `hub —— [USB NIC] Pi [onboard eth0] —— router`

The Pi becomes an invisible pass-through. The hub still gets its normal DHCP lease
from the router, still reaches the internet, and the Hozelock app keeps working.
Nothing about the hub's behaviour changes — that's the point.

## Setup

### 1. Identify the interfaces

Plug the USB NIC in and run `ip link`. Onboard is usually `eth0`; the dongle shows
as `eth1` or a predictable name like `enx00e04c680001`. Unplug the dongle and see
which entry disappears if it's ambiguous.

### 2. Install

```bash
sudo apt update && sudo apt install -y tcpdump tshark bridge-utils
sudo mkdir -p /var/captures
```

### 3. Bring up the bridge

```bash
sudo ./capture/hozelock-bridge.sh eth0 eth1
```

The three things in that script that matter:

- unmanages both NICs in NetworkManager, which would otherwise DHCP on the members
  and tear the bridge apart;
- `forward_delay 0` with STP off, so the hub doesn't eat a 30-second blackhole on
  every link bounce;
- unloads `br_netfilter`, which pushes bridged frames through iptables and can drop
  them silently.

### 4. Verify before trusting it

```bash
bridge link                    # both members should read "state forwarding"
sudo tcpdump -i br0 -n -c 20   # expect the hub's DHCP/DNS/ARP
```

Confirm the hub is still online in the Hozelock app. If it isn't, stop and debug —
a half-working bridge yields a pcap full of retransmits and nothing else.

### 5. Find the hub's MAC

```bash
sudo tcpdump -i br0 -n -e -c 50 'port 67 or arp'
```

Or read it from the router's DHCP lease table. Filter on the MAC, not the IP: the
IP can change on lease renewal mid-capture and silently break the filter.

### 6. Start the long capture

Edit `capture/hozelock-capture.service`, replace `AA:BB:CC:DD:EE:FF` with the hub's MAC,
then:

```bash
sudo cp capture/hozelock-capture.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hozelock-capture
```

Hourly files. `-U` flushes per packet, so a yanked power cable doesn't cost the last
hour. `-W 720` is a ceiling, not automatic reaping: the count resets whenever tcpdump
restarts and strftime filenames never collide. For a longer run add a
`find /var/captures -name 'hub-*.pcap' -mtime +30 -delete` timer.

### 7. Survive reboot

A power cut otherwise kills the watering *and* the capture.

```bash
sudo cp capture/hozelock-bridge.sh /usr/local/sbin/
sudo chmod 755 /usr/local/sbin/hozelock-bridge.sh
```

`capture/hozelock-bridge.service` runs it at boot. If your dongle isn't `eth1`, change
the name in **both** the `ExecStart=` line and the two `sys-subsystem-net-devices-*`
lines — systemd escapes dots and dashes, so `enx00e04c680001` is fine as-is but a name
containing `-` needs `systemd-escape --path`.

```bash
sudo cp capture/hozelock-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hozelock-bridge
```

Check it, then reboot and check again — boot order is the only thing this unit is for,
and the one thing running it by hand doesn't test:

```bash
systemctl status hozelock-bridge   # "active (exited)" is correct for oneshot
bridge link                        # both members "state forwarding"
sudo reboot
```

`capture/hozelock-capture.service` declares `Requires=`/`After=` on the bridge, so the
capture won't start against a missing `br0` — if you enabled the capture at step 6
before creating the bridge unit, re-run `daemon-reload` to pick that up.

## First analysis pass (after ~a day)

`tshark -r` takes a single file, so the hourly rotation needs `mergecap` in front of it:

```bash
alias hubcap='mergecap -w - /var/captures/hub-*.pcap | tshark -r -'
```

```bash
# What hostnames does the hub look up?
hubcap -Y dns.flags.response==0 -T fields -e dns.qry.name | sort -u

# What does it connect to, on what ports?
hubcap -Y 'tcp.flags.syn==1 && tcp.flags.ack==0' \
  -T fields -e ip.dst -e tcp.dstport | sort | uniq -c | sort -rn

# The traffic itself - cleartext HTTP, as it turned out.
hubcap -Y http.request -T fields -e http.host -e http.request.uri
```

## Extracting data for analysis

`tshark` writes a TSV that [decode.py](../capture/decode.py) understands:

```bash
mergecap -w - /var/captures/hub-*.pcap | tshark -r - -Y http \
  -T fields -e frame.time_epoch -e ip.src -e http.request.uri -e http.file_data \
  > /var/captures/hb.tsv
```

**Prefer the raw extraction.** tshark's HTTP dissector silently fails to pair some
responses — 14 schedule blobs against 26 for a raw sentinel match, so nearly half the
samples were being lost:

```bash
mergecap -w - /var/captures/hub-*.pcap | tshark -r - \
  -Y 'tcp contains "#!hb="' -T fields -e frame.time_epoch -e ip.src -e tcp.payload \
  > /var/captures/hb-raw.tsv
```

The symptom is a `tap/0` request with no response beside it — re-extract with the raw
form rather than assuming the hub got nothing.

Copy it off the Pi and decode:

```bash
python3 capture/decode.py --timeline data/captures/hb.tsv --events data/events.log --diff
python3 capture/decode.py --tap data/captures/hb.tsv --anchor 06:00
```

`--diff` prints only exchanges where the blob changed, which is what you want —
most heartbeats are identical.

### Keep an events log

The pcap is worthless without knowing what you did and when. Log every deliberate
action with a timestamp, on the Pi so the clock matches the capture:

```bash
mark() { echo "$(date -Is) $*" | sudo tee -a /var/captures/events.log; }
mark "waterNow zone 1 via app"
```

One change at a time — two at once make the byte diff ambiguous and you have to redo
the experiment. `decode.py --events` interleaves this file with the traffic.

`tap/0` is fetched rarely, sometimes not for 14 hours, so schedule changes need a long
wait or a hub power-cycle before they reach the wire.

## What was found

**The protocol is fully documented in [protocol.md](protocol.md)** — endpoints, both
blob layouts, the schedule encoding, and what a replacement server has to reproduce.

In short: cleartext HTTP, no auth, no TLS. Two GET endpoints. State travels up in a
24-byte blob, commands come down via a generation counter, and schedules are a list
of (duration, interval) pairs with no absolute times in them at all. Both checksums
are solved — [checksum.md](checksum.md).

## Things to watch for

- **Firmware-update endpoints.** `hoz3.com` was the only hostname resolved across the
  capture, but an update endpoint that only appears at boot would be easy to miss.
  Re-check DNS across a hub reboot before cutting the hub off from the internet.
- **Check ports other than 80.** Only port 80 was seen. Worth confirming against a
  longer capture before assuming the two endpoints are the whole story.
- **Idle capture answers nothing.** The blobs only yield to labelled state changes.
  A month of untouched pcap is near-worthless; an hour of deliberate experiments is
  worth all of it.
- **Two different poll intervals, don't conflate them.** The controller↔hub radio
  runs at 20 minutes and sets the *watering* latency floor. The hub↔cloud heartbeat
  is separate, at ~16 minutes normally and ~7 seconds in demo mode.

## What comes next

Decoding is done; the replacement server is in [server.md](server.md) and the
cutover runbook in [live-test.md](live-test.md).

Keep the rig assembled until the replacement is running against the real hub. Being
able to diff your server's responses against Hozelock's is worth more than any
amount of re-reading the spec — and it is also how more checksum samples get
collected, which is only possible while the real service is alive.
