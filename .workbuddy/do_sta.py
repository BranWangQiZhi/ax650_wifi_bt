# -*- coding: utf-8 -*-
"""冷重启 AX650 后干净地启动 STA。

背景：wifi-sta.sh 第 74-78 行会 kill 掉已存在的 wifi-role，而当前那个进程
处于热重枚举后的半初始化状态（probe -22 / ethsta0 DOWN / MAC 全 0）。
直接 kill 会把 C5 卡死（README 5.8，只能串口 RTS 硬复位）。所以先 reboot，
让 S99sdio_wifi 重新干净地加载驱动，此时没有 wifi-role 可 kill，脚本走全新启动路径。

用法: python do_sta.py
"""
import os
import sys
import time

import paramiko

HOST, USER, PWD = "192.168.137.77", "root", "123456"
HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.normpath(os.path.join(HERE, "..", "scripts", "udhcpc-tsf.sh"))
STA_CMD = "cd /soc/sdio_wifi && WIFI_SSID='1303' WIFI_PASS='88889999' ./wifi-sta.sh 2>&1"


def connect(timeout=8):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PWD, timeout=timeout,
              banner_timeout=timeout, auth_timeout=timeout)
    return c


def run(c, cmd, timeout=60):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace").rstrip()
    err = e.read().decode("utf-8", "replace").strip()
    return out + (("\n[stderr] " + err) if err else "")


def uptime(c):
    try:
        return float(run(c, "cat /proc/uptime").split()[0])
    except Exception:
        return -1.0


def main():
    c = connect()
    old = uptime(c)
    print("[1] 当前 uptime=%.1fs" % old, flush=True)

    print("[2] 部署缺失的 udhcpc-tsf.sh", flush=True)
    sftp = c.open_sftp()
    sftp.put(HOOK, "/soc/sdio_wifi/udhcpc-tsf.sh")
    sftp.close()
    print("    " + run(c, "chmod 755 /soc/sdio_wifi/udhcpc-tsf.sh; "
                        "ls -l /soc/sdio_wifi/udhcpc-tsf.sh"), flush=True)

    print("[3] 触发 reboot（后台延迟 2s，避免 SSH 断连报错）", flush=True)
    print("    " + run(c, "sync; sync; (sleep 2; reboot) >/dev/null 2>&1 & "
                         "echo reboot-scheduled", timeout=10), flush=True)
    c.close()

    print("[4] 等待新内核上线（最多 240s）…", flush=True)
    t0 = time.time()
    c = None
    while time.time() - t0 < 240:
        time.sleep(4)
        try:
            c2 = connect(timeout=5)
            u = uptime(c2)
            if 0 <= u < old / 2:
                c = c2
                print("    上线，uptime=%.1fs，用时 %.0fs" % (u, time.time() - t0), flush=True)
                break
            c2.close()
        except Exception:
            pass
    if c is None:
        print("!! 240s 内未检测到新内核，板子可能没起来", flush=True)
        return 3

    print("[5] 等 SDIO 枚举 + S99sdio_wifi 自动加载 ko（最多 90s）…", flush=True)
    t0 = time.time()
    ready = False
    while time.time() - t0 < 90:
        r = run(c, "echo -n sdio=; ls /sys/bus/sdio/devices/ 2>/dev/null | "
                   "grep -c 'mmc1:0001'; echo -n ko=; lsmod | grep -c '^esp32_sdio'")
        if "sdio=2" in r and "ko=1" in r:
            ready = True
            print("    " + r.replace("\n", " "), flush=True)
            break
        time.sleep(3)
    if not ready:
        print("    " + r.replace("\n", " ") + "  -> 未就绪，中止（不做任何 kill）", flush=True)
        c.close()
        return 4

    # README 5.10：cmdline 是 console=ttyS0,115200n8 loglevel=8，驱动每个包
    # 打一条 INFO 会同步刷串口，实测把 Wi-Fi 从 45 Mbps 拖到 14 Mbps。压到
    # level 5 只保留 WARN/ERR，避免逐包 printk 拖垮 SDIO 写通道。
    run(c, "dmesg -n 5")
    print("[5b] dmesg -n 5（压制逐包 INFO，避免串口拖慢 SDIO）", flush=True)

    print("[6] 确认没有残留 wifi-role（否则 wifi-sta.sh 会 kill 它）", flush=True)
    procs = run(c, "for p in /proc/[0-9]*/comm; do "
                   "[ \"$(cat $p 2>/dev/null)\" = wifi-role ] && echo $p; done").strip()
    if procs:
        print("!! 仍有 wifi-role 进程: " + procs + "，为保护 C5 中止", flush=True)
        c.close()
        return 5
    print("    无残留，可以安全启动", flush=True)

    print("[7] 执行: " + STA_CMD, flush=True)
    print("-" * 60, flush=True)
    print(run(c, STA_CMD, timeout=300), flush=True)
    print("-" * 60, flush=True)

    print("[8] 验收", flush=True)
    checks = [
        "ifconfig | grep -E '^[a-z]'",
        "ip -4 -o addr show dev ethsta0 2>&1",
        "ip route",
        "cat /run/tsf/wifi.env 2>/dev/null || echo '(no wifi.env)'",
        "tail -12 /soc/tsf/logs/wifi-sta.log 2>&1",
        "dmesg | grep -E 'new SDIO card|peripheral capabilities' | tail -4",
        "echo -n 'H2E retries='; dmesg | grep -c 'H2E write retry'; "
        "echo -n 'slave stalled='; dmesg | grep -c 'slave stalled'",
    ]
    for cmd in checks:
        print("$ " + cmd + "\n" + run(c, cmd) + "\n", flush=True)

    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
