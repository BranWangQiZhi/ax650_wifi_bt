# -*- coding: utf-8 -*-
"""持续监测 SD/SDIO 卡槽：一旦枚举到设备立刻报告并停止。
用户插拔卡片时无需通知，脚本会自动捕获。"""
import sys
import time
import paramiko

HOST, USER, PWD = "192.168.137.77", "root", "123456"
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 300

PROBE = (
    "mount -t debugfs none /sys/kernel/debug 2>/dev/null; "
    "echo \"SDIO=[$(ls /sys/bus/sdio/devices/ 2>/dev/null | tr '\\n' ' ')]\"; "
    "echo \"MMC=[$(ls /sys/bus/mmc/devices/ 2>/dev/null | tr '\\n' ' ')]\"; "
    "echo \"CD=$(devmem 0x30E00024 2>/dev/null)\""
)

RESCAN = (
    "echo 30e00000.sdhc > /sys/bus/platform/drivers/sdhci-axera/unbind 2>/dev/null; "
    "sleep 1; dmesg -c >/dev/null 2>&1; "
    "echo 30e00000.sdhc > /sys/bus/platform/drivers/sdhci-axera/bind 2>/dev/null"
)


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PWD, timeout=8)
    return c


def run(c, cmd, timeout=40):
    _, o, _ = c.exec_command(cmd, timeout=timeout)
    return o.read().decode("utf-8", "replace").strip()


print("[监测] 持续 %d 秒，等待卡槽枚举出设备…" % DURATION)
print("       每次 rescan 前会清空 dmesg，成功时把日志打出来\n")

end = time.time() + DURATION
last_cd = None
cli = None
n = 0
while time.time() < end:
    try:
        if cli is None:
            cli = connect()
        n += 1
        info = run(cli, PROBE)
        line = " ".join(x for x in info.splitlines() if x.strip())
        cd = line.split("CD=")[-1].strip() if "CD=" in line else "?"

        mark = ""
        if last_cd is not None and cd != last_cd:
            mark = "   <-- 卡检测位变化 %s -> %s" % (last_cd, cd)
        last_cd = cd
        print("[%3d] %s%s" % (n, line, mark))

        if "SDIO=[" in line and "SDIO=[]" not in line:
            print("\n*** 枚举到 SDIO 设备！***")
            print(run(cli, "ls -l /sys/bus/sdio/devices/; dmesg | grep -v 'sdhci:' | grep -iE 'mmc1|sdio' | head -25"))
            break
        if "MMC=[" in line and "mmc1:" in line:
            print("\n*** mmc1 上有卡被识别（普通 SD 卡测试成功）***")
            print(run(cli, "cat /sys/bus/mmc/devices/mmc1:*/name 2>/dev/null; "
                           "cat /sys/bus/mmc/devices/mmc1:*/type 2>/dev/null; "
                           "dmesg | grep -v 'sdhci:' | grep -i mmc1 | head -20"))
            break

        if n % 4 == 0:
            print("      …重新枚举一次")
            run(cli, RESCAN, timeout=60)
        time.sleep(4)
    except Exception as e:
        print("      [warn] %s: %s" % (type(e).__name__, e))
        try:
            cli.close()
        except Exception:
            pass
        cli = None
        time.sleep(3)

if cli:
    cli.close()
print("\n[监测结束]")
