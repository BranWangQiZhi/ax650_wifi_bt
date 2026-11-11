#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linktest.py -- PC 侧链路收发测试程序（板子对端见 scripts/linktest-board.sh）

两套链路用 --link 选择，但能力不对称，这是本板硬件决定的：
  wifi : AF_INET TCP/UDP socket，双向、可靠、可跑吞吐
  ble  : 只能扫描 C5 的 BLE 广播（单向、小数据、无连接），因为板子是
         BLE-only，BT PAN(BNEP) 起不来，也没有 GATT 服务端

用法：
  python linktest.py wifi server --port 9000 [--proto tcp|udp] [--bind 0.0.0.0]
  python linktest.py wifi client --host 192.168.0.92 --port 9000 \
         [--proto tcp|udp] [--bytes 1048576] [--block 1024]
  python linktest.py ble scan --seconds 8 [--prefix LNK:]
  python linktest.py info

依赖：仅标准库；ble 模式额外需要 `python -m pip install bleak`（WinRT 后端）。
"""
import argparse
import asyncio
import socket
import sys
import threading
import time

BLOCK_MAX = 65536


def human(n):
    return f"{n / 1024 / 1024:.2f} MiB" if n >= 1024 * 1024 else f"{n} B"


def srv_tcp(args):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((args.bind, args.port))
    s.listen(4)
    print(f"[wifi] TCP echo server {args.bind}:{args.port}  (Ctrl-C 结束)", flush=True)
    try:
        while True:
            c, addr = s.accept()
            total, t0 = 0, time.perf_counter()
            with c:
                while True:
                    data = c.recv(65536)
                    if not data:
                        break
                    c.sendall(data)
                    total += len(data)
            dt = time.perf_counter() - t0
            print(f"[wifi] {addr[0]} echoed {human(total)} in {dt:.3f}s "
                  f"({total * 8 / 1e6 / dt:.2f} Mbps)" if dt > 0 else "")
    except KeyboardInterrupt:
        pass
    finally:
        s.close()


def srv_udp(args):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((args.bind, args.port))
    print(f"[wifi] UDP echo server {args.bind}:{args.port}  (Ctrl-C 结束)", flush=True)
    total, t0 = 0, time.perf_counter()
    try:
        while True:
            data, addr = s.recvfrom(65535)
            s.sendto(data, addr)
            total += len(data)
            dt = time.perf_counter() - t0
            if dt >= 5.0:
                print(f"[wifi] echoed {human(total)}  {total * 8 / 1e6 / dt:.2f} Mbps")
                total, t0 = 0, time.perf_counter()
    except KeyboardInterrupt:
        pass
    finally:
        s.close()


def stream_client(args):
    """连续发送再统一收回显，用来测带宽（ping-pong 模式测的是 RTT）。"""
    udp = args.proto == "udp"
    s = socket.socket(socket.AF_INET,
                      socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    s.settimeout(5.0)
    if not udp:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            s.connect((args.host, args.port))
        except OSError as e:
            print(f"[wifi] 连接 {args.host}:{args.port} 失败: {e}")
            s.close()
            return 1
    block = min(args.block, 1400 if udp else 65536)
    pattern = bytes((i * 31 + 7) & 0xFF for i in range(block))
    sent = got = 0
    t0 = time.perf_counter()

    def reader():
        nonlocal got
        while got < args.bytes:
            try:
                part = s.recvfrom(65535)[0] if udp else s.recv(65536)
            except (socket.timeout, OSError):
                break
            if not part:
                break
            got += len(part)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        while sent < args.bytes:
            n = min(block, args.bytes - sent)
            chunk = pattern[:n]
            if udp:
                s.sendto(chunk, (args.host, args.port))
            else:
                s.sendall(chunk)
            sent += n
        t.join(timeout=8.0)
    except OSError as e:
        print(f"[wifi] 收发失败: {e}")
        return 1
    finally:
        s.close()
    dt = time.perf_counter() - t0
    print(f"[wifi] stream: {human(sent)} sent / {human(got)} echoed  "
          f"{dt:.3f}s  {got * 8 / 1e6 / dt:.2f} Mbps")
    return 0 if got == sent else 1


def client(args):
    if args.stream:
        return stream_client(args)
    udp = args.proto == "udp"
    s = socket.socket(socket.AF_INET,
                      socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    s.settimeout(3.0)
    if not udp:
        try:
            s.connect((args.host, args.port))
        except OSError as e:
            print(f"[wifi] 连接 {args.host}:{args.port} 失败: {e}")
            s.close()
            return 1
    pattern = bytes((i * 31 + 7) & 0xFF for i in range(min(args.block, BLOCK_MAX)))
    sent = ok = bad = 0
    rtt_sum = 0.0
    t0 = time.perf_counter()
    print(f"[wifi] -> {args.host}:{args.port}  proto={args.proto}  "
          f"total={human(args.bytes)}  block={args.block} B", flush=True)
    try:
        while sent < args.bytes:
            n = min(args.block, args.bytes - sent)
            chunk = pattern[:n]
            t1 = time.perf_counter()
            if udp:
                s.sendto(chunk, (args.host, args.port))
            else:
                s.sendall(chunk)
            got = bytearray()
            while len(got) < n:
                try:
                    part = s.recvfrom(65535)[0] if udp else s.recv(65535)
                except socket.timeout:
                    break
                if not part:
                    break
                got += part
            rtt_sum += time.perf_counter() - t1
            if bytes(got) == chunk:
                ok += 1
            else:
                bad += 1
            sent += n
    except (ConnectionRefusedError, OSError) as e:
        print(f"[wifi] 连接/收发失败: {e}")
        return 1
    finally:
        s.close()
    dt = time.perf_counter() - t0
    print(f"[wifi] result: {human(sent)}  {ok} blocks ok / {bad} lost-or-corrupt  "
          f"{dt:.3f}s  {sent * 8 / 1e6 / dt:.2f} Mbps  "
          f"avg RTT {rtt_sum * 1000 / max(ok + bad, 1):.2f} ms")
    return 1 if bad else 0


async def ble_scan(args):
    try:
        from bleak import BleakScanner
    except ImportError:
        print("[ble] 缺少 bleak，先执行: python -m pip install bleak")
        return 2

    hits, seen, all_seen = 0, {}, 0

    def on_detect(dev, adv):
        nonlocal hits, all_seen
        name = getattr(adv, "local_name", None) or ""
        all_seen += 1
        all_flag = getattr(args, "all", False)
        if all_flag and len(seen) < 20:
            seen.setdefault(dev.address, name or "(no name)")
            print(f"[ble:all] {dev.address}  rssi={getattr(adv, 'rssi', '?')}  "
                  f"name={name!r}", flush=True)
        if args.prefix and not name.startswith(args.prefix):
            return
        rssi = getattr(adv, "rssi", None)
        if rssi is None:
            rssi = getattr(dev, "rssi", "?")
        payload = name[len(args.prefix):] if args.prefix else name
        hits += 1
        seen[dev.address] = (rssi, payload)
        print(f"[ble] {dev.address}  rssi={rssi}  "
              f"name={name!r}  data={payload!r}", flush=True)

    print(f"[ble] 扫描 {args.seconds}s，匹配广播名前缀 {args.prefix!r} ...", flush=True)
    try:
        scanner = BleakScanner(detection_callback=on_detect)
    except TypeError:
        scanner = BleakScanner(callback=on_detect)
    await scanner.start()
    try:
        await asyncio.sleep(args.seconds)
    finally:
        await scanner.stop()
    print(f"[ble] 共 {hits} 条匹配广播 / {len(seen)} 个设备 "
          f"（适配器共收到 {all_seen} 条广播事件）")
    if hits == 0:
        print("[ble] 诊断提示: 适配器事件数为 0 -> 检查 PC 蓝牙是否开启；"
              "事件数 >0 但无匹配 -> 检查板子广播是否已启动、名字前缀是否为 LNK:")
    return 0 if hits else 1


def info():
    print("本机 IPv4：")
    for line in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = line[4][0]
        if not ip.startswith("127."):
            print(f"  {ip}")
    print("\n板子（AX650）已知地址：")
    print("  eth0    192.168.137.77/24  (有线直连 PC)")
    print("  ethsta0 192.168.0.92/23    (WiFi 空口，走热点 1303)")
    print("  hci0    10:BD:A3:DF:1E:52  (BLE-only，无 bnep0)")
    print("\n提示：BT PAN 需要 bnep0，本板不存在；WiFi 与 BLE 是两条不同的编程接口。")
    return 0


def main():
    p = argparse.ArgumentParser(description="AX650 <-> PC 链路收发测试")
    sub = p.add_subparsers(dest="link", required=True)

    w = sub.add_parser("wifi", help="AF_INET TCP/UDP（ethsta0 / ethap0）")
    wsub = w.add_subparsers(dest="role", required=True)
    ws = wsub.add_parser("server")
    ws.add_argument("--port", type=int, default=9000)
    ws.add_argument("--proto", choices=["tcp", "udp"], default="tcp")
    ws.add_argument("--bind", default="0.0.0.0")
    wc = wsub.add_parser("client")
    wc.add_argument("--host", required=True)
    wc.add_argument("--port", type=int, default=9000)
    wc.add_argument("--proto", choices=["tcp", "udp"], default="tcp")
    wc.add_argument("--bytes", type=int, default=1024 * 1024)
    wc.add_argument("--block", type=int, default=1024)
    wc.add_argument("--stream", action="store_true",
                    help="连续发送不等回显，测带宽（默认逐块确认，测 RTT）")

    b = sub.add_parser("ble", help="扫描 C5 的 BLE 广播（单向小数据）")
    bsub = b.add_subparsers(dest="role", required=True)
    bs = bsub.add_parser("scan")
    bs.add_argument("--seconds", type=int, default=8)
    bs.add_argument("--prefix", default="LNK:")
    bs.add_argument("--all", action="store_true",
                    help="打印扫描到的所有 BLE 广播（诊断用）")

    sub.add_parser("info", help="打印两端链路信息")

    a = p.parse_args()
    if a.link == "info":
        return info()
    if a.link == "wifi":
        if a.role == "server":
            (srv_udp(a) if a.proto == "udp" else srv_tcp(a))
            return 0
        return client(a)
    if a.link == "ble":
        return asyncio.run(ble_scan(a))
    return 2


if __name__ == "__main__":
    sys.exit(main())
