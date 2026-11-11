#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AX650 DTB 原位补丁工具 (v5)
--------------------------------
目标：让 sdhc@30E00000 真正获得 SDIO 引脚的 pinctrl 控制，并把占用同一
      组引脚的 spi@0x113000(ICM45686 IMU) 关掉。

约束（AXERA 签名机制）：
  * 头部 1024 字节完全不动（0x1B8..0x2C4 是签名，U-Boot 只校验它）
  * payload 总长度不变（FDT struct block 末尾用 FDT_NOP 填充补齐）
  * strings block 长度不变（只做等长改名）
"""
import os, struct, sys

FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 1, 2, 3, 4, 9


class Node:
    def __init__(self, name):
        self.name = name
        self.props = {}          # name -> [nameoff, bytes]
        self.children = []

    def has(self, n):
        return n in self.props

    def get(self, n):
        v = self.props.get(n)
        return None if v is None else v[1]

    def get_str(self, n):
        v = self.get(n)
        return None if v is None else v.split(b"\x00")[0].decode("utf-8", "replace")

    def put(self, name, val, nameoff=None):
        self.props[name] = [nameoff, val]

    def put_str(self, name, s):
        self.props[name] = [None, s.encode() + b"\x00"]

    def put_u32(self, name, *vals):
        self.props[name] = [None, b"".join(struct.pack(">I", v) for v in vals)]

    def put_empty(self, name):
        self.props[name] = [None, b""]

    def child(self, name):
        for c in self.children:
            if c.name == name:
                return c
        return None


def parse(raw_pay, strings):
    magic, totalsize, off_struct, off_strings = struct.unpack(">IIII", raw_pay[:16])
    assert magic == 0xD00DFEED
    off_rsvmap, version, last_comp, boot_cpuid, size_strings, size_struct = \
        struct.unpack(">IIIIII", raw_pay[16:40])
    return dict(totalsize=totalsize, off_struct=off_struct, off_strings=off_strings,
                size_strings=size_strings, size_struct=size_struct)


def build_tree(sblk, strings):
    pos, root, stack = 0, None, []
    while pos < len(sblk):
        tag, = struct.unpack(">I", sblk[pos:pos + 4])
        if tag == FDT_BEGIN_NODE:
            e = sblk.find(b"\x00", pos + 4)
            n = Node(sblk[pos + 4:e].decode("utf-8", "replace"))
            if stack:
                stack[-1].children.append(n)
            else:
                root = n
            stack.append(n)
            pos = (e + 4) & ~3
        elif tag == FDT_END_NODE:
            stack.pop()
            pos += 4
        elif tag == FDT_PROP:
            ln, nameoff = struct.unpack(">II", sblk[pos + 4:pos + 12])
            val = sblk[pos + 12:pos + 12 + ln]
            nm = strings[nameoff:strings.find(b"\x00", nameoff)].decode("utf-8", "replace")
            stack[-1].props[nm] = [nameoff, val]
            pos = (pos + 12 + ln + 3) & ~3
        elif tag == FDT_NOP:
            pos += 4
        elif tag == FDT_END:
            break
        else:
            raise SystemExit("unknown tag %d @ %d" % (tag, pos))
    return root


def walk(node, path=""):
    p = (path + "/" + node.name).replace("//", "/")
    yield p, node
    for c in node.children:
        yield from walk(c, p)


def name_offsets(strings, name):
    """返回该名字在 strings 中所有合法起始偏移"""
    b = name.encode()
    res, i = [], -1
    while True:
        i = strings.find(b, i + 1)
        if i < 0:
            break
        if (i == 0 or strings[i - 1] == 0) and strings[i + len(b)] == 0:
            res.append(i)
    return res


def serialize(root, strings):
    buf = bytearray()

    def pad4():
        while len(buf) % 4:
            buf.append(0)

    def emit(node):
        buf.extend(struct.pack(">I", FDT_BEGIN_NODE))
        buf.extend(node.name.encode() + b"\x00")
        pad4()
        for pname, (noff, val) in node.props.items():
            if noff is None:
                offs = name_offsets(strings, pname)
                if not offs:
                    raise SystemExit("属性名不在 strings 中: %s" % pname)
                noff = offs[0]
            buf.extend(struct.pack(">III", FDT_PROP, len(val), noff))
            buf.extend(val)
            pad4()
        for c in node.children:
            emit(c)
        buf.extend(struct.pack(">I", FDT_END_NODE))

    emit(root)
    return bytes(buf)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    apply = "--apply" in sys.argv

    raw = open(src, "rb").read()
    header = raw[:1024]
    pay = raw[1024:]
    strings = bytearray(pay[struct.unpack(">I", pay[12:16])[0]:
                             struct.unpack(">I", pay[12:16])[0] +
                             struct.unpack(">I", pay[32:36])[0]])
    info = parse(pay, strings)
    sblk = pay[info["off_struct"]:info["off_struct"] + info["size_struct"]]
    root = build_tree(sblk, bytes(strings))

    print("原 struct block=%d  strings=%d" % (info["size_struct"], len(strings)))

    # ---------- 0. 统计属性名使用次数，找出可安全改名的字符串 ----------
    usage = {}
    for p, n in walk(root):
        for k in n.props:
            usage[k] = usage.get(k, 0) + 1
    print("sd-uhs-ddr50 被 %d 个节点使用" % usage.get("sd-uhs-ddr50", 0))
    offs = name_offsets(bytes(strings), "sd-uhs-ddr50")
    print("sd-uhs-ddr50 在 strings 中偏移 %s（恰好 %d 字节）" %
          (offs, len("sd-uhs-ddr50\x00")))

    # ---------- 1. 等长改名：sd-uhs-ddr50 -> cap-sdio-irq ----------
    if not name_offsets(bytes(strings), "cap-sdio-irq"):
        assert offs, "strings 中找不到 sd-uhs-ddr50"
        o = offs[0]
        assert bytes(strings[o:o + 13]) == b"sd-uhs-ddr50\x00"
        strings[o:o + 13] = b"cap-sdio-irq\x00"
        print("[0] 字符串等长改名 sd-uhs-ddr50 -> cap-sdio-irq @0x%x" % o)

    # ---------- 2. 定位节点 ----------
    sdhc = next(n for p, n in walk(root) if n.name == "sdhc@30E00000")
    spi = next(n for p, n in walk(root) if n.name == "spi@0x113000")

    print("\n[1] spi@0x113000（IMU 走 SPI_M0，与 SD_ 引脚不冲突）")
    spi_mode = os.environ.get("SPI_OFF", "2")
    if spi_mode == "1":
        print("    完全关闭：status -> 'disabled'，移除引脚属性")
        spi.put_str("status", "disabled")
        for k in ("pinctrl-0", "pinctrl-names", "cs-gpios", "num-cs"):
            if k in spi.props:
                del spi.props[k]
                print("    移除 %s" % k)
        for ch in list(spi.children):
            spi.children.remove(ch)
            print("    移除子节点 %s" % ch.name)
    elif spi_mode == "2":
        # 保留 SPI 控制器与 pinctrl（引脚仍是 SPI_M0，占 SDIO_ 焊盘，不碰 SD_ 焊盘）
        # 只摘掉 IMU 子设备，腾出 struct 空间；IMU 驱动因此不再 probe
        for ch in list(spi.children):
            spi.children.remove(ch)
            print("    仅移除子设备 %s（腾 %d 字节；SPI 控制器与引脚保留）"
                  % (ch.name, 116))
    else:
        print("    保持原样（不动 IMU）")

    print("\n[2] sdhc@30E00000 整备")
    # 纯 SD 卡专用标志，SDIO 槽位不需要，删掉腾出 struct 空间
    for k in ("no-sdio", "sd-uhs-ddr50", "sd-uhs-sdr12", "sd-uhs-sdr25",
              "sd-uhs-sdr50", "sd-uhs-sdr104", "cap-sd-highspeed",
              "disable-wp"):
        if k in sdhc.props:
            del sdhc.props[k]
            print("    删除 %s" % k)
    if "no-sd" not in sdhc.props:
        sdhc.put_empty("no-sd")
        print("    新增 no-sd（SDIO-only）")
    if "no-mmc" not in sdhc.props:
        sdhc.put_empty("no-mmc")
    if "cap-sdio-irq" not in sdhc.props:
        sdhc.put_empty("cap-sdio-irq")
        print("    新增 cap-sdio-irq")
    mf = int(os.environ.get("MAXF", "0"))
    if mf:
        sdhc.put_u32("max-frequency", mf)
        print("    max-frequency = %d" % mf)

    # ---------- 回收 SPI 专用字符串空间，腾出两个新属性名 ----------
    acr = int(os.environ.get("CARDCLK", "0"))
    if acr:
        blob = b"rx-sample-delay-ns\x00num-cs\x00spi-max-frequency\x00"
        o = strings.find(blob)
        assert o >= 0, "找不到可回收的 SPI 字符串块"
        newblob = b"assigned-clock-rates\x00assigned-clocks\x00"
        assert len(newblob) <= len(blob)
        strings[o:o + len(blob)] = newblob + b"\x00" * (len(blob) - len(newblob))
        print("\n[3b] 回收 SPI 字符串 44B @0x%x -> assigned-clock-rates/assigned-clocks" % o)
        # 三个属性名已失效，从所有节点删除对应属性
        for pth, n in walk(root):
            for k in ("rx-sample-delay-ns", "num-cs", "spi-max-frequency"):
                if k in n.props:
                    del n.props[k]


    if acr:
        # clocks = <0x19 0x1e 0x19 0x13 0x19 0x23> → cardclk = (0x19, 0x23)
        sdhc.put_u32("assigned-clocks", 0x19, 0x23)
        sdhc.put_u32("assigned-clock-rates", acr)
        print("    assigned-clock-rates(cardclk) = %d" % acr)

    sdhc.put_str("pinctrl-names", "default")

    # ---------- 3. 给 6 个引脚组补 phandle ----------
    # PINS=sd   -> SD_CLK/CMD/DAT0-3（SD 卡槽，C5 转接卡走这组）
    # PINS=sdio -> SDIO_CLK/CMD/DAT0-3（纯 SDIO 焊盘）
    pfx = os.environ.get("PINS", "sd")
    groups = [pfx + "_dat0", pfx + "_dat1", pfx + "_dat2", pfx + "_dat3",
              pfx + "_cmd", pfx + "_clk"]
    if os.environ.get("PWREN", "0") == "1":
        groups.append("sd_pwr_en")
    maxph = 0
    for p, n in walk(root):
        v = n.get("phandle")
        if v and len(v) == 4:
            maxph = max(maxph, struct.unpack(">I", v)[0])

    ph = []
    print("\n[3] sdio 引脚组 phandle（基准最大 0x%x）" % maxph)
    for g in groups:
        gn = next(n for p, n in walk(root) if n.name == g)
        mux = gn.child("mux")
        pins = mux.get("pins").rstrip(b"\x00").decode() if mux else "?"
        func = mux.get_str("function") if mux else "?"
        cfg = gn.child("configs")
        pu = cfg.has("bias-pull-up") if cfg else False
        ds = struct.unpack(">I", cfg.get("drive-strength"))[0] if cfg and cfg.has("drive-strength") else "-"
        old = gn.get("phandle")
        if old and len(old) == 4:
            val = struct.unpack(">I", old)[0]
        else:
            maxph += 1
            val = maxph
            gn.put_u32("phandle", val)
        ph.append(val)
        print("    %-10s pins=%-11s func=%-11s pull-up=%-5s drv=%-3s ph=0x%x"
              % (g, pins, func, pu, ds, val))
    sdhc.put_u32("pinctrl-0", *ph)
    print("    pinctrl-0 = %s" % " ".join("0x%x" % x for x in ph))

    # ---------- 4. 序列化 ----------
    new_struct = serialize(root, bytes(strings))
    limit = info["size_struct"] - 4
    print("\n[4] 新 struct = %d / 上限 %d（省下 %d 字节）" %
          (len(new_struct), limit, limit - len(new_struct)))
    if len(new_struct) > limit:
        raise SystemExit("!! 超出 %d 字节" % (len(new_struct) - limit))

    out = bytearray(new_struct)
    while len(out) < limit:
        out.extend(struct.pack(">I", FDT_NOP))
    out.extend(struct.pack(">I", FDT_END))
    assert len(out) == info["size_struct"]

    if not apply:
        print("\n(dry-run；加 --apply 生成镜像)")
        return

    newpay = bytearray(pay)
    s, so = info["off_struct"], info["off_strings"]
    newpay[s:s + info["size_struct"]] = bytes(out)
    newpay[so:so + len(strings)] = bytes(strings)
    assert len(newpay) == len(pay)

    open(dst, "wb").write(header + bytes(newpay))
    print("\n已写出 %s（%d 字节 = 原始大小 %s）" %
          (dst, len(raw), len(raw) == len(raw)))


if __name__ == "__main__":
    main()
