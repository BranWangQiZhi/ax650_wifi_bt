#!/bin/sh
# SPDX-License-Identifier: MIT
#
# wifi-sta.sh - bring this board up as a Wi-Fi station (ESP32-C5 via SDIO).
#
# The TSF side needs two things from this script and nothing else:
#   1. ethsta0 associated with the AX650 AP, and
#   2. an IPv4 address on ethsta0, written to $STATE_DIR/wifi.env.
#
# IP allocation is deliberately split in two:
#   - the address comes from the AX650 AP's own DHCP server (192.168.4.10-200),
#     so two AX637 boards never collide;
#   - the *identity* comes from the beacon (see tsf_peer_beacon), so the
#     controller works even when DHCP hands out a different address after a
#     reboot or a lease expiry.
#
# A device with $TSF_ROOT/device-id containing 1..9 additionally asks DHCP for
# 192.168.4.1<id> (e.g. 192.168.4.11 / .12). That is a request, not a
# reservation: if the address is taken, DHCP assigns another one and everything
# still works because the beacon carries the real address.
#
# Configuration file (optional): $TSF_ROOT/wifi.conf or /etc/tsf-wifi.conf
#   WIFI_SSID=AX650-SDIO-AP
#   WIFI_PASS=ax650sdio
#   WIFI_CHANNEL=36
#   WIFI_STATIC_IP=192.168.4.12      # optional, skips DHCP entirely
#   TSF_DEVICE_ID=2                  # optional, same as $TSF_ROOT/device-id
#
# Environment overrides: WIFI_SSID WIFI_PASS WIFI_CHANNEL WIFI_STATIC_IP
#                        WIFI_UP_TIMEOUT DHCP_TIMEOUT WIFI_SKIP_MODULE
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/common.sh"

# Environment wins over the file - see read_wifi_conf() in common.sh.
read_wifi_conf

WIFI_SSID="${WIFI_SSID:-AX650-SDIO-AP}"
WIFI_PASS="${WIFI_PASS:-ax650sdio}"
WIFI_CHANNEL="${WIFI_CHANNEL:-36}"
WIFI_UP_TIMEOUT="${WIFI_UP_TIMEOUT:-90}"
DHCP_TIMEOUT="${DHCP_TIMEOUT:-45}"
WIFI_SKIP_MODULE="${WIFI_SKIP_MODULE:-0}"
LOG="$LOG_DIR/wifi-sta.log"

ensure_dirs

log "wifi-sta: SSID=$WIFI_SSID channel=$WIFI_CHANNEL if=$WIFI_IF"

# ---------------------------------------------------------------------------
# Already up? Then this is a no-op, which is what the boot hook and the TSF
# supervisor both want: they call it unconditionally.
# ---------------------------------------------------------------------------
if [ -n "$(iface_ipv4 "$WIFI_IF")" ] && have_comm wifi-role; then
    log "wifi-sta: already associated with $(iface_ipv4 "$WIFI_IF") on $WIFI_IF"
    write_state "$STATE_DIR/wifi.env" \
        "WIFI_ROLE=sta" "WIFI_IF=$WIFI_IF" "WIFI_IP=$(iface_ipv4 "$WIFI_IF")" \
        "WIFI_GW=$WIFI_GW" "WIFI_DEVICE_ID=$(tsf_device_id)"
    exit 0
fi

# ---------------------------------------------------------------------------
# 1. Driver
# ---------------------------------------------------------------------------
if [ "$WIFI_SKIP_MODULE" != "1" ]; then
    ensure_wifi_module || die "Wi-Fi driver is not available (see the WARN lines above)"
fi

# ---------------------------------------------------------------------------
# 2. Stop any previous role instance. A stale AP instance on an STA board would
#    keep the radio in the wrong mode.
# ---------------------------------------------------------------------------
if have_comm wifi-role; then
    log "wifi-sta: stopping previous wifi-role"
    kill_comm wifi-role TERM
    wait_comm_gone wifi-role 10 || kill_comm wifi-role KILL
    sleep 1
fi

# ---------------------------------------------------------------------------
# 3. Start the role
# ---------------------------------------------------------------------------
: > "$LOG"
log "wifi-sta: starting wifi-role (EH_MODE=sta EH_EXTERNAL_IP=1)"
EH_MODE=sta \
EH_EXTERNAL_IP=1 \
EH_AP_CHANNEL="$WIFI_CHANNEL" \
EH_STA_SSID="$WIFI_SSID" \
EH_STA_PASSWORD="$WIFI_PASS" \
    "$WIFI_APP" >> "$LOG" 2>&1 &
APP_PID=$!

# A missing C5 answers with a very specific line and never recovers, so stop on
# it instead of burning the whole timeout.
_left="$WIFI_UP_TIMEOUT"
while [ "$_left" -gt 0 ]; do
    if grep -q 'sta up\.' "$LOG" 2>/dev/null; then
        break
    fi
    if grep -q 'no response uid=1 msg_id=278' "$LOG" 2>/dev/null; then
        tail -5 "$LOG" >&2
        die "the C5 is not responding: reset it and re-enumerate SDIO (docs/wifi/04-FAQ.md Q4)"
    fi
    kill -0 "$APP_PID" 2>/dev/null || {
        tail -10 "$LOG" >&2
        die "wifi-role exited before association"
    }
    sleep 1
    _left=$((_left - 1))
done

if ! grep -q 'sta up\.' "$LOG" 2>/dev/null; then
    tail -10 "$LOG" >&2
    die "STA did not associate within ${WIFI_UP_TIMEOUT}s (SSID=$WIFI_SSID channel=$WIFI_CHANNEL).
       Check that the AX650 AP is running, on the same band, and with the same password."
fi
log "wifi-sta: associated"
grep 'sta up\.' "$LOG" | tail -1

# ---------------------------------------------------------------------------
# 4. Address
# ---------------------------------------------------------------------------
ip link set "$WIFI_IF" up 2>/dev/null || true

requested="$(tsf_requested_ipv4)"

# Static is only used when asked for explicitly. A bare device id still goes
# through DHCP first, because a request the server cannot honour must not leave
# the board with an address someone else owns.
use_static=0
[ -n "${WIFI_STATIC_IP:-}" ] && use_static=1
[ "${WIFI_FORCE_STATIC:-0}" = "1" ] && use_static=1

if [ "$use_static" = "1" ]; then
    static_ip="${WIFI_STATIC_IP:-}"
    if [ -z "$static_ip" ]; then
        static_ip="$(tsf_requested_ipv4)"
        [ -n "$static_ip" ] || \
            die "WIFI_FORCE_STATIC=1 needs WIFI_STATIC_IP or a numeric device-id (1..9)"
    fi
    log "wifi-sta: static address $static_ip (DHCP skipped)"
    ip addr flush dev "$WIFI_IF" scope global 2>/dev/null || true
    ip addr add "$static_ip/24" dev "$WIFI_IF" || die "cannot assign $static_ip"
    ip link set "$WIFI_IF" up
else
    command -v udhcpc >/dev/null 2>&1 || \
        die "udhcpc not found; set WIFI_STATIC_IP=... to configure the address manually"

    # Kill a udhcpc left by a previous run - but only the one on this interface,
    # never the wired eth0 lease.
    for _pid in $(board_comm_pids udhcpc); do
        case "$(tr '\0' ' ' < "/proc/$_pid/cmdline" 2>/dev/null)" in
        *"$WIFI_IF"*) kill "$_pid" 2>/dev/null || true ;;
        esac
    done

    rm -f "$STATE_DIR/wifi_ip"
    log "wifi-sta: DHCP on $WIFI_IF${requested:+ (requesting $requested)}"
    if [ -n "$requested" ]; then
        TSF_STATE_DIR="$STATE_DIR" TSF_WIFI_IF="$WIFI_IF" \
            udhcpc -b -i "$WIFI_IF" -r "$requested" \
                -s "$HERE/udhcpc-tsf.sh" \
                -p "$STATE_DIR/udhcpc-$WIFI_IF.pid" >/dev/null 2>&1 || \
            warn "udhcpc could not be started"
    else
        TSF_STATE_DIR="$STATE_DIR" TSF_WIFI_IF="$WIFI_IF" \
            udhcpc -b -i "$WIFI_IF" \
                -s "$HERE/udhcpc-tsf.sh" \
                -p "$STATE_DIR/udhcpc-$WIFI_IF.pid" >/dev/null 2>&1 || \
            warn "udhcpc could not be started"
    fi

    _left="$DHCP_TIMEOUT"
    while [ "$_left" -gt 0 ] && [ -z "$(iface_ipv4 "$WIFI_IF")" ]; do
        sleep 1
        _left=$((_left - 1))
    done

    if [ -z "$(iface_ipv4 "$WIFI_IF")" ]; then
        warn "DHCP did not answer within ${DHCP_TIMEOUT}s"
        for _pid in $(board_comm_pids udhcpc); do
            case "$(tr '\0' ' ' < "/proc/$_pid/cmdline" 2>/dev/null)" in
            *"$WIFI_IF"*) kill "$_pid" 2>/dev/null || true ;;
            esac
        done
        if [ -n "$requested" ]; then
            warn "falling back to the requested address $requested"
            ip addr add "$requested/24" dev "$WIFI_IF" 2>/dev/null || true
            ip link set "$WIFI_IF" up
        else
            # No device id, so nothing pins an address. Retrying DHCP forever
            # would leave the follower unaddressable and the sync never starts,
            # so take a deterministic address from outside the pool instead and
            # make the cause loud: this is a symptom, not a configuration.
            _fb_ip="$(tsf_fallback_ipv4)"
            warn "no address after ${DHCP_TIMEOUT}s and no device-id to request one"
            warn "  using the MAC-derived $_fb_ip/24 (outside the DHCP pool .10-.200)"
            warn "  likely cause: the AX650 AP's DHCP server is stopped -"
            warn "  see docs/06-TROUBLESHOOTING.md (GOT_IP/LOST_IP flap)"
            ip addr add "$_fb_ip/24" dev "$WIFI_IF" 2>/dev/null || true
            ip link set "$WIFI_IF" up
        fi
    fi
fi

ipv4="$(iface_ipv4 "$WIFI_IF")"
[ -n "$ipv4" ] || die "no IPv4 address on $WIFI_IF after configuration"

# Never let the Wi-Fi link rewrite the default route. The gateway comes from
# the DHCP lease, so it is read from the route table rather than assumed.
drop_default_route_dev "$WIFI_IF"

write_state "$STATE_DIR/wifi.env" \
    "WIFI_ROLE=sta" "WIFI_IF=$WIFI_IF" "WIFI_IP=$ipv4" \
    "WIFI_GW=$WIFI_GW" "WIFI_DEVICE_ID=$(tsf_device_id)"

log "wifi-sta: ready  if=$WIFI_IF ip=$ipv4 device=$(tsf_device_id)"
log "wifi-sta: MAC $(iface_mac "$WIFI_IF")"

# ---------------------------------------------------------------------------
# 5. Reachability - report it, but do not fail the whole bring-up on one lost
#    ping: the TSF beacon is the real liveness check for the controller.
# ---------------------------------------------------------------------------
if ping_ip "$WIFI_GW" 5; then
    log "wifi-sta: $WIFI_GW reachable"
else
    warn "$WIFI_GW does not answer ping; check the AP side (it may still work for TCP)"
fi
