#!/bin/sh
# Network isolation for Arena challenge containers (run on the RUNNER host as root).
#
#   sh docker-user.sh            install / refresh rules (idempotent)
#   sh docker-user.sh --remove   remove Arena rules
#
# Two places must be filtered:
#  * FORWARD (Docker's DOCKER-USER chain): container -> other networks/internet
#  * INPUT: container -> services on the runner host itself (sshd, Docker API,
#    any database or cache). DOCKER-USER does NOT see this traffic.
#
# Policy for traffic originating FROM challenge containers:
#   * allow packets of connections initiated by players (ESTABLISHED,RELATED)
#   * DROP anything to the host (INPUT), to RFC1918 / CGNAT / link-local
#     (cloud metadata 169.254.169.254) / loopback ranges and ARENA_PLATFORM_IPS
#   * DROP all other egress unless ARENA_ALLOW_INTERNET_EGRESS=1
#
# Containers live on per-instance bridges carved from Docker's default pools
# (172.16.0.0/12); override with ARENA_CONTAINER_NETS (space separated).
set -eu

NETS="${ARENA_CONTAINER_NETS:-172.16.0.0/12}"
PLATFORM_IPS="${ARENA_PLATFORM_IPS:-}"
ALLOW_INTERNET="${ARENA_ALLOW_INTERNET_EGRESS:-0}"

remove() {
  iptables -D DOCKER-USER -j ARENA-EGRESS 2>/dev/null || true
  iptables -D INPUT -j ARENA-INPUT 2>/dev/null || true
  for chain in ARENA-EGRESS ARENA-INPUT; do
    iptables -F "$chain" 2>/dev/null || true
    iptables -X "$chain" 2>/dev/null || true
  done
}

if [ "${1:-}" = "--remove" ]; then
  remove
  echo "Arena firewall rules removed"
  exit 0
fi

iptables -N ARENA-EGRESS 2>/dev/null || iptables -F ARENA-EGRESS
iptables -N ARENA-INPUT 2>/dev/null || iptables -F ARENA-INPUT
iptables -C DOCKER-USER -j ARENA-EGRESS 2>/dev/null || iptables -I DOCKER-USER 1 -j ARENA-EGRESS
iptables -C INPUT -j ARENA-INPUT 2>/dev/null || iptables -I INPUT 1 -j ARENA-INPUT

for net in $NETS; do
  # --- host services (INPUT) ---
  iptables -A ARENA-INPUT -s "$net" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  iptables -A ARENA-INPUT -s "$net" -j DROP
  # --- forwarded traffic (DOCKER-USER) ---
  iptables -A ARENA-EGRESS -s "$net" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  for dst in 169.254.0.0/16 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 127.0.0.0/8 $PLATFORM_IPS; do
    iptables -A ARENA-EGRESS -s "$net" -d "$dst" -j DROP
  done
  if [ "$ALLOW_INTERNET" != "1" ]; then
    iptables -A ARENA-EGRESS -s "$net" -j DROP
  fi
done
iptables -A ARENA-EGRESS -j RETURN
iptables -A ARENA-INPUT -j RETURN
echo "Arena firewall installed for: $NETS (internet egress allowed: $ALLOW_INTERNET)"
