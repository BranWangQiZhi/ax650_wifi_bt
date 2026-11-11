# -*- coding: utf-8 -*-
"""定位 bt-extadv-tool(aarch64 静态 ELF) 里 adv_data_len 的 `+4` 立即数。

没有交叉编译器时的应急补丁工具：源码里 adv_data_len 应为 5+n，
旧二进制算的是 4+n（少 1 字节），导致广播名最后一个字符被截断、
对端解析 local_name 为空。

用法:
  python patch_extadv.py <elf>            # 只反汇编定位，不改文件
  python patch_extadv.py <elf> --apply    # 写回补丁（生成 .patched）
"""
import struct
import sys

from capstone import CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN, Cs


def shdr(d, i, shoff, entsize):
    o = shoff + i * entsize
    v = struct.unpack_from("<IIQQQQIIQQ", d, o)
    return dict(name=v[0], typ=v[1], flags=v[2], addr=v[3],
                offset=v[4], size=v[5], link=v[6], info=v[7],
                align=v[8], entsize=v[9])


def main():
    path = sys.argv[1]
    apply_patch = "--apply" in sys.argv[2:]
    d = bytearray(open(path, "rb").read())
    assert d[:4] == b"\x7fELF", "not an ELF64 file"

    shoff = struct.unpack_from("<Q", d, 0x28)[0]
    shentsize = struct.unpack_from("<H", d, 0x3A)[0]
    shnum = struct.unpack_from("<H", d, 0x3C)[0]
    shstrndx = struct.unpack_from("<H", d, 0x3E)[0]
    secs = [shdr(d, i, shoff, shentsize) for i in range(shnum)]

    def sname(off):
        s = secs[shstrndx]
        return bytes(d[s["offset"] + off:].split(b"\0")[0]).decode()

    symtab = next((s for s in secs if s["typ"] == 2), None)   # SHT_SYMTAB
    if not symtab:
        print("!! ELF 已 strip，无符号表，无法按名字定位 adv_on")
        return 2
    strtab = secs[symtab["link"]]

    def symname(off):
        return bytes(d[strtab["offset"] + off:].split(b"\0")[0]).decode()

    found = {}
    for o in range(symtab["offset"], symtab["offset"] + symtab["size"], 24):
        st_name, st_info, st_other, st_shndx, st_value, st_size = \
            struct.unpack_from("<IBBHQQ", d, o)
        n = symname(st_name)
        if n in ("adv_on", "main"):
            found[n] = (st_value, st_size)
    if not found:
        print("!! 找不到 adv_on / main 符号")
        return 2
    # -O2 下 adv_on 被内联进 main，此时补丁点落在 main 里
    label = "adv_on" if "adv_on" in found else "main"
    vaddr, size = found[label]
    sec = next(s for s in secs if s["addr"] <= vaddr < s["addr"] + s["size"])
    foff = sec["offset"] + (vaddr - sec["addr"])
    print(f"{label}: vaddr=0x{vaddr:x} size={size} file_off=0x{foff:x} "
          f"section={sname(sec['name'])}")

    md = Cs(CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN)
    md.detail = True
    insns = list(md.disasm(bytes(d[foff:foff + size]), vaddr))
    candidates = []
    for i, ins in enumerate(insns):
        note = ""
        if ins.mnemonic in ("add", "adds") and "#4" in ins.op_str.replace(" ", ""):
            note = "  <== +4 候选"
            candidates.append((i, ins))
        print(f"  {ins.address:x}: {ins.mnemonic:<8} {ins.op_str}{note}")

    # 数据写入 data[3] 的 strb 附近、且是 +4 的那条 add 才是目标
    target = None
    for i, ins in candidates:
        window = insns[max(0, i - 2): i + 4]
        if any(x.mnemonic in ("strb", "str", "strh") for x in window):
            target = ins
            break
    if not target and candidates:
        target = candidates[0][1]

    if not target:
        print("!! 没定位到 +4 指令")
        return 2
    print(f"\n目标指令: 0x{target.address:x} {target.mnemonic} {target.op_str} "
          f"bytes={target.bytes.hex()}")
    raw = struct.unpack("<I", target.bytes)[0]
    # AArch64 ADD (immediate): imm12 在 bit10..21，+1 即把立即数 4 -> 5
    patched = raw + (1 << 10)
    print(f"patch: 0x{raw:08x} -> 0x{patched:08x} （imm12 4 -> 5）")
    if apply_patch:
        off = foff + (target.address - vaddr)
        d[off:off + 4] = struct.pack("<I", patched)
        out = path + ".patched"
        open(out, "wb").write(d)
        print(f"已写出 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
