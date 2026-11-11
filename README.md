# WiFi + BT 支持目录（AX650 / AX637）

ESP32-C5 通过 SDIO 同时提供 **Wi-Fi（802.3 / RPC 路线）** 和 **BLE**。本目录是
自包含的支撑包：驱动、CP 固件、用户态工具、启动脚本一次给全。

> **要不要重编内核？**
> 取决于板子现有内核有没有开 `CONFIG_BT` / `CONFIG_WIRELESS`：
> - **已经开了**（`grep -c ' hci_register_dev$' /proc/kallsyms` ≥ 2）→
>   直接装驱动和固件即可，**不需要重编内核**
> - **没开**（AX637 / AX650 出厂镜像都属于这种）→ 需要打
>   `kernel-patch/` 下的补丁后重编，见 §2.1
>
> 本包**不包含内核镜像**，只提供补丁与判定方法。

> 本目录可以独立于 TSF 部分单独使用。包根的 `README.md` 说明了两部分如何配合。

---

## 1. 目录内容

```text
wifi-bt/
├── README.md                   本文件
├── driver/
│   ├── esp32_sdio-ax650-5.15.73-bt.ko      AX650 用（内核 5.15.73）
│   └── esp32_sdio-ax637-6.1.83-rt28-bt.ko  AX637 用（内核 6.1.83 PREEMPT_RT）
├── firmware/
│   ├── app.bin                 CP 固件（Wi-Fi + BT 双功能）
│   ├── bootloader.bin
│   ├── partition-table.bin     1.75 MB 双 OTA 布局
│   ├── ota_data_initial.bin
│   └── flash.sh                烧录脚本（四个分区，顺序正确）
├── app/
│   ├── bt-mgmt-tool            MGMT 工具：info / power / scan
│   ├── bt-extadv-tool          **BLE 广播工具（推荐）**
│   ├── bt-hci-tool             raw HCI 工具：info / reset / scan
│   ├── wifi-role               Wi-Fi 角色程序（AP/STA），无源码
│   └── bt-*.c                  三个 BT 工具的源码（wifi-role 不在其列）
├── scripts/
│   ├── common.sh               共享函数（板型识别、wifi.conf 解析）；
│   │                           **wifi-ap.sh / wifi-sta.sh 的硬依赖，不能漏拷**
│   ├── bt.sh                   BLE 管理：up / down / status / adv / scan
│   ├── wifi-ap.sh              SoftAP 启动
│   └── wifi-sta.sh             STA 启动
├── kernel-patch/               ★ 本目录能跑起来所必需/可选的内核改动
│   ├── README.md               每个补丁干什么、怎么打、验证记录
│   ├── ax637/
│   │   ├── 0001-config-enable-wifi-bt.patch  WIRELESS/CFG80211 + BT/BT_LE
│   │   └── 0002-dts-enable-sdio.patch        SD 槽改成 SDIO 模式
│   ├── ax650/
│   │   ├── 0001-config-enable-bt.patch
│   │   └── 0002-dts-enable-sdio.patch
│   └── optional/
│       └── 0001-net-bluetooth-hardening.patch  并发注销加固（非必需）
└── config/
    └── wifi.conf               Wi-Fi 默认配置
```

两个驱动都**同时带 Wi-Fi 和 BT**，用哪一个取决于板子的内核版本：

| 板子 | 内核 | 用哪个 ko |
|---|---|---|
| AX650 | 5.15.73 | `esp32_sdio-ax650-5.15.73-bt.ko` |
| AX637 | 6.1.83-rt28 | `esp32_sdio-ax637-6.1.83-rt28-bt.ko` |

> 如果板上跑的已经是带 BT 的内核（`grep -c ' hci_register_dev$' /proc/kallsyms`
> 返回 ≥ 2，且 `/sys/bus/sdio/devices/` 有 2 个功能），就不需要 `kernel-patch/`。
> 具体依赖关系见 §2.1。

---

## 2. 前置条件

### 2.1 内核必须开 `CONFIG_BT`（AX637 出厂没开）

AX637 出厂镜像 `# CONFIG_BT is not set`，必须换成带 BT 的内核；AX650 出厂
`# CONFIG_BT is not set` 同理。两种途径：

1. **用厂商提供的 BT 版内核**（推荐，本包不含内核镜像）
2. **自己给 SDK 打补丁** —— 见 `kernel-patch/README.md`：
   - `kernel-patch/ax637/`：config（`WIRELESS`/`CFG80211` + `BT`/`BT_LE`）
     + DTS（SD 槽改成 SDIO 模式）
   - `kernel-patch/ax650/`：同上两项

验证方法：

```sh
# 内核带 BT 时该符号存在
grep -c ' hci_register_dev$' /proc/kallsyms    # 应 >= 2

# 内核开了 CFG80211 时该目录存在
ls /sys/kernel/debug/ieee80211 2>/dev/null >/dev/null && echo cfg80211 ok
```

返回 0 说明内核没有 BT，驱动加载后会报找不到 `hci_alloc_dev` /
`hci_register_dev` 符号，modpost 直接拒绝整个模块。

### 2.2 C5 必须烧 BT 版固件

出厂固件可能只有 Wi-Fi。用 `firmware/flash.sh` 烧四个分区（顺序不能错）：

```sh
# Windows：COM97 / Linux：/dev/ttyUSB0
./flash.sh COM97
```

烧完必须**重新枚举 SDIO**（见 §3.1），否则驱动 probe 会超时。

验证固件带 BT：加载驱动后 `dmesg` 里应该有

```
esp32_sdio: process_capabilities: ESP peripheral capabilities: 0xd
```

`0x1` 表示只有 Wi-Fi（固件不带 BT），`0xd` 表示 Wi-Fi + BT + HCI over SDIO。

---

## 3. 快速开始

### 3.1 上电后先确认 SDIO 枚举

**这一步最容易漏**。C5 和 SoC 同时上电时，sdhci 先 probe 完但 C5 还没准备好，
`mmc_rescan()` 这一轮就漏掉了，表现为 `/sys/bus/sdio/devices/` 为空、
`insmod` 失败，看起来像驱动坏了。

```sh
# 正常应该有 2 个功能
ls /sys/bus/sdio/devices/
# mmc1:0001:1  mmc1:0001:2      ← 第二个功能是 BT，只有 BT 固件才有

# 如果为空，重新 bind 一次
# AX637
echo c010000.sdhci > /sys/bus/platform/drivers/sdhci-axera/unbind
sleep 2
echo c010000.sdhci > /sys/bus/platform/drivers/sdhci-axera/bind
sleep 4

# AX650：热解绑有 panic 风险，建议直接重启
```

`bt.sh up` 会自动做这一步（仅当检测到 SDIO 为空时）。

### 3.2 部署并上电 BT

**推荐：用包根的 `install.sh`**（自动选目录、自动带上 `common.sh`）：

```sh
cd <解包后的包根>
./install.sh --wifi-bt-only     # AX637 → /opt/data/wlan，AX650 → /soc/sdio_wifi
```

手工部署时要拷**四类**文件，漏任何一类都会在启动时失败：

```sh
W=/opt/data/wlan                 # AX650 上是 /soc/sdio_wifi

# 1) 驱动（按内核版本选 ko）
cp driver/esp32_sdio-$(uname -r | sed 's/+//')-bt.ko $W/esp32_sdio.ko

# 2) 用户态程序
cp app/wifi-role app/bt-mgmt-tool app/bt-extadv-tool app/bt-hci-tool $W/

# 3) 脚本 —— common.sh 必须一起拷，wifi-ap.sh/wifi-sta.sh 第一行就 source 它
cp scripts/common.sh scripts/bt.sh scripts/wifi-ap.sh scripts/wifi-sta.sh $W/

# 4) 配置（已存在则不要覆盖，可能填了现场 SSID）
[ -f $W/wifi.conf ] || cp config/wifi.conf $W/

chmod 755 $W/wifi-role $W/bt-* $W/*.sh
```

然后上电 BT：

```sh
cd $W && ./bt.sh up
```

正常输出：

```
[bt] 加载 /opt/data/wlan/esp32_sdio.ko
SET_POWERED(1): OK (status=0x00)
[bt] BT 就绪（hci0 已注册，LE 已开）
== controller ==
  index   : 0
  BD_ADDR : 14:C1:9F:86:FC:4E
  version : HCI 14 / mfr 0x02E5
```

> `bt.sh up` 会自动处理 SDIO 未枚举的情况（§3.1），并顺带打开
> `enable_ecred`（`/sys/module/bluetooth/parameters/enable_ecred`）。

### 3.3 扫描周边 BLE 设备

```sh
sh bt.sh scan 10
```

```
START_DISCOVERY(LE): OK (status=0x00)
扫描 10 秒...
  4C:E9:5E:D6:9F:E1  rssi=-72 dBm  type=2  (no name)
  AC:2D:FA:D7:50:EA  rssi=-61 dBm  type=1  (no name)
```

> 扫描的 `type` 参数是 **bdaddr 位掩码**，LE 必须传 `0x06`
> （`BIT(1)|BIT(2)`），传 `0x01` 会被内核当成经典蓝牙发现而报 `0x0c NOT_SUPPORTED`，
> 传 `0x02` 会报 `0x0d INVALID_PARAMS`。工具里已经处理好了。

### 3.4 开 BLE 广播

```sh
sh bt.sh adv AX637-BLE 30    # 播 30 秒
sh bt.sh adv AX637-BLE       # 一直播，Ctrl-C 结束
sh bt.sh adv off             # 停
```

> **C5 只接受 extended advertising 命令**（CP 固件开了 `CONFIG_BT_LE_EXT_ADV=y`），
> legacy 的 `0x2006/0x2008/0x200a` 它一直不应答。所以广播必须用
> `bt-extadv-tool`（内部发 `0x2036/0x2037/0x2039`），不要用 `bt-hci-tool adv`。

### 3.5 Wi-Fi 说明：走的是 802.3/RPC，不是 cfg80211

**先建立正确的预期**，否则会找不到 `wlan0` 而以为驱动没起来：

| 项 | 本方案 | 常见 Wi-Fi 方案 |
|---|---|---|
| 数据通路 | **RPC / 802.3**（esp-hosted）| cfg80211 + nl80211 |
| 网口形态 | `ethap0`（AP）、`ethsta0`（STA），**标准以太网口** | `wlan0` 无线接口 |
| 配置工具 | `wifi-role`（随包）+ `wifi.conf` | `iw` / `hostapd` / `wpa_supplicant` |
| 内核要求 | `CONFIG_WIRELESS` + `CONFIG_CFG80211` | 同 |
| `iw dev` 能否看到 | ❌ 看不到（不是无线接口）| ✅ |

也就是说：**C5 在系统里表现为一块"以太网卡"**，只是这块卡后面是 Wi-Fi。
所以 `ip addr` / `udhcpc` / `iperf3` 都能直接用，但 `iw` / `hostapd` 用不上。

> 内核里 `CONFIG_CFG80211=y` 仍然是必须的——`wifi-role` 内部要调 cfg80211
> 做扫描与关联，只是**数据面**不走它。

### 3.6 启动 Wi-Fi（AX650 做 AP）

```sh
cd /soc/sdio_wifi                # AX637 上是 /opt/data/wlan
./wifi-ap.sh                     # 默认 AP 模式
```

正常输出：

```
wifi-ap: mode=ap SSID=AX650-SDIO-AP channel=36 if=ethap0
wifi-ap: starting wifi-role (EH_MODE=ap)
I (395) wifi apsta: ap up. AP SSID:AX650-SDIO-AP ch:36
wifi-ap: ethap0=192.168.4.1 (DHCP pool 192.168.4.10-192.168.4.200)
wifi-ap: ready. Followers use SSID=AX650-SDIO-AP / password=ax650sdio
```

要点：

- `ethap0` 固定 `192.168.4.1/24`，DHCP 池 `.10–.200`
- 默认 **5 GHz ch36**（本版本只在 5 GHz 验证过）
- AX650 还可以同时做 STA（`UPLINK_ENABLE=1`，`EH_MODE=apsta`）连外部网络，
  但**一颗射频时分复用**，外部 AP 必须与 `WIFI_CHANNEL` 同信道；
  外部网络不存在时务必置 `UPLINK_ENABLE=0`，否则 STA 反复扫描会抢占 AP

### 3.7 启动 Wi-Fi（AX637 做 STA）

```sh
cd /opt/data/wlan
./wifi-sta.sh
```

正常输出：

```
wifi-sta: SSID=AX650-SDIO-AP channel=36 if=ethsta0
wifi-sta: starting wifi-role (EH_MODE=sta EH_EXTERNAL_IP=1)
wifi-sta: associated
wifi-sta: DHCP on ethsta0
wifi-sta: ready  if=ethsta0 ip=192.168.4.10 device=ax637-6fc4c
wifi-sta: 192.168.4.1 reachable
```

验证连通：

```sh
ip -4 addr show ethsta0          # 应看到 192.168.4.10
ping -c 3 192.168.4.1            # 0% 丢包
```

### 3.8 配置：`wifi.conf`

两个脚本都直接 `source` 同目录（或 `$TSF_ROOT`）下的 `wifi.conf`：

```sh
WIFI_SSID=AX650-SDIO-AP     # AP 名
WIFI_PASS=ax650sdio         # >=8 字节为 WPA2-PSK，空串为开放网络
WIFI_CHANNEL=36             # >=36 走 5 GHz
UPLINK_ENABLE=1             # AX650 是否同时做 STA
UPLINK_SSID=chl
UPLINK_PASS=1234567890
```

**两块板的 `WIFI_*` 必须完全一致**，否则关联失败（典型症状：`reason=201`
表示"找不到 AP"，多半是 SSID 大小写、信道或频段不一致）。

### 3.9 Wi-Fi 与 BLE 同时使用

两颗功能共用同一颗 C5 和同一个 SDIO 通道，**可以同时工作**，实测无冲突：

```sh
# 都在 /soc/sdio_wifi（AX637 为 /opt/data/wlan）
./bt.sh up                       # 先把 BT 起来
./wifi-ap.sh                     # 或 ./wifi-sta.sh
./bt.sh adv MY-DEVICE &          # BLE 广播常驻
./bt.sh scan 10                  # 期间 Wi-Fi 正常收发
```

实测 `no-credit`（SDIO 发送信用耗尽）计数为 **0**，`ethap0`/`ethsta0` 与
`hci0` 全程共存。会互相影响的只有**射频空口时间**（单射频时分），
表现为 BT 扫描/广播期间 Wi-Fi 吞吐略降，见 §4.2。

---

## 4. 实测结果

环境：AX650（5.15.73）+ AX637（6.1.83-rt28），两块 C5 各烧 BT 固件。

### 4.1 功能验证

| 项目 | 结果 |
|---|---|
| CP 上报能力 | `capabilities: 0xd`（Wi-Fi + BT + HCI over SDIO）|
| `hci0` 注册 | 两板均正常 |
| HCI 初始化 | 20 条命令 / 20 个事件，零超时 |
| BD_ADDR | AX650 `14:C1:9F:8B:A5:CA`，AX637 `14:C1:9F:86:FC:4E` |
| HCI 版本 | HCI 5.4，厂商 `0x02E5`（Espressif），**仅 BLE**（无经典蓝牙）|
| BLE 扫描 | 两板各自单轮 12–16 台设备，含 RSSI 与地址类型 |
| BLE 广播 | 对端实测收到，RSSI −28 ~ −30 dBm |
| **双向互通** | AX637 收到 AX650；AX650 收到 AX637 ✅ |
| **Wi-Fi AP/STA** | AX650 AP `192.168.4.1`，AX637 STA `192.168.4.10`，ping 0% 丢包 |
| **BT + Wi-Fi 共存** | `ethsta0` / `ethap0` 与 `hci0` 同时工作，0 次 no-credit |

互通原始记录：

```text
# AX650 播（ext adv），AX637 扫
14:C1:9F:8B:A5:CA  rssi=-30 dBm  type=1

# AX637 播，AX650 扫
14:C1:9F:86:FC:4E  rssi=-28 dBm  type=1
```

两块板的 BT 地址与各自 C5 的 Wi-Fi MAC 同 base（`…A5:C8` → `…A5:CA`、
`…FC:4C` → `…FC:4E`）。

### 4.2 Wi-Fi / BLE 性能

> **测量口径很重要，先说清楚。**
> 只有 iperf3 客户端与服务端**都在 Wi-Fi 链路上**时，测到的才是 Wi-Fi 吞吐。
> AX637 到外部服务器（如 10.30.6.150）走的是有线 `eth0` 默认路由，
> 那种测法得到的是**千兆网口**的数字（约 942 Mbps），与 Wi-Fi 无关。
>
> 本节所有数字都用纯 Wi-Fi 链路：AX637 的 C5 STA（`ethsta0`）→ 空口 →
> AX650 的 C5 AP（`ethap0`），iperf3 服务端绑在 `192.168.4.1`。

测试条件：

| 项 | 值 |
|---|---|
| 链路 | C5 STA ↔ C5 AP，5 GHz ch36 / 40 MHz |
| SDIO | 50 MHz × 4 bit（理论 200 Mbps，half-duplex）|
| 工具 | iperf3 3.16，`-O 1` 跳过慢启动，每次 10–12 s |
| RTT | 1.7–2.1 ms（对比有线 0.28 ms，可见确实在空口）|

| 场景 | 上行 | 下行 |
|---|---|---|
| **A. Wi-Fi 单独**（BLE `powered=0`）| **43.9 Mbps** | **43.0 Mbps** |
| **B. Wi-Fi + BLE 广播**（对端常驻广播）| 38.5 Mbps | 40.1 Mbps |
| **C. Wi-Fi + BLE 广播 + 扫描**（本端同时扫）| 25.4 Mbps | **1.9 Mbps** |

BLE 并发的影响：

| 对比 | 上行 | 下行 |
|---|---|---|
| B vs A（仅广播）| −12.3% | −6.7% |
| **C vs A（广播 + 扫描）** | **−42.1%** | **−95.6%** |

**44 Mbps 这个上限是怎么来的（2026-11-10 复测）：**

| 观察 | 数值 | 说明 |
|---|---|---|
| TCP 单流 / 4 并行流 | 40.1 / 40.4 Mbps | 加流不涨 → 不是 TCP 窗口或拥塞限制 |
| UDP 不限速 `-b 0` | 45.1 Mbps，**0% 丢包** | 发送端被限速而非丢包 → 流控 |
| 主机 CPU | 13.8%（STA）/ 5.0%（AP）| 主机不忙 |
| SDIO 占用 | 45 ÷ 200 ≈ **22%** | SDIO 带宽远未饱和 |
| CP 侧报错 | `BTDBG credit: no H2E buffer (available=0 needed=1 drop#N)` | **C5 接收缓冲耗尽** |

即：主机把数据推给 C5 时，C5 的 H2E 接收缓冲已被占满，驱动只能在 credit 上
空转等待。空口和 SDIO 都还有余量，所以**调高 SDIO 时钟不会有效果**；
要突破这个数得动 C5 侧的 ESP-Hosted CP 实现或加大 H2E 缓冲。

**结论：**

1. **Wi-Fi 单向约 44 Mbps，瓶颈在 CP（C5）侧流控，不在 SDIO 带宽。**
2. **BLE 广播对 Wi-Fi 影响很小**（−7% ~ −12%），因为广播由 C5 控制器自己发，
   主机侧不参与。
3. **BLE 扫描会把 Wi-Fi 下行打到几乎为 0（−95.6%）**。这是**单射频时分**的必然
   结果：扫描要把射频切到接收窗口守 3 个广播信道，这段时间不能收 Wi-Fi 数据。
   **实际部署中不要让 BLE 扫描与 Wi-Fi 大流量并发**；需要持续扫描时，
   把 Wi-Fi 流量安排在扫描之外，或接受吞吐下降。
4. UDP 已能测到稳定数据（见上表第 1 条），但**持续大流量会拖垮链路**：连续跑
   iperf3 超过约 1–2 分钟后，ARP 变为 INCOMPLETE/FAILED，接着 C5 的 RPC 停止
   响应（`eh_host_feat_rpc: request: no response`），必须串口硬复位（§5.8）
   才能恢复。实测复现 3 次。**验收/压测请用短时（≤10 s）多轮，不要长连接打满**，
   见 §5.11。


---

## 5. 已知限制

### 5.1 只能做 BLE，没有经典蓝牙

C5 是 BLE-only 控制器。内核虽然开了 `CONFIG_BT_BREDR=y`，控制器不上报 BR/EDR，
`supported_settings` 里没有对应位。**不要按"能连蓝牙耳机"来预期。**

### 5.2 自定义 PSM 的 L2CAP CoC 数据通道不可用

想用 `AF_BLUETOOTH + BTPROTO_L2CAP` 建一条 BLE 数据通道时，`connect()` 会立即
返回 `ECONNREFUSED`，而且**一条 HCI 命令都没发出去**。

根因在内核（`net/bluetooth/l2cap_sock.c`）：

```c
if (bdaddr_type_is_le(la.l2_bdaddr_type)) {
        /* We only allow ATT user space socket */
        if (la.l2_cid && la.l2_cid != cpu_to_le16(L2CAP_CID_ATT))
                return -EINVAL;
}
```

5.15 / 6.1 的 L2CAP socket 对 LE **只开放 ATT 的 CID**，用户态没法注册任意 PSM
的服务端。虽然 `enable_ecred`（`BT_MODE_LE_FLOWCTL`）参数存在，但它服务于内核
内部（GATT），不构成用户态 CoC 通道。

**要用 BLE 传业务数据，有两条路：**

1. **GATT 特征值转发** —— 起 `bluetoothd`（BlueZ），用 GATT 服务承载数据。
   需要 rootfs 里有 BlueZ，且 BlueZ 会接管 hci0（与 MGMT 工具互斥）。
2. **raw HCI 自己实现 ACL/L2CAP** —— 绕开内核协议栈，直接收发 ACL 包。
   `bt-hci-tool` 的 HCI socket 框架可以复用，但要自己实现 L2CAP 信令。

本包提供的是**广播 + 扫描**这套外设能力，足够覆盖"设备可被发现、可上报数据"
这类场景（例如把数据放进广播报文或扫描响应里）。

### 5.3 MGMT 的广播命令空口看不到

`bt-mgmt-tool adv on`（走内核 MGMT `ADD_ADVERTISING`）会返回 `SUCCESS`，
但实测**对端扫不到**。原因是内核的下发顺序是
`0x2036 → 0x2038 → 0x2039(Enable) → 0x2037(Set Data)`——先使能后设数据，
而且用 `instance=1` 当广播句柄，而 C5 的 `MAX_EXT_ADV_INSTANCES=1` 只有句柄 0。

**所以广播统一用 `bt-extadv-tool`**（句柄 0，顺序 2036→2037→2039），`bt.sh adv`
默认就是它。

### 5.4 raw HCI 收不到事件（属正常现象）

RPC/802.3 路由下，raw socket 发出的命令**能进 `hdev->send`**，控制器应答
**也回到了驱动**，但 `read()` 拿不到（`hci_send_to_sock()` 的 promisc 分发在此
路由下不生效）。

**判读规则**：工具报"等事件超时"**不代表命令失败**，要以内核日志为准：

```sh
dmesg | grep 'BTDBG bt_tx'    # 发了什么
dmesg | grep 'BTDBG bt_rx'    # 控制器应答及 status
```

`bt-extadv-tool` 已按此设计：读不到事件不算失败。**注意**：发布包的正式 ko
去掉了 BTDBG 埋点，需要抓包时请用 `debug/` 下的诊断版 ko。

### 5.5 广播进程不要频繁重拉

AX650 上反复 `pkill` + 重拉 raw ext adv 曾触发一次内核 panic（次级 CPU 卡在
中断循环）。建议广播进程常驻，需要换广播内容时用 `adv off` → `adv on`，
而不是杀掉重启。

### 5.6 双板 BT 地址不能相同

两块 C5 的 BT 地址由各自的 Wi-Fi MAC 派生，只要 MAC 不同就不会冲突。
若客户自己改 MAC，注意 BT 地址会跟着变。

### 5.7 短时间连续扫描会漏掉广播

在 AX637 上接连执行多次 `scan`（间隔十几秒以内）时，`START_DISCOVERY` 仍返回
`status=0x00`，但**部分广播会收不到**。实测：同一对板子，每轮只扫一次时
25 轮里 96% 能看到对端广播；同一轮连续扫两次时看到率降到 0。

原因是 C5 连续启动扫描时扫描状态会叠加，控制器侧没有完全重置。
**建议**：

- 两次 `scan` 之间留 3 秒以上冷却
- 需要长时间持续监听时，用一次较长的 `scan <秒数>`（如 `scan 60`），
  不要用循环调用短扫描
- 扫不到某台设备时，先排除这条，再怀疑对端

> 这条也解释了「为什么有时能扫到、有时扫不到」的间歇性现象 —— 大多是扫描
> 调用过于密集，不是硬件或驱动故障。

### 5.8 【重要】杀掉 `wifi-role` 会把 C5 卡死，只能靠串口硬复位恢复

**这是本版本最容易踩、后果最严重的坑，务必先读。**

只要 `wifi-role` 进程在持有 RPC 会话时被 `kill` / `kill -9` / `SIGTERM` 结束，
C5 就可能停在"SDIO 硬件还在、固件不再响应"的状态。表现：

```
# 驱动 probe 反复失败
Ignoring stale probe-time RX len=608708 (> cap 15872); resyncing baseline
esp32_sdio: probe of mmc1:0001:2 failed with error -22

# 没有 /dev/esps0，或者节点在但 open 报 ENXIO
eh_vserial_linux: open(/dev/esps0) failed: No such device or address
eh_host_xport: TRANSPORT_FAILURE: aborting host process
wifi-sta: ERROR: the C5 is not responding: reset it and re-enumerate SDIO
```

**此时以下方法全部无效**（都试过）：

| 尝试 | 结果 |
|---|---|
| `reboot -f`（整板软重启）| ❌ C5 不掉电，状态保持，`Rx len` 值一模一样 |
| SD 电源 GPIO（`SD_PWR_EN` / `SD_PWR_SW`，全局 165/166）| ❌ 不控制 C5 供电 |
| `rmmod` + `sdhci` unbind/bind + `insmod` | ❌ probe 仍读到陈旧长度 |
| 等 C5 自己恢复 | ❌ 不会 |

**唯一可靠的方法：用 esptool 通过 C5 的串口拉 RTS 硬复位。**

```sh
# AX637 的 C5 在 COM99，AX650 的在 COM97
# 方式一：直接跑包里的烧录脚本（幂等，刷同样的 otadata 即可触发复位）
./wifi-bt/firmware/flash.sh COM99 --app     # 或完整四分区

# 方式二：串口服务器 API（本环境实际用的）
curl -X POST -H "Authorization: Bearer <token>" \
     --data-binary @wifi-bt/firmware/ota_data_initial.bin \
     "http://10.126.41.50:8770/api/esp/flash?port=COM99&offset=0xd000&force=1"
# 日志里出现 "Hard resetting via RTS pin..." 即复位成功
```

复位后按 §3.1 → §3.2 重新枚举并加载驱动，`/dev/esps0` 与 `hci0` 会回来。

**如何避免：**

- **不要**为了"重来一次"去杀 `wifi-role`。要切换 AP/STA 模式时，
  用 `wifi-ap.sh` / `wifi-sta.sh` 自带的幂等逻辑（它们会检测已在运行）。
- 确实要重启 Wi-Fi 时，**先 `rmmod esp32_sdio`**（这会干净地拆掉 transport），
  再重启 `wifi-role`，而不是直接 kill。
- 需要 BT 与 Wi-Fi 同时用时，先起 Wi-Fi 再起 BT，两者互不影响；
  BT 侧的 `bt.sh` 全程不碰 `wifi-role`。

### 5.9 反复重启 Wi-Fi 后可能出现「关联成功但 ARP 失败」

如果多次 kill/重启 `wifi-role`，即使最后关联成功（AP 日志有
`AP: station ... join, AID=1`、STA 也拿到 IP），仍可能出现：

```
ip neigh show 192.168.4.1     → INCOMPLETE
ip neigh show 192.168.4.237   → FAILED
ping 192.168.4.1              → 100% packet loss
```

即 **L2 数据面没通**。清 ARP 表无效。处理办法：

1. 按 §5.8 硬复位两颗 C5
2. `reboot -f` 重启板子（清掉驱动/网络栈残留状态）
3. 重启后重新枚举 SDIO，再起 AP/STA（**不要** rmmod，让 `wifi-sta.sh` 自己加载）

按此顺序恢复后实测 ping 4/4、RTT 1.7–2.1 ms、TSF 立即同步。

---

### 5.10 【重要】串口控制台的逐包 `printk` 会拖慢 Wi-Fi

驱动编译时开了 `CONFIG_INFO_LOGS=y`（见驱动 `Makefile`），`esp_info` 映射为
`pr_info`，所以 `esp_sdio.c` 的 `write_packet` 等路径**每个包打一条**：

```
esp32_sdio: write_packet: H2E enqueue: prio=1 if=4 num=0 len=3 off=13 ...
```

而两板出厂 `cmdline` 是 `console=ttyS0,115200n8 loglevel=8`，即这些 INFO
全部同步刷到 115200 baud 的串口控制台。实测后果：

| 状态 | Wi-Fi 吞吐 | RTT |
|---|---|---|
| `printk=5`（压制 INFO）| 45–46 Mbps | 1.2–1.8 ms |
| `printk=8` + BT 已上电并做过广播/扫描 | **14–16 Mbps** | avg **16.8 ms**，max 54 ms |

部署时建议先执行：

```sh
dmesg -n 5          # 等价于 echo 5 > /proc/sys/kernel/printk
```

排查问题时再临时调回 `8`。要根治则需关掉 `CONFIG_INFO_LOGS` 重编 ko。

> 注：同一板子在 `printk=8` 下也测到过 45.5 Mbps，所以 `loglevel` 不是唯一变量；
> 但「每包一条 INFO + console 全开」确定是性能隐患，建议默认关掉。

---

### 5.11 【重要】持续打满 Wi-Fi 会把 C5 拖到 RPC 无响应

**不要用长连接压测。** 连续跑 iperf3 超过约 1–2 分钟后，链路会按这个顺序垮掉：

```
① dmesg: BTDBG credit: no H2E buffer (available=0 needed=1 drop#N)
② ip neigh: 192.168.4.1 → FAILED / INCOMPLETE
③ wifi-sta.sh 重启时报:
   eh_host_feat_rpc: request: no response uid=1 msg_id=278 (5000 ms)
   the C5 is not responding: reset it and re-enumerate SDIO
```

实测复现 3 次（2026-11-09 与 11-10，AX637 侧）。②之前 SDIO 与 `wifi-role`
进程都还正常，所以容易被误判成「只是关联掉了」。

**根因**：C5 的 H2E 接收缓冲被持续打满，CP 侧来不及回收，最终连 RPC 通道也
排不上队。

**恢复**：只能按 §5.8 用串口 RTS 硬复位 C5 + 重新枚举 SDIO，清 ARP 或重启
`wifi-role` 都无效。

**建议**：

- 性能验收用**短时多轮**（每轮 ≤10 s，轮间留 2–3 s 冷却），不要 `-t 60` 打满。
- 业务侧如果确实需要长时间大流量，应做限速（如 `tc` 限到 30 Mbps 以下）留出余量。
- 需要看 TSF 波形的现场尤其注意：链路垮掉后 TSF 会进入 `free_run_frames`
  自由运行（`reflost_ms` 持续增长），波形不再反映真实同步。

---

## 6. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `ls /sys/bus/sdio/devices/` 为空 | 上电时 C5 未就绪 | unbind/bind 重新枚举（§3.1）|
| `insmod` 报 `Unknown symbol hci_register_dev` | 内核没有 `CONFIG_BT` | 换带 BT 的内核（§2.1）|
| `capabilities: 0x1` | C5 固件是 Wi-Fi only | 烧 BT 固件（§2.2）|
| `hci0` 不出现 | SDIO 只枚举了 1 个功能 | 同上，先确认 2 个功能 |
| 扫描返回 `0x0c NOT_SUPPORTED` | `type` 传了 `0x01`（经典蓝牙）| 用 `0x06`（工具已处理）|
| 扫描返回 `0x0d INVALID_PARAMS` | `type` 传了 `0x02` | 用 `0x06` |
| 广播命令 `status=0x00` 但对端扫不到 | 用了 MGMT 路径 | 改用 `bt-extadv-tool`（§5.3）|
| `H2E write failed -110` / `slave stalled` | SDIO 写超时 | 检查 SDIO 是否 2 个功能；必要时重启 |
| `eh_host_feat_rpc: request: no response` | C5 已不响应 RPC | 串口 RTS 硬复位（§5.8）后重新枚举 |
| Wi-Fi 吞吐明显偏低（十几 Mbps）| 串口逐包 `printk` | `dmesg -n 5`（§5.10）|
| Wi-Fi 起不来但同时装了 BT | 驱动加载顺序 | 先加载 ko，再起 `wifi-role` |

日志位置：

```sh
dmesg | grep -iE 'esp32_sdio|bluetooth|hci'      # 驱动与 HCI
dmesg | grep 'BTDBG'                              # 诊断版 ko 的埋点
cat /sys/class/bluetooth/hci0/name                # hci0 存在性
```

---

## 7. 工具速查

### bt-mgmt-tool

```sh
bt-mgmt-tool info            # BD_ADDR / HCI 版本 / settings
bt-mgmt-tool power on|off    # 上/下电（触发 HCI Reset）
bt-mgmt-tool scan [秒]       # LE 扫描
bt-mgmt-tool adv on [名字]   # ⚠️ 走内核 MGMT，空口看不到，仅调试用
```

### bt-extadv-tool

```sh
bt-extadv-tool <名字> [秒]   # 开广播（默认一直播）
bt-extadv-tool off           # 停广播
```

### bt-hci-tool

```sh
bt-hci-tool info             # raw HCI 读 BD_ADDR / 版本 / buffer
bt-hci-tool reset            # HCI_Reset
bt-hci-tool scan [秒]        # raw LE 扫描
```

> `bt-hci-tool adv` 走的是 legacy 命令（`0x2006`），**在 C5 上无限期无应答**，
> 不要用。

---

## 8. 重新编译工具

工具源码都在 `app/` 下，交叉编译：

```sh
CC=/home/compiler/arm/arm9/aarch64gxv201912/bin/aarch64-none-linux-gnu-gcc
$CC -O2 -static -o bt-mgmt-tool   app/bt-mgmt-tool.c
$CC -O2 -static -o bt-extadv-tool app/bt-extadv-tool.c
$CC -O2 -static -o bt-hci-tool    app/bt-hci-tool.c
```

静态链接，不依赖 BlueZ / dbus，可以直接跑在只有 busybox 的 rootfs 上。

---

## 9. PC ↔ 板子数据链路测试（含 BT PAN 可行性结论）

### 9.1 结论：BT PAN / BNEP 在这块板子上不可用

想让蓝牙变成一个 `bnep0` 网口、再用 `AF_INET` socket 收发（即"一份代码靠配置
切换 WiFi / BT 两条链路"），本包硬件的**三层条件全不满足**：

| 层 | PAN 需要 | 本板实测 | 结论 |
|---|---|---|---|
| 控制器 | BR/EDR（经典蓝牙）ACL 链路 | ESP32-C5 `capabilities: 0xd`、`BLE only`；`supported_settings=0x0001FE1B` 里无 BREDR 位，`current=0x201` 只有 powered+LE | ❌ |
| 内核 | `CONFIG_BT_BNEP` + `bnep.ko` | `/proc/config.gz`：`CONFIG_BT_BNEP / RFCOMM / HIDP is not set`；全盘无 `bnep.ko` | ❌ |
| 用户态 | BlueZ（`bluetoothd` / `pand`）建 PANU/NAP | 无 `bluetoothd` / `pand` / `hciconfig` / `bluetoothctl` | ❌ |

PC 侧反倒有 PAN 网络适配器（"蓝牙网络连接" 169.254.x.x）和 Intel BR/EDR+BLE
控制器，但**对端没有对等体**。要真做 PAN 得换带经典蓝牙的控制器（例如板子插
USB BT dongle）+ 重编内核开 BNEP + 移植 BlueZ，属于改系统，不是"写个简单程序"。

所以：**WiFi 侧 `AF_INET` 一套代码成立；BT 侧不存在可 bind 的 IP 接口**。
BLE 能做的是广播/扫描（短数据、单向、无连接）或 GATT（需 BlueZ 或自写 ATT，
成本中高，见 §5.2）。

### 9.2 提供的测试程序

| 文件 | 运行位置 | 作用 |
|---|---|---|
| `app/linktest.c` | 板子 / Linux PC | 一份源码两用的 `AF_INET` TCP/UDP echo server/client，统计丢包、吞吐、RTT |
| `app/linktest.py` | PC（Python 3，免编译） | `wifi server/client` 与 `ble scan`，按子命令选链路；BLE 扫描需 `bleak` |
| `scripts/linktest-board.sh` | 板子（busybox sh，免编译） | `status` / `wifi server\|client` / `ble beacon\|off` |

```sh
# 板子（/soc/sdio_wifi）
sh linktest-board.sh status
sh linktest-board.sh wifi server tcp 9000           # nc echo 服务端
sh linktest-board.sh ble beacon HELLO 20            # 数据编码进广播名

# PC
python app/linktest.py wifi client --host 192.168.0.92 --port 9000 \
       --bytes 1048576 --block 4096
python app/linktest.py wifi client --host 192.168.0.92 --port 9000 \
       --bytes 4194304 --block 65536 --stream      # 测带宽而非 RTT
python app/linktest.py ble scan --seconds 10 --prefix LNK:
python app/linktest.py info
```

`ble beacon` 把数据放进 BLE 广播名（前缀 `LNK:`，整名 ≤ 24 字符），PC 用
`bleak` 扫描并解出数据。这是**单向、无确认、每包十几字节**的通道，适合传状态/
传感器值，不适合传文件或流。

C 版编译（板子无 gcc，需交叉编译；本机可用 zig 生成静态 aarch64 ELF）：

```sh
python -m pip install ziglang
python -m ziglang cc -target aarch64-linux-musl -O2 -static -o app/linktest app/linktest.c
python -m ziglang cc -target aarch64-linux-musl -O2 -static \
       -o app/bt-extadv-tool app/bt-extadv-tool.c
# 或 SDK 工具链：aarch64-none-linux-gnu-gcc -O2 -static
```

### 9.3 广播工具的一个 bug（已修）

`app/bt-extadv-tool.c` 的 `adv_data_len` 原写成 `2+2+strlen(name)`，而实际 AD 是
`flags(3B) + name(2+n)` = `5+n`。少算 1 字节导致控制器把名字最后一个字符截掉，
name AD 的长度字段与数据不匹配：**对端能扫到 BD_ADDR 和 RSSI，但解析出的
`local_name` 恒为空**，数据自然也读不出来。已改为 `5+n` 并限制 `n<=26`。

本机没有可用的交叉编译器（SDK 工具链在 Linux 主机、Zig 源下载不完整），所以用
`.workbuddy/patch_extadv.py` 给现成的 aarch64 静态 ELF 打了等价补丁：`adv_on`
被 `-O2` 内联进 `main`，目标指令是 `0x400634: add w19, w0, #4`（后面
`strb w19,[sp,#0x6b]` 写 `data[3]`、`add w3,w19,#4` 算 dlen），把 imm12 由 4
改成 5 即可。原始二进制备份在 `.workbuddy/bt-extadv-tool.orig`，板子上备份为
`/soc/sdio_wifi/bt-extadv-tool.bak`。**正式发布仍应以修复后的源码重新交叉编译为准**，
二进制补丁只是无工具链环境下的应急手段。

### 9.4 实测（2026-11-11，AX650 + PC）

环境：板子 `ethsta0 192.168.0.92/23`（热点 1303），PC WLAN `192.168.0.4/23`，
`hci0 = 10:BD:A3:DF:1E:52`，BT `current=0x201`（powered + LE）。

WiFi（板子 `nc -lk -p 9000 -e cat` echo，PC `linktest.py wifi client`）：

| 模式 | 数据量 | 结果 |
|---|---|---|
| 逐块确认 block=1 KiB | 256 KiB | 256/256 块回显一致，0 丢包，RTT 16.6 ms，0.49 Mbps |
| 逐块确认 block=64 KiB | 1 MiB | 16/16 块一致，0 丢包，RTT 107 ms，4.88 Mbps |
| 连续流 `--stream` | 4 MiB | 4 MiB 全量回显，4.84 s，6.94 Mbps |

> 逐块确认的吞吐受 RTT 限制；连续流也只有 ~7 Mbps，因为板端是 `nc -e cat`
> 用户态逐包转发（叠加串口日志开销），**不是链路上限** —— 空口基线见 §4.2
> （iperf3 约 44 Mbps）。要测链路极限请直接跑 `iperf3 -s` / `iperf3 -c`。

BLE（板子 `ble beacon HELLO-WIFI-BT 20`，PC `ble scan --seconds 12`）：

```text
10:BD:A3:DF:1E:52  rssi=-44  name='LNK:HELLO-WIFI-BT'  data='HELLO-WIFI-BT'
```

12 秒内稳定收到数十条，RSSI −43 ~ −47 dBm。修 `adv_data_len` 之前 `local_name`
恒为空、只能看到地址。**这条是单向、无连接、每包十几字节**的通道，不能替代
PAN 的双向 IP 通道。

PC → 板子方向：Windows 桌面用 WinRT `BluetoothLEAdvertisementPublisher` 发布广播时，
带 `LocalName` 会报 `E_INVALIDARG`，只能发 manufacturer data，而板端
`bt-mgmt-tool` 不解析该字段，本环境未能实测该方向的数据解码。需要 PC→板子的
可靠数据时走 WiFi（已通），或后续做 BLE GATT。
