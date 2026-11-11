#!/bin/sh
# SPDX-License-Identifier: MIT
#
# udhcpc-tsf.sh - udhcpc hook passed by wifi-sta.sh via `-s`.
#
# This file was missing from the deployed package: wifi-sta.sh line ~163 calls
# `udhcpc -s "$HERE/udhcpc-tsf.sh"`, and without it udhcpc exits immediately,
# the script warns "could not be started", waits DHCP_TIMEOUT seconds and then
# falls back to a MAC-derived 192.168.4.2xx address that is not on the real
# uplink subnet.
#
# Both boards ship busybox's stock /usr/share/udhcpc/default.script, which
# already handles deconfig / renew / bound (address, router, DNS). Delegate to
# it and additionally publish the leased address for the TSF state file.
#
# udhcpc exports: $1 = state (deconfig|renew|bound|nak|leasefail), plus
# $interface $ip $mask $router $broadcast $dns $domain ...

case "$1" in
renew|bound)
    if [ -n "${TSF_STATE_DIR:-}" ] && [ -n "${ip:-}" ]; then
        printf '%s\n' "$ip" > "$TSF_STATE_DIR/wifi_ip" 2>/dev/null || true
    fi
    ;;
esac

if [ -x /usr/share/udhcpc/default.script ]; then
    exec /usr/share/udhcpc/default.script "$@"
fi

# Minimal fallback for a rootfs without the stock busybox script.
[ -n "${interface:-}" ] || exit 0
case "$1" in
deconfig)
    ip addr flush dev "$interface" scope global 2>/dev/null || true
    ip link set "$interface" up 2>/dev/null || true
    ;;
renew|bound)
    [ -n "${ip:-}" ] || exit 0
    ip addr flush dev "$interface" scope global 2>/dev/null || true
    ip addr add "$ip/${mask:-24}" dev "$interface" 2>/dev/null || true
    ip link set "$interface" up 2>/dev/null || true
    for r in ${router:-}; do
        ip route add default via "$r" dev "$interface" 2>/dev/null || true
    done
    ;;
esac
exit 0
