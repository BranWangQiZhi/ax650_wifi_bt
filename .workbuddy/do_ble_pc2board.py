# -*- coding: utf-8 -*-
"""PC -> 板子 BLE 数据链路实测：PC 发 WinRT 广播，板子 bt.sh scan 接收。"""
import os
import subprocess
import sys
import time

import paramiko

HOST, USER, PWD = "192.168.137.77", "root", "123456"
ROOT = r"D:/project/ax650/03.Report/wifi-bt"
PY = sys.executable
SECS = 12


def main():
    pc = subprocess.Popen(
        [PY, os.path.join(ROOT, "app", "linktest.py"), "ble", "beacon",
         "--data", "HELLO-1303", "--seconds", str(SECS)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.5)

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=10)
    _, out, err = cli.exec_command(
        f"cd /soc/sdio_wifi && ./bt.sh scan {SECS}", timeout=60)
    o = out.read().decode("utf-8", "replace").strip()
    e = err.read().decode("utf-8", "replace").strip()
    print("[board] bt.sh scan 输出:")
    print(o or "(空)")
    if e:
        print("[board stderr]", e[:500])
    cli.close()

    pc.wait(timeout=30)
    print("[pc] 广播进程输出:")
    print((pc.stdout.read() or "").strip())


if __name__ == "__main__":
    main()
