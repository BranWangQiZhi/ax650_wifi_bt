# -*- coding: utf-8 -*-
"""链路验收：部署板端程序，跑 WiFi TCP echo（PC<->板子）与 BLE 广播（板子->PC）。

前置：PC 已 `pip install bleak ziglang`；交叉编译产物在 app/ 下：
  app/bt-extadv-tool  (aarch64, 修好 adv_data_len)
  app/linktest        (aarch64, AF_INET 收发)
用法: python .workbuddy/do_linktest.py
"""
import os
import subprocess
import sys
import time

import paramiko

HOST, USER, PWD = "192.168.137.77", "root", "123456"
BOARD_WIFI_IP = "192.168.0.92"          # ethsta0，经热点 1303 走空口
ROOT = r"D:/project/ax650/03.Report/wifi-bt"
REMOTE = "/soc/sdio_wifi"
PY = sys.executable
PC = os.path.join(ROOT, "app", "linktest.py")


def board(cli, cmd, timeout=60):
    _, out, err = cli.exec_command(cmd, timeout=timeout)
    return (out.read().decode("utf-8", "replace").strip(),
            err.read().decode("utf-8", "replace").strip())


def start(cli, cmd):
    """保持 SSH session 让远端前台进程持续运行，返回 session。"""
    s = cli.get_transport().open_session()
    s.set_combine_stderr(True)
    s.exec_command(cmd)
    return s


def pc(*args, timeout=120):
    r = subprocess.run([PY, *args], capture_output=True, text=True, timeout=timeout)
    print(r.stdout.strip() or r.stderr.strip())
    return r.returncode


def main():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=10)

    # 1) 部署（板子上的原广播工具备份一次）
    print("[1] 部署板端程序")
    print(board(cli, f"[ -f {REMOTE}/bt-extadv-tool.bak ] || "
                     f"cp {REMOTE}/bt-extadv-tool {REMOTE}/bt-extadv-tool.bak; "
                     "echo backup-ok")[0])
    sftp = cli.open_sftp()
    for local, name in [("scripts/linktest-board.sh", "linktest-board.sh"),
                        ("app/bt-extadv-tool", "bt-extadv-tool"),
                        ("app/linktest", "linktest")]:
        lp = os.path.join(ROOT, local.replace("/", os.sep))
        if not os.path.isfile(lp):
            print(f"  ! 缺少 {lp}，跳过")
            continue
        sftp.put(lp, f"{REMOTE}/{name}")
        sftp.chmod(f"{REMOTE}/{name}", 0o755)
        print(f"  {name}: {os.path.getsize(lp)} B")
    sftp.close()
    print(board(cli, f"cd {REMOTE} && sh linktest-board.sh status")[0])

    # 2) WiFi：板子起 echo server，PC 分两种 block 做 ping-pong
    print("\n[2] WiFi (AF_INET, ethsta0 空口)")
    srv = start(cli, f"cd {REMOTE} && sh linktest-board.sh wifi server tcp 9000")
    time.sleep(1.5)
    print(board(cli, "netstat -ltn 2>/dev/null | grep 9000 || echo no-listener")[0])
    for block, total in [(1024, 262144), (65536, 1048576)]:
        print(f"  -- PC client block={block} B --")
        pc(PC, "wifi", "client", "--host", BOARD_WIFI_IP, "--port", "9000",
           "--proto", "tcp", "--bytes", str(total), "--block", str(block))
    print("  -- PC client stream 4 MiB（测带宽）--")
    pc(PC, "wifi", "client", "--host", BOARD_WIFI_IP, "--port", "9000",
       "--proto", "tcp", "--bytes", "4194304", "--block", "65536", "--stream")

    # 3) 交叉编译的 C 版 linktest 在板子上跑一次（连本机 echo server）
    if os.path.isfile(os.path.join(ROOT, "app", "linktest")):
        print("  -- 板子 C 版 linktest client（交叉编译产物自检）--")
        print(board(cli, f"cd {REMOTE} && ./linktest client 127.0.0.1 tcp 9000 65536 4096")[0])
    srv.close()

    # 4) BLE：板子广播，PC 扫描
    print("\n[3] BLE (广播承载数据)")
    adv = start(cli, f"cd {REMOTE} && sh linktest-board.sh ble beacon HELLO-WIFI-BT 20")
    time.sleep(2)
    print(board(cli, "ps | grep '[b]t-extadv-tool'")[0])
    pc(PC, "ble", "scan", "--seconds", "12", "--prefix", "LNK:", timeout=90)
    adv.settimeout(1.0)
    buf = b""
    try:
        while True:
            d = adv.recv(65535)
            if not d:
                break
            buf += d
    except Exception:
        pass
    print("  广播工具输出:", buf.decode("utf-8", "replace")[:600].replace("\n", " | "))
    adv.close()

    # 5) 收尾：只清测试相关进程，绝不碰 wifi-role
    board(cli, "for p in /proc/[0-9]*/cmdline; do "
               "c=$(tr '\\0' ' ' < $p 2>/dev/null); "
               "case \"$c\" in *'-lk -p 9000'*|*'bt-extadv-tool LNK:'*) "
               "pid=${p#/proc/}; pid=${pid%/cmdline}; "
               "kill $pid 2>/dev/null;; esac; done; echo cleaned")
    cli.close()


if __name__ == "__main__":
    main()
