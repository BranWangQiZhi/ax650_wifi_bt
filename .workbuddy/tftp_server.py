# -*- coding: utf-8 -*-
"""最小 TFTP 服务器（RFC1350 + blksize/timeout 选项），供 U-Boot tftpboot 使用"""
import socket
import struct
import sys
import os
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
OP_RRQ, OP_WRQ, OP_DATA, OP_ACK, OP_ERROR, OP_OACK = 1, 2, 3, 4, 5, 6


def handle(req, addr, bind_ip):
    parts = req[2:].split(b"\x00")
    filename = parts[0].decode(errors="replace")
    opts = {}
    for i in range(2, len(parts) - 1, 2):
        if parts[i]:
            opts[parts[i].decode(errors="replace").lower()] = parts[i + 1].decode(errors="replace")
    print(f"[tftp] RRQ {filename} opts={opts} from {addr}", flush=True)

    # 独立会话 socket（TFTP 标准：服务器换用新端口）
    ds = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ds.bind((bind_ip, 0))
    ds.settimeout(4)

    path = os.path.join(ROOT, filename)
    if not os.path.isfile(path):
        ds.sendto(struct.pack("!HH", OP_ERROR, 1) + b"File not found\x00", addr)
        ds.close()
        return
    data = open(path, "rb").read()
    print(f"[tftp] serving {len(data)} bytes", flush=True)

    blksize = 512
    if "blksize" in opts:
        try:
            blksize = max(8, min(1468, int(opts["blksize"])))
        except ValueError:
            pass
    # OACK：只回显我们支持的选项
    oack_opts = b""
    if "blksize" in opts:
        oack_opts += b"blksize\x00" + str(blksize).encode() + b"\x00"
    if "timeout" in opts:
        oack_opts += b"timeout\x00" + opts["timeout"].encode() + b"\x00"

    def wait_ack(seq):
        for _ in range(8):
            try:
                r, _a = ds.recvfrom(1024)
            except socket.timeout:
                return False
            if len(r) >= 4 and struct.unpack("!H", r[:2])[0] == OP_ACK \
                    and struct.unpack("!H", r[2:4])[0] == (seq & 0xFFFF):
                return True
        return False

    off = 0
    if oack_opts:
        ds.sendto(struct.pack("!H", OP_OACK) + oack_opts, addr)
        if not wait_ack(0):
            print("[tftp] OACK 未确认", flush=True)
            ds.close()
            return
    seq = 1
    while True:
        chunk = data[off:off + blksize]
        pkt = struct.pack("!HH", OP_DATA, seq & 0xFFFF) + chunk
        ok = False
        for _ in range(8):
            ds.sendto(pkt, addr)
            if wait_ack(seq):
                ok = True
                break
        if not ok:
            print(f"[tftp] seq {seq} 失败", flush=True)
            ds.close()
            return
        off += blksize
        seq += 1
        if len(chunk) < blksize:
            print(f"[tftp] 完成 {off}/{len(data)} bytes", flush=True)
            ds.close()
            return


def main():
    bind_ip = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind_ip, 69))
    print(f"[tftp] listening {bind_ip}:69 root={ROOT}", flush=True)
    while True:
        req, addr = s.recvfrom(2048)
        if len(req) < 4:
            continue
        if struct.unpack("!H", req[:2])[0] != OP_RRQ:
            continue
        threading.Thread(target=handle, args=(req, addr, bind_ip), daemon=True).start()


if __name__ == "__main__":
    main()
