# -*- coding: utf-8 -*-
"""给 AX650 dtb 分区镜像打 SDIO 补丁:
   - /soc/sdhc@30E00000: 删除 no-sdio, 添加 cap-sdio-irq (与 kernel-patch/ax650/0002 一致)
   - 重算 payload SHA-256 更新头部 0x14, 更新头部 0x10 大小字段
"""
import hashlib
import struct
import fdt

SRC = "dtb_p6.bak"
DST = "dtb_p6.patched"
HDR_PAYLOAD_SIZE_OFF = 0x10
HDR_SHA256_OFF = 0x14
HDR_LEN = 1024

data = open(SRC, "rb").read()
header, payload = bytearray(data[:HDR_LEN]), data[HDR_LEN:]

# --- 解析 DTB ---
dt = fdt.parse_dtb(payload)

# 找目标节点（路径大小写不敏感试探）
node = None
for path in ("/soc/sdhc@30E00000", "/soc/sdhc@30e00000"):
    try:
        node = dt.get_node(path)
        print("found node at", path)
        break
    except Exception:
        pass
assert node is not None, "target node not found"

print("before:", [p.name for p in node.props if p.name in
                  ("no-sdio", "cap-sdio-irq", "no-mmc", "cap-sd-highspeed")])

# 删 no-sdio
node.remove_property("no-sdio")
# 加 cap-sdio-irq（空属性：value=None 序列化为 len=0）
node.set_property("cap-sdio-irq", None)

new_payload = dt.to_dtb()
print("old payload size:", len(payload) if False else struct.unpack('>I', payload[4:8])[0])
new_size = struct.unpack(">I", new_payload[4:8])[0]
print("new DTB totalsize:", new_size)
assert new_payload[:4] == payload[:4] == bytes.fromhex("d00dfeed")

# --- 重建镜像 ---
image = bytearray(HDR_LEN + len(new_payload))
image[:HDR_LEN] = header
image[HDR_LEN:] = new_payload
# 更新大小字段（LE）
struct.pack_into("<I", image, HDR_PAYLOAD_SIZE_OFF, new_size)
# 更新 sha256
sha = hashlib.sha256(new_payload).digest()
image[HDR_SHA256_OFF:HDR_SHA256_OFF + 32] = sha

# 头部其余静态字段校验：magic / AXERA!
assert image[0x08:0x0E] == b"AXERA!", image[0x08:0x0E]
print("magic kept:", image[0:4].hex())
print("new sha256:", sha.hex())

open(DST, "wb").write(bytes(image))
print("written", DST, len(image), "bytes")

# --- 自校验：重新解析 ---
dt2 = fdt.parse_dtb(bytes(image[HDR_LEN:]))
n2 = dt2.get_node("/soc/sdhc@30E00000")
names = [p.name for p in n2.props]
assert "no-sdio" not in names and "cap-sdio-irq" in names
print("self-check OK: no-sdio removed, cap-sdio-irq added")
