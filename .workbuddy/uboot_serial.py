# -*- coding: utf-8 -*-
"""与 AX650 U-Boot 串口交互的助手模块（同步到提示符后发送命令）"""
import serial
import time

PORT = "COM3"
BAUD = 115200
PROMPT = "AXERA-UBOOT=>"


class UBoot:
    def __init__(self):
        self.s = serial.Serial(PORT, BAUD, timeout=1)

    def wait_prompt(self, timeout=60):
        """等待 U-Boot 提示符出现（板子可能在重启中）"""
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            buf += self.s.read(8192)
            text = buf.decode("utf-8", "replace")
            if text.rstrip().endswith(PROMPT):
                return text
        raise TimeoutError(f"等待提示符超时, 最后输出:\n{buf[-2000:].decode('utf-8','replace')}")

    def send(self, cmd, timeout=30):
        """发送一条命令并读取直到出现提示符（命令回显+输出+提示符）"""
        self.s.reset_input_buffer()
        self.s.write((cmd + "\n").encode())
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            buf += self.s.read(16384)
            text = buf.decode("utf-8", "replace")
            if text.rstrip().endswith(PROMPT):
                return text
            # 若发生看门狗复位（出现 U-Boot 横幅），等待重新进入提示符
            if "U-Boot 2020.04" in text:
                pass  # 继续读，直到新会话打印提示符
        return buf.decode("utf-8", "replace")

    def close(self):
        self.s.close()
