#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传并写入 DTB 到 /dev/mmcblk0p6，回读校验"""
import hashlib, os, sys, time
import paramiko

HOST, USER, PWD = "192.168.137.77", "root", "123456"
LOCAL = sys.argv[1] if len(sys.argv) > 1 else "dtb_p6_v5.img"
REMOTE = "/tmp/" + os.path.basename(LOCAL)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PWD, timeout=15)


def run(cmd, verbose=True):
    si, so, se = c.exec_command(cmd, timeout=120)
    out = so.read().decode(errors="replace")
    err = se.read().decode(errors="replace")
    if verbose:
        print("$ " + cmd)
        if out.strip():
            print(out.rstrip())
        if err.strip():
            print("[err]", err.rstrip())
    return out, err


local_md5 = hashlib.md5(open(LOCAL, "rb").read()).hexdigest()
print("本地 %s  size=%d  md5=%s" % (LOCAL, os.path.getsize(LOCAL), local_md5))

sftp = c.open_sftp()
t0 = time.time()
sftp.put(LOCAL, REMOTE)
sftp.close()
print("上传完成 %.1fs" % (time.time() - t0))

run("md5sum %s" % REMOTE)
run("dd if=%s of=/dev/mmcblk0p6 bs=1M conv=fsync 2>&1" % REMOTE)
run("sync")
run("dd if=/dev/mmcblk0p6 of=/tmp/readback.img bs=1M count=1 2>&1")
out, _ = run("md5sum /tmp/readback.img")
remote_md5 = out.split()[0] if out.split() else "?"
print("\n本地 md5 = %s\n板上 md5 = %s\n一致: %s" %
      (local_md5, remote_md5, local_md5 == remote_md5))
c.close()
