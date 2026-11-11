#!/bin/sh
# SPDX-License-Identifier: MIT
#
# common.sh - shared helpers for the TSF + Wi-Fi scripts.
#
# Sourced, never executed. It deliberately uses only busybox-compatible shell:
# both boards run ash, and `ps` column order differs between them, so process
# management goes through /proc instead of ps/pgrep/pkill.
#
# Board defaults are derived from the kernel release, which is the one property
# that actually differs and cannot be confused: 6.1.83-rt28 is AX637 and
# 5.15.73 is AX650. Every path can still be overridden from the environment.

set -u

# ---------------------------------------------------------------------------
# Board identity and default paths
# ---------------------------------------------------------------------------
if [ -z "${BOARD:-}" ]; then
    case "$(uname -r)" in
    6.1.83*) BOARD=637 ;;
    5.15.73*) BOARD=650 ;;
    *)
        echo "ERROR: unknown board kernel '$(uname -r)'; set BOARD=637 or 650" >&2
        exit 2
        ;;
    esac
fi
export BOARD

if [ "$BOARD" = "637" ]; then
    TSF_ROOT="${TSF_ROOT:-/root/tsf}"
    WIFI_DIR="${WIFI_DIR:-/opt/data/wlan}"
    MCU_UART="${MCU_UART:-/dev/ttyS3}"
    LATCH_CHANNEL="${LATCH_CHANNEL:-0}"
    MODULE_BACKEND="${MODULE_BACKEND:-backend=lpwm lpwm_channel=8 lpwm_phys=0x11002000 pinmux_phys=0x11008018}"
    PAD_ADDR="${PAD_ADDR:-0x11008018}"
    PAD_VALUE="${PAD_VALUE:-0x00010003}"
    LATCH_PAD_ADDR="${LATCH_PAD_ADDR:-}"
    LATCH_PAD_VALUE="${LATCH_PAD_VALUE:-}"
    LIBRARY_DIR="${LIBRARY_DIR:-/opt/lib}"
else
    TSF_ROOT="${TSF_ROOT:-/soc/tsf}"
    WIFI_DIR="${WIFI_DIR:-/soc/sdio_wifi}"
    MCU_UART="${MCU_UART:-/dev/ttyS5}"
    LATCH_CHANNEL="${LATCH_CHANNEL:-3}"
    MODULE_BACKEND="${MODULE_BACKEND:-backend=gpio gpio=30 pinmux_phys=0x0425083C}"
    PAD_ADDR="${PAD_ADDR:-0x0425083C}"
    PAD_VALUE="${PAD_VALUE:-0x00060003}"
    LATCH_PAD_ADDR="${LATCH_PAD_ADDR:-0x04250618}"
    LATCH_PAD_VALUE="${LATCH_PAD_VALUE:-0x00010003}"
    LIBRARY_DIR="${LIBRARY_DIR:-}"
fi

SAMPLE_BIN="${SAMPLE_BIN:-$TSF_ROOT/tsf_sync_sample}"
BEACON_BIN="${BEACON_BIN:-$TSF_ROOT/tsf_peer_beacon}"
MODULE_PATH="${MODULE_PATH:-$TSF_ROOT/ax_frame_pulse.ko}"
WIFI_APP="${WIFI_APP:-$WIFI_DIR/wifi-role}"
WIFI_KO="${WIFI_KO:-$WIFI_DIR/esp32_sdio.ko}"

WIFI_IF="${WIFI_IF:-ethsta0}"
WIFI_AP_IF="${WIFI_AP_IF:-ethap0}"
WIFI_GW="${WIFI_GW:-192.168.4.1}"
PEER_PORT="${PEER_PORT:-47800}"
FOLLOW_LISTEN_PORT="${FOLLOW_LISTEN_PORT:-47654}"
PEER_ID_PREFIX="${PEER_ID_PREFIX:-ax637}"

# State and logs. /run is a tmpfs on both boards; logs go next to the binaries so
# a reboot does not erase the evidence.
if mkdir -p /run 2>/dev/null && [ -w /run ]; then
    STATE_DIR="${STATE_DIR:-/run/tsf}"
else
    STATE_DIR="${STATE_DIR:-/tmp/tsf}"
fi
LOG_DIR="${LOG_DIR:-$TSF_ROOT/logs}"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log()  { printf '%s [%s] %s\n' "$(date '+%F %T')" "$BOARD" "$*"; }
warn() { printf '%s [%s] WARN: %s\n' "$(date '+%F %T')" "$BOARD" "$*" >&2; }
die()  { printf '%s [%s] ERROR: %s\n' "$(date '+%F %T')" "$BOARD" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------
# wifi-ap.sh and wifi-sta.sh document WIFI_*/UPLINK_* as environment overrides.
# A bare `. "$conf"` cannot honour that: the shipped wifi.conf assigns the same
# names, so the file always wins and `UPLINK_ENABLE=0 ./wifi-ap.sh` would be a
# silent no-op - the operator asks for no uplink and still gets one. Snapshot
# the names the caller actually set, source the file, then put them back.
#
# POSIX sh has no indirect expansion, hence the eval; the names come from the
# fixed list below and never from input.
WIFI_CONF_OVERRIDES="WIFI_SSID WIFI_PASS WIFI_CHANNEL WIFI_STATIC_IP
UPLINK_ENABLE UPLINK_SSID UPLINK_PASS UPLINK_CHANNEL UPLINK_DEFAULT_ROUTE
UPLINK_UP_TIMEOUT WIFI_UP_TIMEOUT DHCP_TIMEOUT WIFI_SKIP_MODULE TSF_DEVICE_ID"

read_wifi_conf() {
    for _rc_v in $WIFI_CONF_OVERRIDES; do
        eval "_rc_set_$_rc_v=\${$_rc_v+1}"
        eval "_rc_val_$_rc_v=\${$_rc_v-}"
    done

    for _rc_f in "$TSF_ROOT/wifi.conf" /etc/tsf-wifi.conf; do
        if [ -r "$_rc_f" ]; then
            # shellcheck disable=SC1090
            . "$_rc_f"
            break
        fi
    done

    for _rc_v in $WIFI_CONF_OVERRIDES; do
        eval "if [ \"\${_rc_set_$_rc_v-}\" = 1 ]; then $_rc_v=\$_rc_val_$_rc_v; fi"
        # busybox ash does not expand globs in `unset`, so clean up per name.
        eval "unset _rc_set_$_rc_v _rc_val_$_rc_v"
    done

    unset _rc_f _rc_v
    return 0
}

# ---------------------------------------------------------------------------
# Process helpers - /proc based, because ps column order differs per board
# ---------------------------------------------------------------------------

# print the PIDs whose /proc/<pid>/comm equals $1
board_comm_pids() {
    _want="$1"
    for _f in /proc/[0-9]*/comm; do
        [ -r "$_f" ] || continue
        if [ "$(cat "$_f" 2>/dev/null)" = "$_want" ]; then
            _p="${_f#/proc/}"
            printf '%s\n' "${_p%/comm}"
        fi
    done
}

# print the PIDs whose command line contains $1 (fixed string)
board_cmdline_pids() {
    _needle="$1"
    for _f in /proc/[0-9]*/cmdline; do
        [ -r "$_f" ] || continue
        _p="${_f#/proc/}"
        _p="${_p%/cmdline}"
        case "$(tr '\0' ' ' < "$_f" 2>/dev/null)" in
        *"$_needle"*) printf '%s\n' "$_p" ;;
        esac
    done
}

kill_comm() {
    _comm="$1"
    _sig="${2:-TERM}"
    for _pid in $(board_comm_pids "$_comm"); do
        kill "-$_sig" "$_pid" 2>/dev/null || true
    done
}

kill_cmdline() {
    _needle="$1"
    _sig="${2:-TERM}"
    for _pid in $(board_cmdline_pids "$_needle"); do
        # Skip this shell AND the shell that launched it. The match is a
        # substring of /proc/<pid>/cmdline, so a wrapper such as
        # `sh -c '/path/tsf-stop.sh; grep tsf_sync_sample ...'` matches the
        # needle itself - killing $PPID there tears down the caller mid-script
        # and the rest of the cleanup silently never runs.
        [ "$_pid" = "$$" ] && continue
        [ "$_pid" = "$PPID" ] && continue
        kill "-$_sig" "$_pid" 2>/dev/null || true
    done
}

# 0 when at least one process with that comm/cmdline is alive
have_comm()    { [ -n "$(board_comm_pids "$1")" ]; }
have_cmdline() { [ -n "$(board_cmdline_pids "$1")" ]; }

# wait_comm_gone <comm> <seconds>
wait_comm_gone() {
    _comm="$1"
    _left="${2:-10}"
    while [ "$_left" -gt 0 ]; do
        have_comm "$_comm" || return 0
        sleep 1
        _left=$((_left - 1))
    done
    return 1
}

# ---------------------------------------------------------------------------
# Single-instance guard
# ---------------------------------------------------------------------------

# check_single_instance <pidfile> <script-name>
#
# Uses the recorded PID, not a cmdline scan: a scan would also match the
# `sh -c /path/tsf-controller.sh` wrapper that some launchers create, and the
# script would refuse to start under its own parent. The cmdline is still
# checked, but only for the recorded PID, to survive PID reuse.
check_single_instance() {
    _pidfile="$1"
    _script="$2"

    if [ -r "$_pidfile" ]; then
        _old="$(cat "$_pidfile" 2>/dev/null)"
        case "$_old" in
        ''|*[!0-9]*) _old="" ;;
        esac
        if [ -n "$_old" ] && kill -0 "$_old" 2>/dev/null; then
            case "$(tr '\0' ' ' < "/proc/$_old/cmdline" 2>/dev/null)" in
            *"$_script"*)
                die "another $_script is already running (pid $_old); stop it with tsf-stop.sh"
                ;;
            esac
        fi
    fi
    echo "$$" > "$_pidfile"
}

# ---------------------------------------------------------------------------
# Wi-Fi driver
# ---------------------------------------------------------------------------

# Load esp32_sdio if it is not loaded yet, with the SDIO clock fixed at 25 MHz
# first. Returns non-zero with a message on stderr when the SDIO link is not
# enumerated - the one failure whose fix differs between the two boards.
ensure_wifi_module() {
    if lsmod 2>/dev/null | grep -q '^esp32_sdio'; then
        return 0
    fi
    [ -d "$WIFI_DIR" ] || { warn "Wi-Fi directory not found: $WIFI_DIR"; return 1; }
    [ -f "$WIFI_KO" ] || { warn "driver not found: $WIFI_KO"; return 1; }
    [ -x "$WIFI_APP" ] || { warn "wifi-role not found or not executable: $WIFI_APP"; return 1; }

    if [ ! -d /sys/bus/sdio/devices ] ||
       [ -z "$(ls -A /sys/bus/sdio/devices 2>/dev/null)" ]; then
        warn "SDIO is not enumerated (no mmc1:0001:x)"
        if [ "$BOARD" = "637" ]; then
            warn "  AX637: echo c010000.sdhci > /sys/bus/platform/drivers/sdhci-axera/unbind"
            warn "         sleep 1"
            warn "         echo c010000.sdhci > /sys/bus/platform/drivers/sdhci-axera/bind"
        else
            warn "  AX650: sync; reboot   (hot unbind panics on AX650)"
        fi
        return 1
    fi

    # 50 MHz is the power-on default. It has no throughput benefit and the
    # downlink stalls, so it must be fixed before insmod.
    mountpoint -q /sys/kernel/debug 2>/dev/null || \
        mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
    if [ -w /sys/kernel/debug/mmc1/clock ]; then
        echo 25000000 > /sys/kernel/debug/mmc1/clock
        log "SDIO clock fixed at 25 MHz"
    else
        warn "cannot set /sys/kernel/debug/mmc1/clock; continuing at the default"
    fi

    log "loading $WIFI_KO"
    insmod "$WIFI_KO" || {
        warn "insmod failed; check dmesg (probe -110 means SDIO was not re-enumerated)"
        return 1
    }
    sleep 3
    lsmod 2>/dev/null | grep -q '^esp32_sdio' || {
        warn "esp32_sdio did not load; check dmesg"
        return 1
    }
    return 0
}

# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

# print the first IPv4 address on an interface, without the prefix length
iface_ipv4() {
    ip -4 -o addr show dev "$1" 2>/dev/null |
        awk '{ for (i = 1; i <= NF; i++) if ($i == "inet") { print $(i + 1); exit } }' |
        cut -d/ -f1
}

iface_mac() {
    cat "/sys/class/net/$1/address" 2>/dev/null
}

# The configured device id, verbatim: env, then $TSF_ROOT/device-id, then a
# MAC-derived name. No prefix is added here.
tsf_device_id_raw() {
    if [ -n "${TSF_DEVICE_ID:-}" ]; then
        printf '%s\n' "$TSF_DEVICE_ID"
        return 0
    fi
    for _f in "$TSF_ROOT/device-id" /etc/tsf-device-id; do
        if [ -r "$_f" ]; then
            _v="$(tr -d ' \t\r\n' < "$_f")"
            [ -n "$_v" ] && { printf '%s\n' "$_v"; return 0; }
        fi
    done
    _mac="$(iface_mac "$WIFI_IF")"
    if [ -n "$_mac" ]; then
        printf 'ax637-%s\n' "$(printf '%s' "$_mac" | tr -d ':' | tail -c 5)"
    else
        printf 'ax637\n'
    fi
}

# The announcement id. It always carries the ax637- prefix, which is what the
# controller's discovery filter (PEER_ID_PREFIX) matches, so a plain "1" in
# device-id cannot silently disconnect the follower from discovery.
tsf_device_id() {
    _raw="$(tsf_device_id_raw)"
    case "$_raw" in
    ax637*) printf '%s\n' "$_raw" ;;
    *) printf 'ax637-%s\n' "$_raw" ;;
    esac
}

# The address this device asks the AP's DHCP server for. Only device id 1..9
# (either "2" or "ax637-2") maps to an address; anything else means "plain
# DHCP", which still works - the controller learns the address from the beacon,
# not from DHCP.
tsf_requested_ipv4() {
    _raw="$(tsf_device_id_raw)"
    case "$_raw" in
    [1-9]) printf '192.168.4.1%s\n' "$_raw" ;;
    ax637-[1-9]) printf '192.168.4.1%s\n' "${_raw#ax637-}" ;;
    esac
}

# Last-resort address for a follower whose DHCP request got no answer and whose
# device id does not pin one.
#
# This should not normally be reached: the shipped default runs a DHCP server
# on the AX650 AP. It exists because that server can be left stopped by a
# transient `GOT_IP -> LOST_IP -> GOT_IP` flap at AP bring-up (documented in
# docs/06-TROUBLESHOOTING.md). Without a fallback the follower ends up with no
# address and the whole frame-sync run fails, which is a much worse outcome
# than an address nobody asked for.
#
# The octet is derived from the MAC and taken from 201-240, i.e. OUTSIDE the
# served pool 192.168.4.10-200, so it cannot collide with a lease the server
# hands out later or with the AP itself (.1). Two followers collide only if
# their MACs agree modulo 40.
tsf_fallback_ipv4() {
    _fb_last="$(iface_mac "$WIFI_IF" | tr -d ':' | tail -c 3)"
    _fb_dec="$(printf '%d' "0x$_fb_last" 2>/dev/null)" || _fb_dec=""
    case "$_fb_dec" in
    ''|*[!0-9]*) _fb_dec=0 ;;
    esac
    printf '192.168.4.%s\n' "$((201 + _fb_dec % 40))"
}

# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------
ensure_dirs() {
    mkdir -p "$STATE_DIR" "$LOG_DIR" 2>/dev/null || true
    [ -d "$STATE_DIR" ] || die "cannot create $STATE_DIR"
}

# Write a file atomically so a reader never sees a partial value. Each argument
# becomes one KEY=VALUE line, which is what the shell "source this file" pattern
# in the docs expects.
write_state() {
    _path="$1"
    shift
    mkdir -p "$(dirname "$_path")" 2>/dev/null || true
    : > "$_path.tmp" || return 1
    for _w in "$@"; do
        printf '%s\n' "$_w" >> "$_path.tmp" || return 1
    done
    mv "$_path.tmp" "$_path"
}

# Remove any default route that goes out through $1. The gateway is not known
# in advance on an uplink (it comes from the external DHCP server), and a stale
# one is worse than none: it silently replaces the return path of the wired SSH
# session.
drop_default_route_dev() {
    _dev="$1"
    ip route 2>/dev/null | while IFS= read -r _line; do
        case "$_line" in
        default*) ;;
        *) continue ;;
        esac
        case " $_line " in
        *" dev $_dev "*) ;;
        *) continue ;;
        esac

        _gw=""
        _prev=""
        _rest="$_line"
        while [ -n "$_rest" ]; do
            _word="${_rest%% *}"
            _rest="${_rest#* }"
            [ "$_rest" = "$_word" ] && _rest=""
            if [ "$_prev" = "via" ] && [ -n "$_word" ]; then
                _gw="$_word"
            fi
            _prev="$_word"
        done

        printf '%s [%s] WARN: removing default route%s dev %s (it would hijack SSH)\n' \
            "$(date '+%F %T')" "$BOARD" "${_gw:+ via $_gw}" "$_dev" >&2
        if [ -n "$_gw" ]; then
            ip route del default via "$_gw" dev "$_dev" 2>/dev/null || true
        else
            ip route del default dev "$_dev" 2>/dev/null || true
        fi
    done
}

# ping_ip <ip> <count> - return 0 if at least one reply
ping_ip() {
    ping -c "${2:-3}" -W 2 "$1" >/dev/null 2>&1
}

# Show the AP's DHCP leases when the backend leaves them somewhere readable.
# The lease file is informational only: it is not used to build the peer list,
# because a lease outlives the device and a controller must never be pointed at
# an address nobody is listening on.
print_leases() {
    for _f in /run/eh_host_dhcps.leases /var/lib/misc/dnsmasq.leases \
              /var/lib/misc/udhcpd.leases /var/lib/dhcpd/dhcpd.leases; do
        [ -r "$_f" ] || continue
        log "AP DHCP leases ($_f):"
        while IFS= read -r _line; do
            [ -n "$_line" ] && log "  $_line"
        done < "$_f"
        return 0
    done
    log "AP DHCP lease file not found (normal before the first client joins)"
}
