#!/bin/sh
# linktest-board.sh -- 板子端链路收发测试（POSIX sh + busybox，免编译）
#
#   sh linktest-board.sh status                     两条链路的状态
#   sh linktest-board.sh wifi server [tcp|udp] [port]   echo 服务端（配 PC 的 client）
#   sh linktest-board.sh wifi client <host> [tcp|udp] [port] [bytes]
#   sh linktest-board.sh ble beacon <数据> [秒]    把数据塞进 BLE 广播名
#   sh linktest-board.sh ble off                   停广播
#
# 为什么 ble 只有 beacon / off：
#   这颗 ESP32-C5 是 BLE-only，内核 CONFIG_BT_BNEP=n、没有 BlueZ，
#   不存在 bnep0 / PAN 网络接口，也没有 GATT 服务端。
#   目前能双向验证的"蓝牙传数据"是把短数据编码进广播名（PC 用 bleak 扫描），
#   单向、每包 <= 20 字节左右、无连接无确认。
#
# 依赖（都已在板上 /soc/sdio_wifi/）：bt-mgmt-tool、bt-extadv-tool、busybox nc。
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
MGMT="$HERE/bt-mgmt-tool"
EXADV="$HERE/bt-extadv-tool"
PREFIX=LNK:

say() { echo "[linktest] $*"; }
die() { echo "[linktest] 错误: $*" >&2; exit 1; }

hci_present() { [ -e /sys/class/bluetooth/hci0 ]; }

bt_up() {
    hci_present || die "hci0 不存在，先执行 $HERE/bt.sh up"
    cur=$("$MGMT" info 2>/dev/null | sed -n 's/.*current : 0x\([0-9A-Fa-f]*\).*/\1/p')
    case "$cur" in
        *[13579BDFbdf]) : ;;                 # 末位为奇数 => bit0 powered=1
        *) "$MGMT" power on >/dev/null 2>&1; say "BT 已上电" ;;
    esac
}

case "${1:-status}" in
status)
    echo "板型/内核 : $(uname -r)"
    echo "WiFi(STA) : $(ip -4 -o addr show ethsta0 2>/dev/null | awk '{print $4}')"
    echo "WiFi(AP)  : $(ip -4 -o addr show ethap0 2>/dev/null | awk '{print $4}')"
    echo "SDIO      : $(ls /sys/bus/sdio/devices/ 2>/dev/null | tr '\n' ' ')"
    if hci_present; then
        echo "BT        : hci0 已注册（BLE-only，无 bnep0 / PAN）"
        "$MGMT" info 2>/dev/null | sed -n '1,6p'
    else
        echo "BT        : 无 hci0，执行 $HERE/bt.sh up"
    fi
    echo "PAN       : 不可用（控制器无 BR/EDR，内核未编 BNEP，rootfs 无 BlueZ）"
    ;;

wifi)
    role=${2:-server}
    case "$role" in
    server)
        proto=${3:-tcp}
        port=${4:-9000}
        case "$proto" in
        udp) say "UDP echo server :$port"; exec nc -u -l -p "$port" -e cat ;;
        *)   say "TCP echo server :$port (Ctrl-C 结束)"; exec nc -lk -p "$port" -e cat ;;
        esac
        ;;
    client)
        host=${3:-}
        proto=${4:-tcp}
        port=${5:-9000}
        bytes=${6:-1024}
        [ -n "$host" ] || die "用法: $0 wifi client <host> [tcp|udp] [port] [bytes]"
        ncopt=""
        [ "$proto" = udp ] && ncopt="-u"
        t0=$(date +%s)
        # shellcheck disable=SC2086
        n=$(dd if=/dev/zero bs=1 count="$bytes" 2>/dev/null | \
            nc $ncopt -w 5 "$host" "$port" | wc -c)
        t1=$(date +%s)
        say "发送 $bytes B ($proto)，收到回显 $n B，耗时 $((t1-t0)) s"
        [ "$n" -eq "$bytes" ] && say "PASS：echo 长度一致" || say "WARN：回显不完整"
        ;;
    *) die "未知 wifi 子命令: $role" ;;
    esac
    ;;

ble)
    cmd=${2:-beacon}
    if [ "$cmd" = off ]; then
        "$EXADV" off >/dev/null 2>&1
        say "广播已停（或用 $HERE/bt.sh adv off）"
        exit 0
    fi
    data=${3:-HELLO}
    secs=${4:-15}
    bt_up
    [ -x "$EXADV" ] || die "找不到 $EXADV"
    # ext adv 的 AD 结构上限 31B：flags(4) + name(2+n) <= 31 => n <= 25，
    # 这里统一截到 24 字符（含 "LNK:" 前缀）。
    name=$(printf '%s%s' "$PREFIX" "$data" | cut -c1-24)
    say "广播 '$name' 共 $secs 秒（PC: python linktest.py ble scan --seconds $secs）"
    if [ -n "$secs" ]; then
        exec "$EXADV" "$name" "$secs"
    else
        exec "$EXADV" "$name"
    fi
    ;;

*)
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
