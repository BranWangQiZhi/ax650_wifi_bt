# -*- coding: utf-8 -*-
"""部署 WiFi/BT 文件到 AX650: python deploy.py"""
import os
import stat
import paramiko

HOST, USER, PWD = "192.168.137.77", "root", "123456"
LOCAL = r"D:/project/ax650/03.Report/wifi-bt"
REMOTE = "/soc/sdio_wifi"

FILES = [
    ("driver/esp32_sdio-ax650-5.15.73-bt.ko", "esp32_sdio.ko"),
    ("app/wifi-role", "wifi-role"),
    ("app/bt-mgmt-tool", "bt-mgmt-tool"),
    ("app/bt-extadv-tool", "bt-extadv-tool"),
    ("app/bt-hci-tool", "bt-hci-tool"),
    ("scripts/common.sh", "common.sh"),
    ("scripts/bt.sh", "bt.sh"),
    ("scripts/wifi-ap.sh", "wifi-ap.sh"),
    ("scripts/wifi-sta.sh", "wifi-sta.sh"),
    ("config/wifi.conf", "wifi.conf"),
]

def main():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=10)
    sftp = cli.open_sftp()

    def exec(cmd):
        _, out, err = cli.exec_command(cmd, timeout=60)
        o, e = out.read().decode().strip(), err.read().decode().strip()
        return o, e

    o, e = exec(f"mkdir -p {REMOTE}")
    if e:
        print("[mkdir stderr]", e)

    for src, dst in FILES:
        lp = os.path.join(LOCAL, src.replace("/", os.sep))
        rp = f"{REMOTE}/{dst}"
        size = os.path.getsize(lp)
        sftp.put(lp, rp)
        # 755 权限
        sftp.chmod(rp, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        print(f"OK  {src} -> {rp}  ({size} bytes)")

    # 校验
    o, _ = exec(f"ls -l {REMOTE}")
    print("\n== 板上目录 ==")
    print(o)

    # 注意：wifi.conf 若板上已存在则不应覆盖 —— 本脚本未做该判断，因为首次部署
    sftp.close()
    cli.close()

if __name__ == "__main__":
    main()
