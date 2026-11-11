# -*- coding: utf-8 -*-
"""YMODEM 发送器（用于 U-Boot loady）"""
import serial
import time


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class YModemSender:
    def __init__(self, ser: serial.Serial):
        self.s = ser

    def _read_bytes(self, timeout=3.0):
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            if self.s.in_waiting:
                buf += self.s.read(self.s.in_waiting)
                # 读到关键控制字符即可返回
                if any(c in buf for c in (0x06, 0x15, 0x43, 0x43)):
                    break
                time.sleep(0.01)
            else:
                time.sleep(0.01)
        return buf

    def _wait_for(self, chars, timeout=15.0):
        """等待指定控制字符出现（ACK=0x06 NAK=0x15 C=0x43）"""
        end = time.time() + timeout
        seen = b""
        while time.time() < end:
            if self.s.in_waiting:
                seen += self.s.read(self.s.in_waiting)
                if any(c in seen for c in chars):
                    return seen
            else:
                time.sleep(0.02)
        return seen  # 超时返回已读数据（可能为空）

    def _drain(self):
        """读空接收缓冲中残留的旧应答"""
        n = 0
        while self.s.in_waiting and n < 100:
            self.s.read(self.s.in_waiting)
            n += 1
            time.sleep(0.005)

    def _send_block(self, seq: int, payload: bytes):
        assert len(payload) in (128, 1024)
        hdr = 0x01 if len(payload) == 128 else 0x02
        frame = bytes([hdr, seq & 0xFF, (0xFF - seq) & 0xFF]) + payload
        frame += crc16_xmodem(payload).to_bytes(2, "big")
        self.s.write(frame)

    def send(self, filename: str, data: bytes, progress=None, block_size=1024):
        assert block_size in (128, 1024)
        # 1. 等待接收端 'C'
        r = self._wait_for((0x43,), timeout=40)
        if 0x43 not in r:
            raise TimeoutError("接收端未进入 CRC 模式: %r" % r)

        # 2. 块 0（文件名+大小）
        hdr = filename.encode() + b"\x00" + str(len(data)).encode() + b" "
        hdr = hdr.ljust(128, b"\x00")[:128]
        self._send_block(0, hdr)
        r = self._wait_for((0x06, 0x15), timeout=15)
        if 0x06 not in r:
            raise TimeoutError("块0 未收到 ACK: %r" % r)
        # 等待 'C' 开始数据块
        self._wait_for((0x43,), timeout=10)

        # 3. 数据块
        nblocks = (len(data) + block_size - 1) // block_size
        for i in range(nblocks):
            seq = (i + 1) & 0xFF
            payload = data[i * block_size:(i + 1) * block_size].ljust(block_size, b"\x1a")
            for retry in range(10):
                time.sleep(0.01)
                # 清掉残留的旧 ACK/NAK，避免状态机错位
                self._drain()
                self._send_block(seq, payload)
                r = self._wait_for((0x06, 0x15), timeout=8)
                if 0x06 in r:
                    break
            else:
                raise TimeoutError("数据块 %d 重试失败" % seq)
            if progress and (i + 1) % 64 == 0:
                progress(i + 1, nblocks)

        # 4. EOT（第一次可能被 NAK，重发）
        for attempt in range(3):
            self.s.write(b"\x04")
            r = self._wait_for((0x06, 0x15), timeout=10)
            if 0x06 in r:
                break
        # 5. 空块 0 结束会话（部分实现需要，超时忽略）
        try:
            self._send_block(0, b"\x00" * 128)
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    print("module only")
