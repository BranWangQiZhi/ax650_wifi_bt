# -*- coding: utf-8 -*-
"""BLE 链路诊断：板子发广播，PC 全量扫描，打印两边证据。"""
import os
import subprocess
import sys
import time

import paramiko

HOST, USER, PWD = "192.168.137.77", "root", "123456"
ROOT = r"D:/project/ax650/03.Report/wifi-bt"
PY = sys.executable


def board(cli, cmd, timeout=30):
    _, out, err = cli.exec_command(cmd, timeout=timeout)
    return (out.read().decode("utf-8", "replace").strip(),
            err.read().decode("utf-8", "replace").strip())


def main():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=10)

    sess = cli.get_transport().open_session()
    sess.set_combine_stderr(True)
    sess.exec_command("cd /soc/sdio_wifi && sh linktest-board.sh ble beacon HELLO-WIFI-BT 20")
    time.sleep(2)
    print("[board] 广播进程:")
    print(board(cli, "ps | grep -E '[b]t-extadv|[l]inktest-board'")[0] or "(无)")
    print("[board] 最近 BT 相关内核日志:")
    print(board(cli, "dmesg | tail -40 | grep -iE 'bluetooth|hci|esp32' || echo '(无)'")[0])

    cmd = [PY, os.path.join(ROOT, "app", "linktest.py"), "ble", "scan",
           "--seconds", "12", "--all"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    print("[pc] BLE 扫描:")
    print(r.stdout.strip() or "(无输出)")
    if r.stderr.strip():
        print("[pc stderr]", r.stderr.strip()[:2000])

    sess.settimeout(1.0)
    buf = b""
    try:
        while True:
            d = sess.recv(65535)
            if not d:
                break
            buf += d
    except Exception:
        pass
    print("[board] 广播工具输出（前 2500 字符）:")
    print(buf.decode("utf-8", "replace")[:2500] or "(无)")
    sess.close()
    cli.close()


if __name__ == "__main__":
    main()
