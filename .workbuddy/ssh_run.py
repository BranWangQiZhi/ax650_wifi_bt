# -*- coding: utf-8 -*-
"""SSH 到 AX650 板子执行命令: python ssh_run.py "cmd1" "cmd2" ..."""
import sys
import paramiko

HOST, USER, PWD = "192.168.137.77", "root", "123456"

def main():
    cmds = sys.argv[1:]
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=10)
    try:
        for c in cmds:
            print(f"\n$ {c}")
            _, out, err = cli.exec_command(c, timeout=30)
            o = out.read().decode("utf-8", "replace").strip()
            e = err.read().decode("utf-8", "replace").strip()
            if o:
                print(o)
            if e:
                print("[stderr]", e)
    finally:
        cli.close()

if __name__ == "__main__":
    main()
