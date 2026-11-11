# -*- coding: utf-8 -*-
"""ESP32-C5 固件烧录 / 硬复位工具（Windows 本机串口）

用法：
    python flash_c5.py                    # 自动扫描所有串口，找 ESP32-C5 并完整烧录 4 个分区
    python flash_c5.py --port COM7        # 指定串口
    python flash_c5.py --port COM7 --app  # 只烧 app（0x10000）
    python flash_c5.py --port COM7 --reset # 只做硬复位（不烧录）
    python flash_c5.py --list             # 只列出串口并探测芯片

原理：
    ESP32 的 SDIO Slave 是纯软件初始化的外设，必须由 app.bin（esp-hosted CP 固件）
    启动后配置 SDIO 数据通路，卡才会对 CMD5/CMD52 应答。固件没烧或没跑起来，
    主机侧看到的就是"总线空闲高电平 + 命令零应答"，与空槽无区别。

    esptool 的 --before default-reset --after hard-reset 会通过 RTS/DTR 拉 EN/IO0，
    这同时就是 README §5.8 所说的"唯一可靠的 C5 硬复位"手段。
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 固件在项目根的 firmware/ 下
FW = os.path.normpath(os.path.join(HERE, "..", "firmware"))

FLASH_ARGS = ["--flash-mode", "dio", "--flash-size", "4MB", "--flash-freq", "80m"]

PARTITIONS = [
    ("0x2000", "bootloader.bin"),
    ("0x8000", "partition-table.bin"),
    ("0xd000", "ota_data_initial.bin"),
    ("0x10000", "app.bin"),
]


def run(cmd, timeout=300):
    print("  $ " + " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        print("    " + line)
    return p.returncode, out


def esptool(args, timeout=300):
    """优先用已安装的 esptool 模块，退回命令行"""
    return run([sys.executable, "-m", "esptool"] + args, timeout=timeout)


def list_ports():
    from serial.tools import list_ports as lp
    return [p.device for p in lp.comports()]


def probe(port):
    """探测该串口上是不是 ESP32-C5（只认成功握手后的 Chip is 行）"""
    try:
        rc, out = esptool(["--chip", "esp32c5", "-p", port, "-b", "115200",
                           "--before", "default-reset", "--after", "hard-reset",
                           "chip-id"], timeout=45)
    except subprocess.TimeoutExpired:
        return False, "超时"
    upper = out.upper()
    if rc == 0 and "CHIP IS ESP32-C5" in upper:
        return True, out
    return False, out


def hard_reset(port):
    """不烧录，仅通过 RTS/DTR 给 C5 一次硬复位（EN 低脉冲后放开）"""
    import serial
    s = serial.Serial(port, 115200, timeout=0.2)
    # 常见接线：DTR->EN, RTS->IO0（低有效）
    s.dtr = False   # EN 拉低 = 复位
    s.rts = True    # IO0 高 = 正常启动
    time.sleep(0.15)
    s.dtr = True    # 释放 EN
    time.sleep(0.05)
    s.rts = False
    s.close()
    print("  已发出硬复位脉冲（EN 低 -> 高，IO0 保持高）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="串口名，如 COM7")
    ap.add_argument("--app", action="store_true", help="只烧 app.bin 到 0x10000")
    ap.add_argument("--reset", action="store_true", help="只做硬复位")
    ap.add_argument("--list", action="store_true", help="只列出串口并探测")
    args = ap.parse_args()

    print("[1] 检查固件目录 %s" % FW)
    missing = [f for _, f in PARTITIONS if not os.path.isfile(os.path.join(FW, f))]
    for _, f in PARTITIONS:
        p = os.path.join(FW, f)
        print("    %-24s %s" % (f, "%d 字节" % os.path.getsize(p) if os.path.isfile(p) else "缺失"))
    if missing and not args.reset:
        print("ERROR: 缺少固件文件 %s" % missing)
        return 1

    print("\n[2] 串口枚举")
    ports = list_ports()
    print("    检测到: %s" % (ports or "无"))

    target = args.port
    if args.reset:
        if not target:
            print("ERROR: --reset 需要 --port")
            return 1
        hard_reset(target)
        return 0

    if args.list:
        print("\n[3] 逐个探测（只检测，不烧录）")
        for p in ports:
            print("  -- 探测 %s" % p)
            ok, _ = probe(p)
            print("     %s" % ("命中 ESP32-C5" if ok else "不是 C5 / 无响应"))
        return 0

    if not target:
        print("\n[3] 自动探测 ESP32-C5")
        for p in ports:
            print("  -- 探测 %s" % p)
            ok, out = probe(p)
            if ok:
                target = p
                print("     >>> 命中 ESP32-C5: %s" % p)
                break
        if not target:
            print("\n未在任何串口上找到 ESP32-C5。请确认：")
            print("  1) USB 转 TTL 已接到 C5 的 TX/RX/EN/IO0/GND")
            print("  2) 没有其他程序（串口助手/MobaXterm）占用该端口")
            print("  3) 若换线后仍不行，尝试 --port 显式指定")
            return 1

    print("\n[4] 开始烧录到 %s" % target)
    if args.app:
        cmd = ["--chip", "esp32c5", "-p", target, "-b", "460800",
               "--before", "default-reset", "--after", "hard-reset",
               "write-flash"] + FLASH_ARGS + ["0x10000", os.path.join(FW, "app.bin")]
    else:
        cmd = ["--chip", "esp32c5", "-p", target, "-b", "460800",
               "--before", "default-reset", "--after", "hard-reset",
               "write-flash"] + FLASH_ARGS
        for off, name in PARTITIONS:
            cmd += [off, os.path.join(FW, name)]

    try:
        rc, out = esptool(cmd, timeout=900)
    except subprocess.TimeoutExpired:
        print("ERROR: 烧录超时")
        return 1

    if rc != 0 or ("Hash of data verified" not in out and "Wrote" not in out):
        print("\nERROR: 烧录失败，返回码 %d" % rc)
        return 1

    print("\n[5] 烧录成功。请按以下顺序验证：")
    print("    a) 保持串口打开 @115200，复位一次，应看到：")
    print("         app_init: Project name:     tsf_uart_bridge_mode2_cp")
    print("         SDIO_SLAVE: SDIO datapath mode: SW_AGGR")
    print("    b) AX650 上必须冷重启（不可热解绑，会 panic）：")
    print("         sync; reboot")
    print("    c) 重启后: cd /soc/sdio_wifi && ./bt.sh up")
    print("       期望: ls /sys/bus/sdio/devices/ 出现 mmc1:0001:1 和 mmc1:0001:2")
    print("       期望: dmesg 里 capabilities: 0xd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
