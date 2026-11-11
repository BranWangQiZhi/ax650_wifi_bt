#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 AXERA 分区镜像里的 DTB payload 反编译成可读 DTS 文本"""
import struct, sys, io

path = sys.argv[1] if len(sys.argv) > 1 else "dtb_p6.bak"
raw = open(path, "rb").read()
# 跳过 1024 字节 AXERA 头
pay = raw[1024:]
magic, totalsize, off_struct, off_strings = struct.unpack(">IIII", pay[:16])
off_rsvmap, version, last_comp, boot_cpuid, size_strings, size_struct = struct.unpack(
    ">IIIIII", pay[16:40])
assert magic == 0xD00DFEED, hex(magic)
strings = pay[off_strings:off_strings + size_strings]
sblk = pay[off_struct:off_struct + size_struct]

out = io.StringIO()

def s_at(off):
    e = strings.find(b"\x00", off)
    return strings[off:e].decode("utf-8", "replace")

pos = 0
depth = 0
path = []
while pos < len(sblk):
    tag, = struct.unpack(">I", sblk[pos:pos + 4])
    if tag == 1:  # FDT_BEGIN_NODE
        e = sblk.find(b"\x00", pos + 4)
        name = sblk[pos + 4:e].decode("utf-8", "replace")
        path.append(name)
        if depth:
            out.write("%s%s {\n" % ("\t" * depth, name))
        else:
            out.write("/ {\n")
        pos = (e + 4) & ~3
        depth += 1
    elif tag == 2:  # FDT_END_NODE
        depth -= 1
        path.pop()
        out.write("%s};\n" % ("\t" * depth))
        pos += 4
    elif tag == 3:  # FDT_PROP
        ln, nameoff = struct.unpack(">II", sblk[pos + 4:pos + 12])
        val = sblk[pos + 12:pos + 12 + ln]
        pname = s_at(nameoff)
        pos = (pos + 12 + ln + 3) & ~3
        if ln == 0:
            txt = ""
        elif ln % 4 == 0 and ln <= 32:
            nums = struct.unpack(">%dI" % (ln // 4), val)
            txt = "<" + " ".join("0x%x" % n for n in nums) + ">"
            if all(32 <= b < 127 or b == 0 for b in val):
                s = val.rstrip(b"\x00").decode("ascii", "replace")
                txt += '  /* "%s" */' % s
        elif all(32 <= b < 127 or b == 0 for b in val):
            txt = '"%s"' % val.rstrip(b"\x00").decode("ascii", "replace")
        else:
            txt = "<" + " ".join("%02x" % b for b in val[:16]) + ("..." if ln > 16 else "") + ">"
        out.write('%s%s = %s;\n' % ("\t" * depth, pname, txt))
    elif tag == 4:  # FDT_NOP
        pos += 4
    elif tag == 9:  # FDT_END
        break
    else:
        out.write("/* unknown tag %d at %d */\n" % (tag, pos))
        break
out.write("};\n")

txt = out.getvalue()
open("dtb_dump.dts", "w", encoding="utf-8").write(txt)
print("nodes/props dumped -> dtb_dump.dts  (%d bytes)" % len(txt))
# 打印节点路径清单
for line in txt.splitlines():
    pass
