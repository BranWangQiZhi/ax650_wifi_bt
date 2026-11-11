/*
 * bt-mgmt-tool -- minimal Bluetooth management (MGMT) client, no BlueZ.
 *
 * Why MGMT instead of raw HCI: the kernel's own BT stack owns hci0, and the
 * mgmt channel is the documented userspace ABI to drive it. Raw HCI sockets
 * bypass cmd_queue bookkeeping and give confusing results (writes accepted,
 * events not delivered). MGMT goes through the same path as bluetoothctl.
 *
 * Subcommands:
 *   info            read controller info (BD_ADDR, version, settings)
 *   power on|off    power the controller
 *   scan [sec]      LE discovery, print address/RSSI/name of found devices
 *
 * Build: aarch64-none-linux-gnu-gcc -O2 -static -o bt-mgmt-tool bt-mgmt-tool.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <poll.h>

#ifndef AF_BLUETOOTH
#define AF_BLUETOOTH 31
#endif
#define BTPROTO_HCI 1
#define HCI_CHANNEL_CONTROL 3

#define MGMT_INDEX_NONE 0xffff
#define MGMT_OP_READ_INDEX_LIST 0x0003
#define MGMT_OP_READ_INFO       0x0004
#define MGMT_OP_SET_POWERED     0x0005
#define MGMT_OP_SET_LE          0x000d
#define MGMT_OP_SET_LOCAL_NAME  0x000f
/*
 * 这两个 opcode 曾被我写成 0x0030 / 0x0031，结果广播恒返 0x0d。
 * 真实含义（kernel include/net/bluetooth/mgmt.h）：
 *   0x0030 = MGMT_OP_LOAD_IRKS          参数 2 + 16*n 字节
 *   0x0031 = MGMT_OP_GET_CONN_INFO      参数 bdaddr
 * 我发 11 字节的广播结构给 LOAD_IRKS，长度检查必然失败 ->
 * MGMT_STATUS_INVALID_PARAMS(0x0d)。这不是"广播不支持"，是发错了命令。
 *
 * 一组容易记错的值，全部照内核抄：
 *   READ_INDEX_LIST 0x0003  READ_INFO        0x0004
 *   SET_POWERED     0x0005  SET_LE           0x000D
 *   SET_LOCAL_NAME  0x000F  START_DISCOVERY  0x0023
 *   STOP_DISCOVERY  0x0024  READ_ADV_FEATURES 0x003D
 *   ADD_ADVERTISING 0x003E  REMOVE_ADVERTISING 0x003F
 */
#define MGMT_OP_ADD_ADVERTISING 0x003e
#define MGMT_OP_READ_ADV_FEATURES 0x003d
#define MGMT_OP_REMOVE_ADVERTISING 0x003f
#define MGMT_OP_START_DISCOVERY 0x0023
#define MGMT_OP_STOP_DISCOVERY  0x0024
#define MGMT_EV_CMD_COMPLETE    0x0001
#define MGMT_EV_CMD_STATUS      0x0002
#define MGMT_EV_DEVICE_FOUND    0x0012

struct sockaddr_hci {
    unsigned short hci_family;
    unsigned short hci_dev;
    unsigned short hci_channel;
};

static int g_fd = -1;
static unsigned short g_index = MGMT_INDEX_NONE;   /* resolved controller idx */

static int mgmt_open(void)
{
    struct sockaddr_hci a;
    int fd = socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI);

    if (fd < 0) {
        fprintf(stderr, "socket(AF_BLUETOOTH): %s\n", strerror(errno));
        return -1;
    }
    memset(&a, 0, sizeof(a));
    a.hci_family = AF_BLUETOOTH;
    a.hci_dev = MGMT_INDEX_NONE;   /* HCI_DEV_NONE: mgmt 通道不绑具体控制器 */
    a.hci_channel = HCI_CHANNEL_CONTROL;
    if (bind(fd, (struct sockaddr *)&a, sizeof(a)) < 0) {
        fprintf(stderr, "bind(CONTROL): %s\n", strerror(errno));
        close(fd);
        return -1;
    }
    return fd;
}

/* 发一条 mgmt 命令：opcode(2) index(2) len(2) + 参数 */
static int mgmt_send(unsigned short opcode, unsigned short index,
                     const void *param, unsigned short plen)
{
    unsigned char buf[512];
    int n = 0;

    buf[n++] = opcode & 0xff;
    buf[n++] = opcode >> 8;
    buf[n++] = index & 0xff;
    buf[n++] = index >> 8;
    buf[n++] = plen & 0xff;
    buf[n++] = plen >> 8;
    if (plen && param) {
        memcpy(buf + n, param, plen);
        n += plen;
    }
    {
        int w = (int)write(g_fd, buf, n);

        /* MGMT 通道的 write() 成功时返回 0（内核 mgmt_control() 的返回值），
         * 不是写入字节数；只有负数才是失败。 */
        if (w < 0) {
            fprintf(stderr, "mgmt write 0x%04x: errno=%d (%s)\n",
                    opcode, errno, strerror(errno));
            return -1;
        }
    }
    return 0;
}

/*
 * 等某条命令的完成事件。返回时把 payload 放到 out（调用方给容量），
 * 返回 status（0 = 成功）。
 */
static int mgmt_wait(unsigned short opcode, unsigned char *out, int out_sz,
                     int *out_len, int timeout_ms)
{
    while (1) {
        unsigned char ev[1024];
        struct pollfd p;
        int n, op, st;
        unsigned short ev_op, ev_idx, ev_len;

        p.fd = g_fd;
        p.events = POLLIN;
        if (poll(&p, 1, timeout_ms) <= 0)
            return -ETIMEDOUT;
        n = read(g_fd, ev, sizeof(ev));
        if (n < 6)
            continue;

        ev_op  = (unsigned short)(ev[0] | (ev[1] << 8));
        ev_idx = (unsigned short)(ev[2] | (ev[3] << 8));
        ev_len = (unsigned short)(ev[4] | (ev[5] << 8));

        if (ev_op == MGMT_EV_CMD_COMPLETE && n >= 8) {
            op = ev[6] | (ev[7] << 8);
            if (op != opcode)
                continue;
            st = (n >= 9) ? ev[8] : 0;
            if (out && out_len) {
                int avail = n - 9;

                if (avail < 0) avail = 0;
                if (avail > out_sz) avail = out_sz;
                memcpy(out, ev + 9, avail);
                *out_len = avail;
            }
            /* CMD_COMPLETE 里 index 无效时用回带回来的 */
            (void)ev_idx;
            return st;
        }
        if (ev_op == MGMT_EV_CMD_STATUS && n >= 9) {
            op = ev[6] | (ev[7] << 8);
            if (op != opcode)
                continue;
            return ev[8];
        }
        (void)ev_len;
    }
}

/* READ_INDEX_LIST 是唯一允许用 MGMT_INDEX_NONE 的命令之一；其余都要真实 index。 */
static int mgmt_resolve_index(void)
{
    unsigned char rsp[64];
    int len = 0, st;

    st = mgmt_send(MGMT_OP_READ_INDEX_LIST, MGMT_INDEX_NONE, NULL, 0);
    if (st < 0)
        return -1;
    st = mgmt_wait(MGMT_OP_READ_INDEX_LIST, rsp, sizeof(rsp), &len, 5000);
    if (st != 0) {
        printf("READ_INDEX_LIST status=0x%02x\n", st);
        return -1;
    }
    if (len < 2) {
        printf("no controller reported by the kernel\n");
        return -1;
    }
    if ((rsp[0] | (rsp[1] << 8)) < 1 || len < 4) {
        printf("controller list is empty (hci0 not registered?)\n");
        return -1;
    }
    g_index = (unsigned short)(rsp[2] | (rsp[3] << 8));
    return 0;
}

/* 读一次 current_settings，用于分步观察 powered/LE/ADVERTISING 位 */
static unsigned int mgmt_current_settings(void)
{
    unsigned char rsp[512];
    int len = 0, st;
    unsigned int cur = 0;

    st = mgmt_send(MGMT_OP_READ_INFO, g_index, NULL, 0);
    if (st < 0)
        return 0;
    st = mgmt_wait(MGMT_OP_READ_INFO, rsp, sizeof(rsp), &len, 5000);
    if (st != 0 || len < 17)
        return 0;
    cur = (unsigned)rsp[13] | ((unsigned)rsp[14] << 8) |
          ((unsigned)rsp[15] << 16) | ((unsigned)rsp[16] << 24);
    return cur;
}

/* MGMT_STATUS 名字表：0x0a 是 BUSY，不是 REJECTED(0x0b)，很容易看错。 */
static const char *mgmt_status_name(int st)
{
    static const char *tab[] = {
        "SUCCESS", "UNKNOWN_COMMAND", "NOT_CONNECTED", "FAILED",
        "CONNECT_FAILED", "AUTH_FAILED", "NOT_PAIRED", "NO_RESOURCES",
        "TIMEOUT", "ALREADY_CONNECTED", "BUSY", "REJECTED",
        "NOT_SUPPORTED", "INVALID_PARAMS", "DISCONNECTED", "NOT_POWERED",
    };

    return (st >= 0 && st < (int)(sizeof(tab) / sizeof(tab[0])))
           ? tab[st] : "?";
}

static int do_info(void)
{
    unsigned char rsp[512];
    int len = 0, st;
    unsigned int settings;

    st = mgmt_send(MGMT_OP_READ_INFO, g_index, NULL, 0);
    if (st < 0)
        return 1;
    st = mgmt_wait(MGMT_OP_READ_INFO, rsp, sizeof(rsp), &len, 5000);
    if (st != 0) {
        printf("READ_INFO status=0x%02x\n", st);
        return 1;
    }
    if (len < 6) {
        printf("READ_INFO short response (%d)\n", len);
        return 1;
    }

    printf("== controller ==\n");
    printf("  index   : %u\n", (unsigned)g_index);
    printf("  BD_ADDR : %02X:%02X:%02X:%02X:%02X:%02X\n",
           rsp[5], rsp[4], rsp[3], rsp[2], rsp[1], rsp[0]);
    if (len >= 8) {
        printf("  version : HCI %u / mfr 0x%04X\n", rsp[6],
               (unsigned)(rsp[7] | ((len >= 9 ? rsp[8] : 0) << 8)));
    }
    if (len >= 13) {
        settings = (unsigned)rsp[9] | ((unsigned)rsp[10] << 8) |
                   ((unsigned)rsp[11] << 16) | ((unsigned)rsp[12] << 24);
        printf("  settings: 0x%08X (powered=%u)\n", settings, settings & 1u);
    }
    if (len >= 17) {
        settings = (unsigned)rsp[13] | ((unsigned)rsp[14] << 8) |
                   ((unsigned)rsp[15] << 16) | ((unsigned)rsp[16] << 24);
        printf("  current : 0x%08X (powered=%u)\n", settings, settings & 1u);
    }
    if (len < 6 || (rsp[0] | rsp[1] | rsp[2] | rsp[3] | rsp[4] | rsp[5]) == 0)
        printf("  ! BD_ADDR 全 0（控制器未设置地址）\n");
    return 0;
}

static int do_power(int on)
{
    unsigned char p = on ? 1 : 0;
    int st;

    st = mgmt_send(MGMT_OP_SET_POWERED, g_index, &p, 1);
    if (st < 0)
        return 1;
    st = mgmt_wait(MGMT_OP_SET_POWERED, NULL, 0, NULL, 5000);
    printf("SET_POWERED(%d): %s (status=0x%02x)\n", on,
           st == 0 ? "OK" : "failed", st);
    return st == 0 ? 0 : 1;
}

/*
 * BLE 广播（MGMT ADD_ADVERTISING）。
 *
 * struct mgmt_cp_add_advertising {
 *     __u8   instance;
 *     __le32 flags;
 *     __le16 duration;     0 = 永久
 *     __le16 timeout;      0 = 永久
 *     __le16 adv_data_len;
 *     __u8   scan_rsp_len;
 *     __u8   data[];       adv_data 后紧跟 scan_rsp
 * } __packed;
 *
 * flags 服务端会替我们拼好 AD 结构，用 LOCAL_NAME 位最省事：内核自己把
 * SET_LOCAL_NAME 设过的名字放进广播包，手机直接能看到。
 *   BIT0 CONNECTABLE, BIT1 DISCOV, BIT6 LOCAL_NAME
 */
#define MGMT_ADV_FLAG_CONNECTABLE 0x00000001u
#define MGMT_ADV_FLAG_DISCOV      0x00000002u
#define MGMT_ADV_FLAG_LOCAL_NAME  0x00000040u

static unsigned int g_adv_flags_override = 0;

static int do_adv(int on, const char *name)
{
    unsigned char p[512];
    int n = 0, st;
    unsigned int flags = g_adv_flags_override ? g_adv_flags_override :
                         (MGMT_ADV_FLAG_CONNECTABLE |
                          MGMT_ADV_FLAG_DISCOV |
                          MGMT_ADV_FLAG_LOCAL_NAME);

    /*
     * 必须先确保控制器已上电。
     *
     * add_advertising() 末尾有一个"什么都不做"的分支：
     *     if (!hdev_is_powered(hdev) || HCI_ADVERTISING || !schedule_instance)
     *             return mgmt_cmd_complete(..., MGMT_STATUS_SUCCESS, ...);
     * 也就是说**未上电时它返回 SUCCESS 但一条 HCI 命令都不下发**，
     * 表现就是"ADD_ADVERTISING OK，但 tx=0、current 里没有 ADVERTISING 位"。
     * 刚 insmod 的 hci0 默认就是 powered=0，所以这里要显式 power on。
     */
    if (on) {
        unsigned char en = 1;

        st = mgmt_send(MGMT_OP_SET_POWERED, g_index, &en, 1);
        if (st == 0) {
            st = mgmt_wait(MGMT_OP_SET_POWERED, NULL, 0, NULL, 8000);
            printf("SET_POWERED(1): %s (status=0x%02x) cur=0x%08x\n",
                   st == 0 ? "OK" : "failed", st, mgmt_current_settings());
        }

        /*
         * 还要单独把 LE 打开。
         *
         * add_advertising() 第一道检查是 mgmt_le_support()：
         *     if (!lmp_le_capable)        -> NOT_SUPPORTED (0x0c)
         *     else if (!HCI_LE_ENABLED)   -> REJECTED     (0x0a)  <-- 这里
         * HCI_LE_ENABLED 只有走过 MGMT SET_LE 才会置位，power on 本身不会。
         * 少了这一步，广播就恒返 0x0a。
         */
        st = mgmt_send(MGMT_OP_SET_LE, g_index, &en, 1);
        if (st == 0) {
            st = mgmt_wait(MGMT_OP_SET_LE, NULL, 0, NULL, 8000);
            printf("SET_LE(1): %s (status=0x%02x) cur=0x%08x "
                   "[LE bit=0x%x]\n",
                   st == 0 ? "OK" : "failed", st, mgmt_current_settings(),
                   mgmt_current_settings() & 0x200u);
        }
    }

    /* struct mgmt_cp_set_local_name {
     *     __u8 name[MGMT_MAX_NAME_LENGTH];        // HCI_MAX_NAME_LENGTH+1 = 249
     *     __u8 short_name[MGMT_MAX_SHORT_NAME_LENGTH]; // 11
     * }   -> MGMT_SET_LOCAL_NAME_SIZE = 260
     *
     * mgmt 通道对长度是**精确匹配**：hci_sock.c 里
     *   if (!var_len && len != handler->data_len) -> MGMT_STATUS_INVALID_PARAMS
     * 多一字节少一字节都会得到 0x0d。 */
    memset(p, 0, sizeof(p));
    strncpy((char *)p, name, 248);
    st = mgmt_send(MGMT_OP_SET_LOCAL_NAME, g_index, p, 260);
    if (st < 0) {
        /*
         * 无 BR/EDR 的控制器（C5 就是）会走到 set_local_name() 末尾的
         *     if (lmp_bredr_capable(hdev)) { __hci_req_update_name(); ... }
         * 整个 req 是空的，hci_req_run() 返回 -ENODATA，这个错误码会
         * 原样从 write() 冒到用户态（errno 61）。名字其实已经写进
         * hdev->dev_name，广播用 LOCAL_NAME flag 时内核会自己拼 AD，
         * 所以这不是错误，继续。
         */
        printf("SET_LOCAL_NAME: write returns %s (expected on BLE-only "
               "controllers), continue\n", strerror(errno));
        st = 0;
    } else {
        st = mgmt_wait(MGMT_OP_SET_LOCAL_NAME, NULL, 0, NULL, 5000);
        printf("SET_LOCAL_NAME(\"%s\"): %s (status=0x%02x)\n", name,
               st == 0 ? "OK" : "failed", st);
    }
    /* 控制器没有 BR/EDR 时，内核的 hci_req_run() 队列为空并返回 -ENODATA，
     * 于是 write() 直接返回 61。名字本身已经写进 hdev->dev_name，广播用
     * LOCAL_NAME flag 时内核会自己拼进 AD 结构，所以这里失败不影响广播，
     * 降级为提示即可（否则广播根本走不到）。 */
    if (st != 0)
        printf("  (提示: 无 BR/EDR 的控制器会返回 ENODATA，名字仍已写入内核，"
               "继续)\n");

    if (!on) {
        p[0] = 0x01;      /* instance 从 1 开始，0 会被判 INVALID_PARAMS */
        st = mgmt_send(MGMT_OP_REMOVE_ADVERTISING, g_index, p, 1);
        if (st < 0)
            return 1;
        st = mgmt_wait(MGMT_OP_REMOVE_ADVERTISING, NULL, 0, NULL, 5000);
        printf("REMOVE_ADVERTISING(0): %s (status=0x%02x)\n",
               st == 0 ? "OK" : "failed", st);
        return st == 0 ? 0 : 1;
    }

    {
        n = 0;
        /* 精确对齐 struct mgmt_cp_add_advertising（__packed，共 11 字节）：
         *   __u8  instance       1
         *   __le32 flags         4
         *   __le16 duration      2
         *   __le16 timeout       2
         *   __u8  adv_data_len   1   <-- 注意是 1 字节，不是 le16！
         *   __u8  scan_rsp_len   1
         *   __u8  data[]
         * 内核校验：data_len != 11 + adv_data_len + scan_rsp_len -> 0x0d
         */
        p[n++] = 0x01;                          /* instance 1（从 1 开始）*/
        p[n++] = (unsigned char)(flags & 0xff); /* flags le32 */
        p[n++] = (unsigned char)((flags >> 8) & 0xff);
        p[n++] = (unsigned char)((flags >> 16) & 0xff);
        p[n++] = (unsigned char)((flags >> 24) & 0xff);
        p[n++] = 0x00; p[n++] = 0x00;           /* duration = 0 (forever) */
        p[n++] = 0x00; p[n++] = 0x00;           /* timeout  = 0 (forever) */
        p[n++] = 0x00;                          /* adv_data_len = 0 */
        p[n++] = 0x00;                          /* scan_rsp_len = 0 */
    }
    /*
     * 重试 BUSY(0x0a)。
     *
     * add_advertising() 里有：
     *     if (adv_busy(hdev)) -> MGMT_STATUS_BUSY
     * 而 adv_busy() 把 pending 的 SET_LE / ADD_ADVERTISING / REMOVE_ADVERTISING
     * 都算"忙"。SET_LE 的 CMD_COMPLETE 已经回到用户态了，但内核要等它背后
     * 的 HCI 命令全部收尾才 mgmt_pending_remove，所以紧接着发 ADD_ADVERTISING
     * 会稳定地撞上 BUSY。退避重试几百毫秒即可。
     */
    for (int attempt = 0; ; attempt++) {
        st = mgmt_send(MGMT_OP_ADD_ADVERTISING, g_index, p, (unsigned short)n);
        if (st < 0)
            return 1;
        st = mgmt_wait(MGMT_OP_ADD_ADVERTISING, NULL, 0, NULL, 8000);
        if (st != 0x0a || attempt >= 8)
            break;
        printf("  ADD_ADVERTISING busy, retry %d ...\n", attempt + 1);
        usleep(300 * 1000);
    }
    printf("ADD_ADVERTISING(flags=0x%02x): %s (status=0x%02x %s)\n",
           flags, st == 0 ? "OK" : "failed", st, mgmt_status_name(st));

    {
        /* 无论成败都读一次 adv 能力/实例数：
         * INVALID_PARAMS 大多来自 instance 校验：
         *   if (cp->instance < 1 || cp->instance > hdev->le_num_of_adv_sets)
         * 把内核认定的实例数打出来，就能区分是"instance 传错"还是
         * "控制器上报的 adv set 数量是 0"。 */
        unsigned char fr[64];
        int fl = 0, fst;

        memset(fr, 0, sizeof(fr));
        fst = mgmt_send(MGMT_OP_READ_ADV_FEATURES, g_index, NULL, 0);
        if (fst == 0)
            fst = mgmt_wait(MGMT_OP_READ_ADV_FEATURES, fr, sizeof(fr), &fl, 5000);
        if (fst == 0 && fl >= 8)
            printf("  [diag] max_instances=%u num_instances=%u "
                   "max_adv_data=%u max_scan_rsp=%u supported_flags=0x%08x\n",
                   fr[6], fr[7], fr[4], fr[5],
                   (unsigned)(fr[0] | (fr[1] << 8) | (fr[2] << 16) |
                              ((unsigned)fr[3] << 24)));
        else
            printf("  [diag] READ_ADV_FEATURES status=0x%02x len=%d\n", fst, fl);
    }
    if (st == 0)
        printf("\n>>> 广播已开启，用手机蓝牙搜索 \"%s\"\n", name);
    return st == 0 ? 0 : 1;
}

static void print_addr(const unsigned char *b)
{
    printf("%02X:%02X:%02X:%02X:%02X:%02X", b[5], b[4], b[3], b[2], b[1], b[0]);
}

static int do_scan(int secs)
{
    /* START_DISCOVERY: type(u8 位掩码) interval(u16) window(u16) */
    unsigned char p[5];
    char name[64];
    int st, left;

    /* MGMT_SETTING_LE (bit 6) 默认关着，不开 LE 的话 START_DISCOVERY 会直接被拒。 */
    {
        unsigned char en = 1;
        int lst;

        lst = mgmt_send(MGMT_OP_SET_LE, g_index, &en, 1);
        if (lst == 0) {
            lst = mgmt_wait(MGMT_OP_SET_LE, NULL, 0, NULL, 5000);
            if (lst != 0)
                printf("SET_LE(1): failed (status=0x%02x)\n", lst);
        }
    }

    /* struct mgmt_cp_start_discovery 只有 1 个字节 type，而且它**不是**
     * 0/1/2 枚举，是 bdaddr_type 的位掩码（kernel: hci_core.h）：
     *
     *   DISCOV_TYPE_BREDR        = BIT(BDADDR_BREDR=0)              = 0x01
     *   DISCOV_TYPE_LE           = BIT(1)|BIT(2)  (public+random)   = 0x06
     *   DISCOV_TYPE_INTERLEAVED  = BIT(0)|BIT(1)|BIT(2)             = 0x07
     *
     * 踩过的坑：发 0x01 会被当成 BR/EDR 发现，C5 没有 BR/EDR，内核回
     * MGMT_STATUS_NOT_SUPPORTED(0x0c)；发 0x02 不匹配任何 case，回
     * INVALID_PARAMS(0x0d)。LE 必须是 0x06。 */
    p[0] = 0x06;

    st = mgmt_send(MGMT_OP_START_DISCOVERY, g_index, p, 1);
    if (st < 0)
        return 1;
    st = mgmt_wait(MGMT_OP_START_DISCOVERY, NULL, 0, NULL, 5000);
    printf("START_DISCOVERY(LE): %s (status=0x%02x)\n",
           st == 0 ? "OK" : "failed", st);
    if (st != 0)
        return 1;

    printf("扫描 %d 秒...\n", secs);
    left = secs;
    while (left > 0) {
        unsigned char ev[1024];
        struct pollfd pf;
        int n;

        pf.fd = g_fd;
        pf.events = POLLIN;
        if (poll(&pf, 1, 1000) <= 0) {
            left--;
            continue;
        }
        n = read(g_fd, ev, sizeof(ev));
        if (n < 6)
            continue;
        if ((unsigned short)(ev[0] | (ev[1] << 8)) != MGMT_EV_DEVICE_FOUND)
            continue;
        {
            /* addr(6) addr_type(1) rssi(1) flags(4) eir_len(2) eir[] */
            int off = 6, dlen, i;
            unsigned char *e;
            int elen = (n >= off + 12) ? (ev[off + 10] | (ev[off + 11] << 8)) : 0;

            if (n < off + 12)
                continue;
            name[0] = 0;
            e = ev + off + 12;
            if (elen > n - (off + 12))
                elen = n - (off + 12);
            dlen = elen;
            for (i = 0; i < dlen; ) {
                int alen = e[i];

                if (alen < 1 || i + alen >= dlen)
                    break;
                if (e[i + 1] == 0x09 || e[i + 1] == 0x08) {
                    int nl = alen - 1;

                    if (nl > 40) nl = 40;
                    memcpy(name, e + i + 2, nl);
                    name[nl] = 0;
                    break;
                }
                i += alen + 1;
            }
            printf("  ");
            print_addr(ev + off);
            printf("  rssi=%d dBm  type=%u  %s\n",
                   (signed char)ev[off + 7], ev[off + 6],
                   name[0] ? name : "(no name)");
        }
        left--;
    }

    st = mgmt_send(MGMT_OP_STOP_DISCOVERY, g_index, NULL, 0);
    if (st == 0)
        mgmt_wait(MGMT_OP_STOP_DISCOVERY, NULL, 0, NULL, 3000);
    printf("扫描结束\n");
    return 0;
}

static void usage(const char *p)
{
    printf("usage: %s [info|power on|power off|scan [sec]|adv on [name [flagsHex]]|adv off]\n", p);
    printf("  talks to the kernel BT stack over the mgmt channel (no BlueZ needed)\n");
}

int main(int argc, char **argv)
{
    const char *cmd = (argc > 1) ? argv[1] : "info";
    int rc = 0;

    if (!strcmp(cmd, "-h") || !strcmp(cmd, "--help")) {
        usage(argv[0]);
        return 0;
    }

    g_fd = mgmt_open();
    if (g_fd < 0)
        return 2;
    if (mgmt_resolve_index() < 0) {
        close(g_fd);
        return 2;
    }

    if (!strcmp(cmd, "info")) {
        rc = do_info();
    } else if (!strcmp(cmd, "power")) {
        int on = (argc > 2 && !strcmp(argv[2], "off")) ? 0 : 1;

        rc = do_power(on);
    } else if (!strcmp(cmd, "scan")) {
        int secs = (argc > 2) ? atoi(argv[2]) : 8;

        if (secs <= 0 || secs > 120)
            secs = 8;
        rc = do_scan(secs);
    } else if (!strcmp(cmd, "adv")) {
        int on = (argc > 2 && !strcmp(argv[2], "off")) ? 0 : 1;
        const char *nm = (argc > 3 && argv[3][0]) ? argv[3] : "AX-WIFI-BT";

        if (argc > 4)
            g_adv_flags_override = (unsigned int)strtoul(argv[4], NULL, 0);
        rc = do_adv(on, nm);
    } else {
        usage(argv[0]);
        rc = 2;
    }

    close(g_fd);
    return rc;
}
