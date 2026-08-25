#!/bin/bash
# Put the hub on its own segment behind the Pi, so hoz3.com can be redirected
# for the hub alone without touching the rest of the network.
#
# Usage: sudo ./hozelock-route.sh <uplink-iface> <hub-iface> <target>
#   target = "local"          server runs on this Pi, port 80
#          | "local:8080"     server runs on this Pi, another port
#          | "10.0.0.5:8080"  server runs elsewhere, any port
#
# The hub always connects to hoz3.com:80 and that is not configurable, so the
# port is rewritten here. A host with port 80 already taken is fine.
set -euo pipefail

UPLINK="${1:?usage: $0 <uplink-iface> <hub-iface> <target>}"
HUBIF="${2:?usage: $0 <uplink-iface> <hub-iface> <target>}"
TARGET="${3:?usage: $0 <uplink-iface> <hub-iface> <target>}"

SUBNET="10.0.9"
GATEWAY="${SUBNET}.1"
LEASE_RANGE="${SUBNET}.50,${SUBNET}.150,12h"

# Check everything up front: this script reconfigures the interface before it
# reaches the firewall rules, and a half-built router is worse than none.
missing=()
command -v dnsmasq >/dev/null  || missing+=(dnsmasq)
command -v iptables >/dev/null || missing+=(iptables)   # not installed by default on Debian 13
if [ ${#missing[@]} -gt 0 ]; then
  echo "missing: ${missing[*]}" >&2
  echo "  sudo apt install -y ${missing[*]}" >&2
  exit 1
fi

TARGET_HOST="${TARGET%%:*}"
TARGET_PORT="${TARGET##*:}"
[ "$TARGET_PORT" = "$TARGET" ] && TARGET_PORT=80
[ "$TARGET_HOST" = "local" ] && TARGET_HOST="$GATEWAY"

# A leftover bridge from the capture rig would hold the interface
ip link del br0 2>/dev/null || true

# The bridge script left the uplink unmanaged and without an address. Routing
# needs it back on the LAN, or the hub has nothing to be NATed to.
if command -v nmcli >/dev/null; then
  nmcli device set "$UPLINK" managed yes || true
  nmcli device set "$HUBIF" managed no || true
fi
for _ in $(seq 20); do
  ip -4 addr show dev "$UPLINK" | grep -q 'inet ' && break
  sleep 1
done
ip -4 addr show dev "$UPLINK" | grep -q 'inet ' || {
  echo "$UPLINK has no IPv4 address - bring it up before routing" >&2
  exit 1
}

ip addr flush dev "$HUBIF"
ip addr add "${GATEWAY}/24" dev "$HUBIF"
ip link set "$HUBIF" up

sysctl -qw net.ipv4.ip_forward=1

add_rule() {  # <table> <chain> <rule...>
  local table=$1 chain=$2; shift 2
  iptables -t "$table" -C "$chain" "$@" 2>/dev/null || iptables -t "$table" -A "$chain" "$@"
}

add_rule nat POSTROUTING -o "$UPLINK" -j MASQUERADE
add_rule filter FORWARD -i "$HUBIF" -o "$UPLINK" -j ACCEPT
add_rule filter FORWARD -i "$UPLINK" -o "$HUBIF" -m state --state RELATED,ESTABLISHED -j ACCEPT

if [ "$TARGET_HOST" != "$GATEWAY" ] || [ "$TARGET_PORT" != "80" ]; then
  add_rule nat PREROUTING -i "$HUBIF" -p tcp -d "$GATEWAY" --dport 80 \
    -j DNAT --to-destination "${TARGET_HOST}:${TARGET_PORT}"
  # Without this the server would reply straight to 10.0.9.x, which it has no
  # route to.
  add_rule nat POSTROUTING -p tcp -d "$TARGET_HOST" --dport "$TARGET_PORT" -j MASQUERADE
fi

cat > /etc/dnsmasq.d/hozelock.conf <<EOF
interface=${HUBIF}
bind-interfaces
dhcp-range=${LEASE_RANGE}
dhcp-option=option:router,${GATEWAY}
dhcp-option=option:dns-server,${GATEWAY}
address=/hoz3.com/${GATEWAY}
EOF

systemctl restart dnsmasq
echo "hub segment up on ${HUBIF} (${GATEWAY})"
echo "hoz3.com -> ${GATEWAY}:80 -> ${TARGET_HOST}:${TARGET_PORT}"
echo "power-cycle the hub so it takes a new lease and reports held=0000"
