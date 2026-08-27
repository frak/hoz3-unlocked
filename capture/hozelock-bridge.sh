#!/bin/bash
# Transparent L2 bridge for tapping the Hozelock hub's uplink.
# Usage: sudo ./hozelock-bridge.sh <uplink-iface> <hub-iface> [--dhcp]
#
# --dhcp gives br0 an address so the Pi itself has a wired route. Without it the
# bridge carries no IP and the Pi's own traffic goes over wifi, which matters if
# anything on the Pi needs the internet (the checksum collector does).
set -euo pipefail

UPLINK="${1:?usage: $0 <uplink-iface-to-router> <hub-iface> [--dhcp]}"
HUBIF="${2:?usage: $0 <uplink-iface-to-router> <hub-iface> [--dhcp]}"
WANT_DHCP="${3:-}"

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

ip link set br0 up

# By default br0 carries no IP and the Pi stays invisible; manage it over wlan0.
if [ "$WANT_DHCP" = "--dhcp" ]; then
  if command -v dhclient >/dev/null; then
    dhclient -v br0 || echo "dhclient failed; br0 has no address" >&2
  elif command -v dhcpcd >/dev/null; then
    dhcpcd -n br0 || echo "dhcpcd failed; br0 has no address" >&2
  else
    echo "no DHCP client found; install isc-dhcp-client" >&2
  fi
  if ip -4 addr show dev br0 | grep -q 'inet '; then
    # An address is not enough: wifi's default route usually has a lower metric,
    # so outbound traffic keeps using it. Make the wired route win.
    GW=$(ip -4 route show default dev br0 | awk '{print $3; exit}')
    if [ -n "$GW" ]; then
      ip route replace default via "$GW" dev br0 metric 50
    fi
    VIA=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')
    echo "br0 has a wired address; outbound traffic uses ${VIA:-unknown}"
  else
    echo "br0 has no address - the Pi will use wifi" >&2
  fi
fi

# br_netfilter would push bridged frames through iptables and can silently drop them
modprobe -r br_netfilter 2>/dev/null || \
  sysctl -w net.bridge.bridge-nf-call-iptables=0 \
         -w net.bridge.bridge-nf-call-ip6tables=0 \
         -w net.bridge.bridge-nf-call-arptables=0 >/dev/null 2>&1 || true

bridge link
echo "bridge up: $UPLINK <-> $HUBIF"
