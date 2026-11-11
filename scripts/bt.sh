#!/bin/sh
# bt.sh -- AX637 / AX650 的 BLE 管理脚本（不依赖 BlueZ）
#
#   ./bt.sh up              加载驱动 + BT 上电 + 开 LE
#   ./bt.sh down            BT 下电（保留 hci0）
#   ./bt.sh status          查看控制器状态
#   ./bt.sh adv <名字> [秒] 开 BLE 广播（不给秒数则一直播）
#   ./bt.sh adv off         停广播
#   ./bt.sh scan [秒]       LE 扫描
#
# 依赖同目录下的 bt-mgmt-tool / bt-extadv-tool，以及内核模块 esp32_sdio.ko。
#
# 注意几件事（都是实测踩过的）：
#   1. 上电后 SDIO 未必自动枚举，本脚本的 up 会自动再 bind 一次
#   2. C5 只接受 extended advertising，所以广播走 bt-extadv-tool
#      （legacy 的 0x2006 命令它不应答）
#   3. bt-mgmt-tool 的 adv 走内核 MGMT，命令都返回 SUCCESS 但空口看不到，
#      所以这里默认用 bt-extadv-tool
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
MGMT="$HERE/bt-mgmt-tool"
EXADV="$HERE/bt-extadv-tool"
KO_NAME=esp32_sdio
PARAM=/sys/module/bluetooth/parameters/enable_ecred

say() { echo "[bt] $*"; }
die() { echo "[bt] 错误: $*" >&2; exit 1; }

board() {
    case "$(uname -r)" in
        6.1.83*) echo ax637 ;;
        5.15.73*) echo ax650 ;;
        *) echo unknown ;;
    esac
}

sdhci_of() {
    case "$(board)" in
        ax637) echo c010000.sdhci ;;
        ax650) echo 30e00000.sdhc ;;
        *) echo "" ;;
    esac
}

hci_present() { [ -e /sys/class/bluetooth/hci0 ]; }

rescan_sdio() {
    of=$(sdhci_of)
    [ -n "$of" ] || return 0
    # 已经有 SDIO 功能就别动，热解绑在部分板子上会导致 panic
    [ -n "$(ls /sys/bus/sdio/devices/ 2>/dev/null)" ] && return 0

    drv=/sys/bus/platform/drivers/sdhci-axera
    [ -d "$drv" ] || return 0

    say "SDIO 未枚举，重新 bind $of"
    echo "$of" > "$drv/unbind" 2>/dev/null || true
    sleep 2
    echo "$of" > "$drv/bind" 2>/dev/null || true
    sleep 4
}

load_ko() {
    if lsmod 2>/dev/null | grep -q "^$KO_NAME"; then
        say "驱动已加载"
        return 0
    fi
    ko=""
    for c in "$HERE/esp32_sdio.ko" \
             "$HERE/../driver/esp32_sdio-$(uname -r | sed 's/\+//')-bt.ko" \
             /opt/data/wlan/esp32_sdio.ko; do
        [ -f "$c" ] && { ko="$c"; break; }
    done
    [ -n "$ko" ] || die "找不到 esp32_sdio.ko（请放到 $HERE/ 或 ../driver/）"

    say "加载 $ko"
    insmod "$ko" || die "insmod 失败（先确认 SDIO 已枚举、且内核 CONFIG_BT=y）"
    sleep 8
}

case "${1:-status}" in
up)
    load_ko
    hci_present || { rescan_sdio; load_ko; }
    hci_present || die "hci0 未出现，检查 dmesg | grep -i bluetooth"

    [ -w "$PARAM" ] && echo 1 > "$PARAM" 2>/dev/null
    "$MGMT" power on 2>&1 | tail -2
    say "BT 就绪（hci0 已注册，LE 已开）"
    "$MGMT" info 2>&1 | sed -n '1,6p'
    ;;

down)
    "$MGMT" power off 2>&1 | tail -1
    say "已下电（hci0 仍在，彻底关闭用 rmmod $KO_NAME）"
    ;;

status)
    if ! hci_present; then
        echo "状态: hci0 不存在（未加载驱动）"
        echo "板型: $(board)   SDIO: $(ls /sys/bus/sdio/devices/ 2>/dev/null | tr '\n' ' ')"
        exit 0
    fi
    echo "板型: $(board)"
    echo "SDIO: $(ls /sys/bus/sdio/devices/ 2>/dev/null | tr '\n' ' ')"
    echo "ecred: $(cat "$PARAM" 2>/dev/null || echo n/a)"
    echo
    "$MGMT" info 2>&1 | sed -n '1,6p'
    echo
    echo "广播进程: $(ps 2>/dev/null | grep -c '[b]t-extadv-tool')"
    ;;

adv)
    if [ "${2:-}" = "off" ]; then
        for p in /proc/[0-9]*/comm; do
            n=$(cat "$p" 2>/dev/null)
            [ "$n" = "bt-extadv-tool" ] && kill "$(basename "$(dirname "$p")")" 2>/dev/null
        done
        say "广播已停"
        exit 0
    fi
    name="${2:-AX637-BLE}"
    dur="${3:-}"
    hci_present || die "hci0 不存在，先执行 $0 up"

    if [ -n "$dur" ]; then
        exec "$EXADV" "$name" "$dur"
    else
        say "广播 \"$name\"（Ctrl-C 结束）"
        exec "$EXADV" "$name"
    fi
    ;;

scan)
    hci_present || die "hci0 不存在，先执行 $0 up"
    secs="${2:-10}"

    # 上一次扫描若没干净收尾，再开扫描会返回 0x0d INVALID_PARAMS。
    # 这里先 mitigate 一次：失败就 power off/on 重置控制器再重试。
    out=$("$MGMT" scan "$secs" 2>&1)
    case "$out" in
    *"status=0x0d"*|*"status=0x0c"*)
        say "扫描返回 0x0d/0x0c（残留状态），重置控制器后重试"
        "$MGMT" power off >/dev/null 2>&1
        sleep 2
        "$MGMT" power on >/dev/null 2>&1
        sleep 2
        exec "$MGMT" scan "$secs"
        ;;
    esac
    printf '%s\n' "$out"
    ;;

*)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
