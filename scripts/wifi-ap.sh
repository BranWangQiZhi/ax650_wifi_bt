#!/bin/sh
# SPDX-License-Identifier: MIT
#
# wifi-ap.sh - bring this board up as a 5 GHz AP for the AX637 followers, and
#              optionally also as a STA joined to an external network (uplink).
#
# AP role (always):
#   wifi-role assigns 192.168.4.1/24 to ethap0 and starts a DHCP server for the
#   pool 192.168.4.10-192.168.4.200, so two AX637 boards never collide and
#   nothing has to be pre-registered. tsf_peer_beacon on this board then learns
#   which address belongs to which live follower.
#
# Uplink role (UPLINK_ENABLE=1):
#   The same C5 also joins an external AP as a client, giving this board a
#   second network through ethsta0. That is EH_MODE=apsta: one radio, AP and STA
#   time-shared. Consequences, both documented and real:
#     - the uplink AP must be on the SAME channel as WIFI_CHANNEL, because there
#       is only one radio; a mismatch means the STA never associates,
#     - while the uplink SSID is missing, the STA retries by scanning every few
#       seconds, and each scan preempts the AP (peer sees reason=2 and heavy
#       loss). So only enable the uplink when that network is actually there.
#   AX650 cannot hot-reset the C5 (unbind panics), so a mode change takes effect
#   only after a reboot; this script says so instead of tearing down a working AP.
#
# Configuration file: $TSF_ROOT/wifi.conf (installed from config/wifi.conf)
#   WIFI_SSID / WIFI_PASS / WIFI_CHANNEL     AP for the AX637 boards
#   UPLINK_ENABLE / UPLINK_SSID / UPLINK_PASS   optional station uplink
#
# Environment overrides: WIFI_SSID WIFI_PASS WIFI_CHANNEL UPLINK_ENABLE
#                        UPLINK_SSID UPLINK_PASS UPLINK_DEFAULT_ROUTE
#                        UPLINK_UP_TIMEOUT WIFI_UP_TIMEOUT WIFI_SKIP_MODULE
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/common.sh"

# Environment wins over the file - see read_wifi_conf() in common.sh.
read_wifi_conf

WIFI_SSID="${WIFI_SSID:-AX650-SDIO-AP}"
WIFI_PASS="${WIFI_PASS:-ax650sdio}"
WIFI_CHANNEL="${WIFI_CHANNEL:-36}"
UPLINK_ENABLE="${UPLINK_ENABLE:-0}"
UPLINK_SSID="${UPLINK_SSID:-}"
UPLINK_PASS="${UPLINK_PASS:-}"
UPLINK_DEFAULT_ROUTE="${UPLINK_DEFAULT_ROUTE:-0}"
UPLINK_UP_TIMEOUT="${UPLINK_UP_TIMEOUT:-30}"
WIFI_UP_TIMEOUT="${WIFI_UP_TIMEOUT:-60}"
WIFI_SKIP_MODULE="${WIFI_SKIP_MODULE:-0}"
LOG="$LOG_DIR/wifi-ap.log"

# A configured uplink without an SSID is a configuration mistake, not a reason
# to silently run AP-only: the operator asked for a second network.
if [ "$UPLINK_ENABLE" = "1" ] && [ -z "$UPLINK_SSID" ]; then
    die "UPLINK_ENABLE=1 but UPLINK_SSID is empty (edit $TSF_ROOT/wifi.conf)"
fi

if [ "$UPLINK_ENABLE" = "1" ]; then
    MODE=apsta
else
    MODE=ap
fi

# channel >= 36 is 5 GHz; the STA and the AP must agree or association fails
# with reason=201 and the log gives almost nothing else to go on.
case "$WIFI_CHANNEL" in
''|*[!0-9]*) die "WIFI_CHANNEL must be numeric, got '$WIFI_CHANNEL'" ;;
esac
[ "$WIFI_CHANNEL" -ge 36 ] || warn "channel $WIFI_CHANNEL is 2.4 GHz - not validated on this release"

# One radio, one channel: the uplink AP has to sit on WIFI_CHANNEL.
if [ "$UPLINK_ENABLE" = "1" ] && [ -n "${UPLINK_CHANNEL:-}" ]; then
    if [ "$UPLINK_CHANNEL" != "$WIFI_CHANNEL" ]; then
        warn "UPLINK_CHANNEL=$UPLINK_CHANNEL != WIFI_CHANNEL=$WIFI_CHANNEL on a single radio"
        warn "  the STA will not stay associated; align both or change WIFI_CHANNEL"
    fi
fi

ensure_dirs

log "wifi-ap: mode=$MODE SSID=$WIFI_SSID channel=$WIFI_CHANNEL if=$WIFI_AP_IF${UPLINK_SSID:+ uplink=$UPLINK_SSID}"

ap_ip="$(iface_ipv4 "$WIFI_AP_IF")"
if [ -n "$ap_ip" ] && have_comm wifi-role; then
    # Mode mismatch detection.
    #
    # The netdev list cannot be used for this: esp32_sdio registers BOTH
    # ethsta0 and ethap0 at probe time, so ethsta0 exists even when wifi-role
    # was started AP-only. The recorded role from the last successful start is
    # the only reliable evidence.
    _prev_role=""
    if [ -r "$STATE_DIR/wifi.env" ]; then
        _prev_role="$(sed -n 's/^WIFI_ROLE=//p' "$STATE_DIR/wifi.env" | head -1)"
    fi
    if [ -n "$_prev_role" ] && [ "$_prev_role" != "$MODE" ]; then
        warn "wifi-role is running as '$_prev_role' but the configuration now asks for '$MODE'"
        warn "  AX650 cannot hot-reset the C5, so this needs a reboot: sync; reboot"
        exit 1
    fi
    log "wifi-ap: already up, $WIFI_AP_IF=$ap_ip (mode=$MODE)"
    write_state "$STATE_DIR/wifi.env" \
        "WIFI_ROLE=$MODE" "WIFI_IF=$WIFI_AP_IF" "WIFI_IP=$ap_ip" "WIFI_GW=$ap_ip" \
        "UPLINK_SSID=${UPLINK_SSID:-}" "UPLINK_IP=$(iface_ipv4 "$WIFI_IF")"
    print_leases
    exit 0
fi

if [ "$WIFI_SKIP_MODULE" != "1" ]; then
    ensure_wifi_module || die "Wi-Fi driver is not available (see the WARN lines above)"
fi

if have_comm wifi-role; then
    log "wifi-ap: stopping previous wifi-role"
    kill_comm wifi-role TERM
    wait_comm_gone wifi-role 10 || kill_comm wifi-role KILL
    sleep 1
fi

: > "$LOG"
if [ "$MODE" = "apsta" ]; then
    log "wifi-ap: starting wifi-role (EH_MODE=apsta, uplink SSID=$UPLINK_SSID)"
    # EH_EXTERNAL_IP must NOT be set here: it would stop both the AP's DHCP
    # server (AX637 would get no address) and the STA's DHCP client (the uplink
    # would get no address).
    EH_MODE=apsta \
    EH_AP_SSID="$WIFI_SSID" \
    EH_AP_PASSWORD="$WIFI_PASS" \
    EH_AP_CHANNEL="$WIFI_CHANNEL" \
    EH_STA_SSID="$UPLINK_SSID" \
    EH_STA_PASSWORD="$UPLINK_PASS" \
        "$WIFI_APP" >> "$LOG" 2>&1 &
else
    log "wifi-ap: starting wifi-role (EH_MODE=ap)"
    EH_MODE=ap \
    EH_AP_SSID="$WIFI_SSID" \
    EH_AP_PASSWORD="$WIFI_PASS" \
    EH_AP_CHANNEL="$WIFI_CHANNEL" \
        "$WIFI_APP" >> "$LOG" 2>&1 &
fi
APP_PID=$!

# wifi-role prints ONE line, prefixed by the mode it actually entered:
#   "ap up. ..." / "sta up. ..." / "apsta up. ..."
# The line carries an ESP-IDF prefix (`I (1234) wifi apsta: apsta up. ...`), so
# it must NOT be anchored to the start of the line.
UP_RE='\(ap\|apsta\) up\.'

_left="$WIFI_UP_TIMEOUT"
while [ "$_left" -gt 0 ]; do
    grep -q "$UP_RE" "$LOG" 2>/dev/null && break
    if grep -q 'no response uid=1 msg_id=278' "$LOG" 2>/dev/null; then
        tail -5 "$LOG" >&2
        die "the C5 is not responding: reset it and re-enumerate SDIO (docs/wifi/04-FAQ.md Q4)"
    fi
    kill -0 "$APP_PID" 2>/dev/null || {
        tail -10 "$LOG" >&2
        die "wifi-role exited before the AP came up"
    }
    sleep 1
    _left=$((_left - 1))
done

if ! grep -q "$UP_RE" "$LOG" 2>/dev/null; then
    tail -10 "$LOG" >&2
    die "AP did not come up within ${WIFI_UP_TIMEOUT}s"
fi
log "wifi-ap: AP up"
grep "$UP_RE" "$LOG" | tail -1

# The role assigns 192.168.4.1/24 and starts the DHCP server itself; give it a
# moment, then verify rather than configuring it a second time.
_left=10
while [ "$_left" -gt 0 ] && [ -z "$(iface_ipv4 "$WIFI_AP_IF")" ]; do
    sleep 1
    _left=$((_left - 1))
done
ap_ip="$(iface_ipv4 "$WIFI_AP_IF")"
if [ -z "$ap_ip" ]; then
    warn "$WIFI_AP_IF has no IPv4 address; the DHCP server cannot be running."
    warn "Check the wifi-role log: $LOG"
else
    log "wifi-ap: $WIFI_AP_IF=$ap_ip (DHCP pool 192.168.4.10-192.168.4.200)"
fi

# ---------------------------------------------------------------------------
# Uplink (only in apsta mode). It is NOT part of the TSF critical path: the AP
# and the frame sync keep working if the external network is absent, so a
# failure here is a warning. The one thing that must not be silent is the
# scanning penalty, which is why the warning names it explicitly.
# ---------------------------------------------------------------------------
uplink_ip=""
if [ "$MODE" = "apsta" ]; then
    _left="$UPLINK_UP_TIMEOUT"
    while [ "$_left" -gt 0 ]; do
        uplink_ip="$(iface_ipv4 "$WIFI_IF")"
        [ -n "$uplink_ip" ] && break
        grep -q 'reason=2\|reason=15\|reason=201' "$LOG" 2>/dev/null && break
        sleep 1
        _left=$((_left - 1))
    done
    uplink_ip="$(iface_ipv4 "$WIFI_IF")"

    if [ -n "$uplink_ip" ]; then
        log "wifi-ap: uplink $WIFI_IF=$uplink_ip (SSID=$UPLINK_SSID)"
    else
        warn "uplink '$UPLINK_SSID' did not associate within ${UPLINK_UP_TIMEOUT}s"
        warn "  the STA retries by scanning, and each scan preempts the AP:"
        warn "  expect packet loss on the AX637 link while it is unassociated."
        warn "  check: is '$UPLINK_SSID' in range, on channel $WIFI_CHANNEL,"
        warn "  with password '$UPLINK_PASS'? or set UPLINK_ENABLE=0 again."
    fi

    if [ -n "$uplink_ip" ]; then
        if [ "$UPLINK_DEFAULT_ROUTE" = "1" ]; then
            log "wifi-ap: keeping the uplink default route (UPLINK_DEFAULT_ROUTE=1)"
        else
            # The uplink's DHCP client installs a default route through the
            # external AP. On a board administered over the wired network that
            # silently replaces the return path, so it is removed by default.
            drop_default_route_dev "$WIFI_IF"
        fi
    fi
fi

write_state "$STATE_DIR/wifi.env" \
    "WIFI_ROLE=$MODE" "WIFI_IF=$WIFI_AP_IF" "WIFI_IP=${ap_ip:-}" \
    "WIFI_GW=${ap_ip:-$WIFI_GW}" \
    "UPLINK_SSID=${UPLINK_SSID:-}" "UPLINK_IF=$WIFI_IF" "UPLINK_IP=${uplink_ip:-}"

print_leases
log "wifi-ap: ready. Followers use SSID=$WIFI_SSID / password=$WIFI_PASS"
