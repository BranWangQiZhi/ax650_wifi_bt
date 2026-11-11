#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
等待板子断电 -> 重新上电 -> 自动做一次完整的 SDIO/WiFi/BT 体检。
用法: python wait_coldboot.py [掉线等待秒] [上线等待秒]
"""
import sys, time, paramiko

HOST, USER, PWD = "192.168.137.77", "root", "123456"


def probe(timeout=6):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(HOST, username=USER, password=PWD, timeout=timeout,
                  banner_timeout=timeout, auth_timeout=timeout)
        return c
    except Exception:
        return None


def run(c, cmds):
    out = []
    for cmd in cmds:
        _, o, e = c.exec_command(cmd, timeout=40)
        t = o.read().decode("utf-8", "replace").rstrip()
        err = e.read().decode("utf-8", "replace").strip()
        out.append("$ " + cmd + "\n" + t + (("\n[err] " + err) if err else ""))
    return "\n\n".join(out)


def main():
    t_down = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    t_up = int(sys.argv[2]) if len(sys.argv) > 2 else 300

    # 阶段 1：确认它确实断电了
    print("[1] 等待板子掉线（请现在断电）…", flush=True)
    c = probe()
    if c is None:
        print("    现在已经是离线状态，直接进入下一步")
    else:
        print("    当前在线，开始监测掉线（最多 %ds）" % t_down)
        gone = False
        t0 = time.time()
        while time.time() - t0 < t_down:
            c.close()
            time.sleep(4)
            c = probe()
            if c is None:
                gone = True
                print("    检测到掉线，用时 %.0fs" % (time.time() - t0), flush=True)
                break
        if not gone:
            print("!! %ds 内没有检测到掉线 —— 板子还在跑。" % t_down)
            print("   如果已经断电但网口还在供电，请确认真的断了。")
            return 2

    # 阶段 2：等它回来
    print("[2] 等待重新上电并启动（最多 %ds，请现在上电）…" % t_up, flush=True)
    t0 = time.time()
    c = None
    while time.time() - t0 < t_up:
        c = probe()
        if c is not None:
            print("    板子已上线，用时 %.0fs" % (time.time() - t0), flush=True)
            break
        time.sleep(5)
    if c is None:
        print("!! 超时未上线")
        return 3

    time.sleep(8)  # 等 mmc 子系统枚举完

    cmds = [
        "mount -t debugfs none /sys/kernel/debug 2>/dev/null; "
        "echo '=== SDIO 设备 ==='; ls /sys/bus/sdio/devices/ 2>&1; echo '(空=未枚举)'",
        "echo '=== mmc 主机 ==='; ls /sys/class/mmc_host/",
        "echo '=== mmc1 关键日志 ==='; dmesg | grep -v 'sdhci:' | grep -iE 'mmc1|sdio' | head -20",
        "echo '=== 引脚 ==='; mount -t debugfs none /sys/kernel/debug 2>/dev/null; "
        "grep -E 'pin (139|140|141|142|143|144|90) ' /sys/kernel/debug/pinctrl/4250200.pinctrl/pinmux-pins",
        "echo '=== 时钟/供电 ==='; cat /sys/kernel/debug/mmc1/ios 2>/dev/null | head -6",
        "echo '=== 驱动与设备节点 ==='; lsmod | grep -i esp; ls -l /dev/esps* /sys/class/bluetooth/ 2>&1",
    ]
    print("[3] 体检结果\n" + "=" * 60)
    print(run(c, cmds))
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
