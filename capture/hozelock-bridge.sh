#!/bin/bash
# Transparent L2 bridge for tapping the Hozelock hub's uplink.
# Usage: sudo ./hozelock-bridge.sh eth0 eth1
set -euo pipefail

UPLINK="${1:?usage: $0 <uplink-iface-to-router> <hub-iface>}"
HUBIF="${2:?usage: $0 <uplink-iface-to-router> <hub-iface>}"

# NetworkManager will otherwise DHCP on the members and fight the bridge
if command -v nmcli >/dev/null; then
  nmcli device set "$UPLINK" managed no || true
  nmcli device set "$HUBIF"  managed no || true
fi

ip link del br0 2>/dev/null || true
ip link add name br0 type bridge
# forward_delay 0 + no STP: the hub must not lose 30s of link on every bounce
ip link set br0 type bridge forward_delay 0
ip link set br0 type bridge stp_state 0

for i in "$UPLINK" "$HUBIF"; do
  ip addr flush dev "$i"
  ip link set "$i" promisc on
  ip link set "$i" master br0
  ip link set "$i" up
done

# No IP on br0 — the Pi stays invisible; manage it over wlan0
ip link set br0 up

# br_netfilter would push bridged frames through iptables and can silently drop them
modprobe -r br_netfilter 2>/dev/null || \
  sysctl -w net.bridge.bridge-nf-call-iptables=0 \
         -w net.bridge.bridge-nf-call-ip6tables=0 \
         -w net.bridge.bridge-nf-call-arptables=0 >/dev/null 2>&1 || true

bridge link
echo "bridge up: $UPLINK <-> $HUBIF"
