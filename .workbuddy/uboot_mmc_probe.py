# -*- coding: utf-8 -*-
"""重启板子并在 U-Boot 中查询 MMC/SD 槽位状态（不依赖被我改过的 DTB）"""
import time
import paramiko
import serial
import sys

_logf = open("uboot_mmc_probe.log", "w", encoding="utf-8")

class _Tee:
    def __init__(self, f): self.f = f
    def write(self, x):
        sys.__stdout__.write(x); self.f.write(x)
    def flush(self):
        sys.__stdout__.flush(); self.f.flush()

sys.stdout = _Tee(_logf)

PORT, BAUD, PROMPT = "COM3", 115200, "AXERA-UBOOT=>"

# 1) 先打开串口开始持续读取（避免错过启动日志），再触发重启
ser = serial.Serial(PORT, BAUD, timeout=0.3)
ser.reset_input_buffer()
log = bytearray()

def pump(seconds, spam=False):
    """读取 seconds 秒；spam=True 时不断发送空格以拦住自动启动"""
    end = time.time() + seconds
    while time.time() < end:
        if spam:
            ser.write(b" ")
        chunk = ser.read(8192)
        if chunk:
            log.extend(chunk)
        time.sleep(0.05)

def text():
    return log.decode("utf-8", "replace")

print("[1] 触发重启")
try:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("192.168.137.77", username="root", password="123456", timeout=10)
    try:
        c.exec_command("reboot", timeout=5)
    except Exception:
        pass
    c.close()
except Exception as e:
    print("    SSH 重启失败:", e)

print("[2] 等待掉线并拦截 U-Boot（持续发送空格）")
got_prompt = False
end = time.time() + 150
while time.time() < end:
    ser.write(b" ")
    chunk = ser.read(8192)
    if chunk:
        log.extend(chunk)
        t = text()
        if t.rstrip().endswith(PROMPT):
            got_prompt = True
            break
    time.sleep(0.03)

if not got_prompt:
    print("    未抓到提示符，最后输出：")
    print(text()[-3000:])
    ser.close()
    raise SystemExit(1)

print("    已进入 U-Boot 提示符")

def cmd(c, timeout=40):
    ser.reset_input_buffer()
    ser.write((c + "\n").encode())
    buf = bytearray()
    end = time.time() + timeout
    while time.time() < end:
        chunk = ser.read(16384)
        if chunk:
            buf.extend(chunk)
            if buf.decode("utf-8", "replace").rstrip().endswith(PROMPT):
                break
    out = buf.decode("utf-8", "replace")
    print("\n$ %s\n%s" % (c, out))
    return out

print("\n[3] 查询 MMC")
cmd("mmc list")
cmd("mmc dev 1")
cmd("mmc info")
cmd("mmc rescan; mmc info")
cmd("mmc dev 0")
cmd("mmc info")

print("\n[4] 恢复启动")
ser.reset_input_buffer()
ser.write(b"boot\n")
time.sleep(3)
chunk = ser.read(65536)
print(chunk.decode("utf-8", "replace")[-1500:])
ser.close()
print("\n[DONE]")
