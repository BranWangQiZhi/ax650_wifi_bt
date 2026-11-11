#!/bin/sh
# 烧录 ESP32-C5 固件。
#
#   ./flash.sh <串口>            完整烧录（bootloader + 分区表 + otadata + app）
#   ./flash.sh <串口> --app      只烧 app（引导已在板上时）
#
# 串口写法：COM97（Windows）/ /dev/ttyUSB0（Linux）/ /dev/cu.usbserial-*（macOS）
#
# 依赖 esptool：pip install esptool
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
FW="$HERE"
PORT="${1:-}"
MODE="${2:-}"

[ -n "$PORT" ] || { echo "usage: $0 <串口> [--app]" >&2; exit 2; }
[ -d "$FW" ] || { echo "ERROR: 找不到 $FW" >&2; exit 1; }

ESPTOOL="esptool"
command -v "$ESPTOOL" >/dev/null 2>&1 || ESPTOOL="python3 -m esptool"

FLASH="--flash-mode dio --flash-size 4MB --flash-freq 80m"

if [ "$MODE" = "--app" ]; then
    echo ">>> 只烧 app -> 0x10000"
    # shellcheck disable=SC2086
    $ESPTOOL --chip esp32c5 -p "$PORT" -b 460800 \
        --before default-reset --after hard-reset \
        write-flash $FLASH 0x10000 "$FW/app.bin"
else
    echo ">>> 完整烧录（4 个分区）"
    # shellcheck disable=SC2086
    $ESPTOOL --chip esp32c5 -p "$PORT" -b 460800 \
        --before default-reset --after hard-reset \
        write-flash $FLASH \
        0x2000  "$FW/bootloader.bin" \
        0x8000  "$FW/partition-table.bin" \
        0xd000  "$FW/ota_data_initial.bin" \
        0x10000 "$FW/app.bin"
fi

echo
echo ">>> 完成。下一步必做："
echo "    1) 重新枚举 SDIO（否则加载驱动会 probe -110）"
echo "       AX637: sdhci-axera unbind/bind"
echo "       AX650: sync; reboot  （不可热解绑，会 panic）"
echo "    2) ./load.sh"
echo
echo "    验证固件：打开同一串口 @115200 复位，应看到"
echo "      app_init: Project name:     tsf_uart_bridge_mode2_cp"
echo "      SDIO_SLAVE: SDIO datapath mode: SW_AGGR"
