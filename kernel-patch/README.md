# Wi-Fi + BLE 依赖的内核补丁

本目录是 `wifi-bt/` 那套驱动/固件**能跑起来**所必需的内核改动。
不打这些补丁，会分别出现以下症状：

| 不打哪个补丁 | 症状 |
|---|---|
| DTS（SDIO）| `ls /sys/bus/sdio/devices/` 永远为空，`insmod` 报绑定失败 |
| config（`CONFIG_BT`）| `insmod` 报 `Unknown symbol hci_alloc_dev` / `hci_register_dev` |
| config（`CONFIG_WIRELESS`/`CFG80211`）| `wifi-role` 起不来，无 `ethsta0`/`ethap0` |

> 如果板上运行的已经是厂商提供的 BT 版内核（`grep -c ' hci_register_dev$'
> /proc/kallsyms` ≥ 2，且 `/sys/bus/sdio/devices/` 有 2 个功能），
> **说明这些补丁已经打过了**，可以直接跳过本目录。

---

## 1. 目录

```text
kernel-patch/
├── ax637/
│   ├── 0001-config-enable-wifi-bt.patch    必需：WIRELESS/CFG80211 + BT
│   └── 0002-dts-enable-sdio.patch          必需：SD 槽改成 SDIO 模式
├── ax650/
│   ├── 0001-config-enable-bt.patch         必需：BT
│   └── 0002-dts-enable-sdio.patch          必需：SD 槽改成 SDIO 模式
└── optional/
    └── 0001-net-bluetooth-hardening.patch  可选：并发注销的加固
```

**必需** = 不打就跑不起来。
**可选** = 不影响功能，只在极端并发场景下更稳，客户可自行决定。

---

## 2. 打补丁

补丁路径以**内核源码根目录**为基准（`a/arch/...`），所以用 `-p1`。

### AX637（linux-6.1.83）

```sh
cd <SDK>/code/kernel/linux/linux-6.1.83

# 先干跑，确认不冲突
patch -p1 --dry-run < .../kernel-patch/ax637/0001-config-enable-wifi-bt.patch
patch -p1 --dry-run < .../kernel-patch/ax637/0002-dts-enable-sdio.patch

# 正式打
patch -p1 < .../kernel-patch/ax637/0001-config-enable-wifi-bt.patch
patch -p1 < .../kernel-patch/ax637/0002-dts-enable-sdio.patch

# 可选加固
patch -p1 < .../kernel-patch/optional/0001-net-bluetooth-hardening.patch
```

### AX650（linux-5.15.73）

```sh
cd <SDK>/code/kernel/linux/linux-5.15.73

patch -p1 < .../kernel-patch/ax650/0001-config-enable-bt.patch
patch -p1 < .../kernel-patch/ax650/0002-dts-enable-sdio.patch
```

`optional/0001-net-bluetooth-hardening.patch` 是 **6.1.83 的**（基于 AX637 基线
生成）。AX650 的 5.15.73 里对应函数结构不同，**不要直接套**；如需同样的加固，
请按 5.15 的代码手工移植。

---

## 3. 各补丁做了什么

### 3.1 `ax637/0001-config-enable-wifi-bt.patch`

在 `arch/arm64/configs/ax637-tiny_defconfig` 里，把

```diff
-# CONFIG_WIRELESS is not set
+CONFIG_WIRELESS=y
+CONFIG_CFG80211=y
+CONFIG_CFG80211_WEXT=y
+CONFIG_BT=y
+CONFIG_BT_BREDR=y
+CONFIG_BT_LE=y
+CONFIG_BT_DEBUGFS=y
```

理由：

- `CONFIG_BT` 关着时，`esp_bt.c` 里的 `hci_alloc_dev()` / `hci_register_dev()`
  找不到符号，modpost 直接拒绝整个模块
- `CONFIG_BT_LE` 覆盖 ESP32-C5（BLE-only）；`CONFIG_BT_BREDR` 留着是为了
  以后换双模模块时不用再改
- `CONFIG_BT_DEBUGFS` 提供 `/sys/kernel/debug/bluetooth/hci0/*`，
  排障时能看 `conn_min_interval`、`features` 等
- `CRYPTO_*` / `CRC16` 由 BT 的 `select` 自动拉入，无需手写

### 3.2 `ax637/0002-dts-enable-sdio.patch`

`ax637-demo-only-nand.dts` 的 `&sd` 节点：

```diff
-       no-sdio;                    ← 来自 m57.dtsi，会让 mmc_rescan() 跳过 SDIO
+       /delete-property/ no-sdio;
+       non-removable;              ← SDIO 适配器不接 card-detect 开关
+       cap-sdio-irq;               ← esp_hosted 要调 sdio_claim_irq()
        no-mmc;
```

为什么三条都要：

- `no-sdio` 会置 `MMC_CAP2_NO_SDIO`，`mmc_attach_sdio()` 根本不会被调用
- 该槽插的是**常驻** SDIO 模块，没有机械检测开关，`cd-gpios` 会一直报"无卡"；
  要改成 `non-removable`（`sdhci_get_cd()` 无条件返回 1）
- `esp_sdio` 用 `sdio_claim_irq()`；没有 `MMC_CAP_SDIO_IRQ` 时核心不会启动
  SDIO 中断线程，DAT1 中断被屏蔽

代价：这个槽插了 SDIO 模块后就不能再插 TF 卡。

### 3.3 `ax650/0001-config-enable-bt.patch`

AX650 出厂 `# CONFIG_BT is not set`，同样需要开 BT/BT_LE/BT_DEBUGFS。

### 3.4 `ax650/0002-dts-enable-sdio.patch`

与 AX637 同理，改的是 `AX650_emmc.dts` 的 `&sd` 节点。

### 3.5 `optional/0001-net-bluetooth-hardening.patch`（可选）

三个防御性修改，**不影响 Wi-Fi/BT 能否工作**：

| 函数 | 作用 |
|---|---|
| `bt_sock_linked()` | 判断 socket 是否还挂在链表上，避免对已 unlink 的 socket 取引用 |
| `hci_conn_valid()` | 校验 `hci_conn` 指针仍有效，避免解引用已释放对象 |
| `l2cap_connect()` | 返回值从 `void` 改为 `struct l2cap_chan *`，便于调用者校验 |

只在"socket/连接被并发注销"这种竞态下才有意义。日常广播/扫描用不到。

---

## 4. 补丁范围说明（重要）

这些补丁是**按功能白名单从完整差异里筛出来的**，只含 Wi-Fi/BT 相关 hunk。
同一个 SDK 里还有别的改动（CPUFreq/OPP 表、USB OTG `dr_mode`、
console 波特率等），它们**不属于本包的交付范围**，已被刻意排除。

验证方式：打完补丁后，与厂商原始 SDK 对比，剩余差异应当只有上面这些无关项。
本次交付的补丁已做过这个验证（见 §5）。

---

## 5. 验证记录（2026-11-09）

以厂商原始 SDK 为基线做 `patch --dry-run`：

| 补丁 | 结果 |
|---|---|
| `ax637/0001-config-enable-wifi-bt.patch` | OK |
| `ax637/0002-dts-enable-sdio.patch` | OK |
| `ax637/optional/0001-net-bluetooth-hardening.patch` | OK |
| `ax650/0001-config-enable-bt.patch` | OK |
| `ax650/0002-dts-enable-sdio.patch` | OK |

应用后再与"已跑通 BAT 的工作区"对比：

- `ax637-demo-only-nand.dts` 的 SDIO 部分**完全一致**，剩余差异只有
  CPUFreq/OPP/USB OTG/console 等无关项
- `ax637-tiny_defconfig` 剩余差异只有 4 行 CPUFreq（同样无关）

即：**补丁覆盖了 Wi-Fi/BT 所需的全部改动，且没有夹带无关修改。**

---

## 6. 打完之后

```sh
# 重新编译内核
cd <SDK>/code/build && ./build.sh ax637_demo_only_nand   # 或对应目标

# 烧录后验证
grep -c ' hci_register_dev$' /proc/kallsyms   # 应 >= 2
ls /sys/bus/sdio/devices/                     # 应有 2 个功能
zcat /proc/config.gz | grep '^CONFIG_BT='     # 应为 y（若内核带 IKCONFIG）
```

两项都对，才能继续 `wifi-bt/README.md` §3 的流程。
