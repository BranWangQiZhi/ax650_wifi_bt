/*
 * bt-hci-tool —— 最小 BLE raw HCI 工具（不依赖 BlueZ / dbus）
 *
 * 为什么需要它：RPC(802.3) 路线下 C5 的 BT 控制器通过 SDIO 暴露成标准 hci0，
 * 但现场 rootfs 通常没有 BlueZ。本工具直接用 AF_BLUETOOTH + HCI socket 与
 * 控制器对话，可以：
 *   info   读 BD_ADDR / 本地版本 / buffer 大小 —— 验证 HCI 通路真的能收发
 *   reset  发 HCI_Reset
 *   adv    开/关 BLE 广播（手机能扫到指定名字）
 *   scan   做一次 LE 扫描，列出周围 BLE 设备
 *
 * 编译（aarch64 静态链接，不依赖板端任何库）：
 *   aarch64-none-linux-gnu-gcc -O2 -static -o bt-hci-tool bt-hci-tool.c
 *
 * 用法：
 *   bt-hci-tool [info|reset|adv on [名字]|adv off|scan [秒]] [-d N] [-U] [-v]
 *
 * 前提：esp32_sdio.ko 已加载且含 BT（/sys/class/bluetooth/hci0 存在），
 *       且没有 BlueZ 之类的 host stack 占用该设备。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <poll.h>

/* 不依赖 bluez headers：这几个值在 Linux 上稳定 */
#ifndef AF_BLUETOOTH
#define AF_BLUETOOTH 31
#endif
#define BTPROTO_HCI 1

#define HCI_CHANNEL_RAW  0
#define HCI_CHANNEL_USER 1

/* HCI ioctl（与内核 net/bluetooth/hci_sock.c 一致） */
#define HCIDEVUP   _IOW('H', 201, int)
#define HCIDEVDOWN _IOW('H', 202, int)

struct sockaddr_hci {
    unsigned short hci_family;
    unsigned short hci_dev;
    unsigned short hci_channel;
};

/* ---- HCI 常量 ---- */
#define HCI_COMMAND_PKT 0x01

#define EVT_CMD_COMPLETE 0x0e
#define EVT_CMD_STATUS   0x0f
#define EVT_LE_META      0x3e

#define OP_RESET             0x0c03
#define OP_READ_BD_ADDR      0x1009
#define OP_READ_LOCAL_VER    0x1001
#define OP_READ_BUF_SIZE     0x1005
#define OP_LE_SET_ADV_PARAM  0x2006
#define OP_LE_SET_ADV_DATA   0x2008
#define OP_LE_SET_ADV_EN     0x200a
#define OP_LE_SET_SCAN_PARAM 0x200b
#define OP_LE_SET_SCAN_EN    0x200c

#define STATUS_OK 0x00

static int g_sock = -1;
static int g_dev = 0;
static int g_verbose = 0;
static int g_channel = HCI_CHANNEL_RAW;

static void hexdump(const char *tag, const unsigned char *d, int n)
{
    int i;

    printf("%s", tag);
    for (i = 0; i < n; i++)
        printf("%02x ", d[i]);
    printf("\n");
}

/*
 * 让控制器进入 UP 状态。
 *
 * HCI_CHANNEL_USER 要求设备已由 hci_dev_open() 打开（内核 HCI_USER_CHANNEL
 * 标志），否则发命令返回 EINVAL。标准做法是 ioctl(HCIDEVUP)：内核 BT core
 * 会自动跑一遍 HCI init 序列（Reset / Read Local Features ...），这一步本身
 * 就是"通路真的能收发"的强证据。
 */
static int hci_dev_up(int dev)
{
    struct sockaddr_hci addr;
    int fd, err;

    fd = socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI);
    if (fd < 0)
        return -1;

    memset(&addr, 0, sizeof(addr));
    addr.hci_family = AF_BLUETOOTH;
    addr.hci_dev = (unsigned short)dev;
    addr.hci_channel = HCI_CHANNEL_RAW;

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }

    if (ioctl(fd, HCIDEVUP, dev) < 0) {
        err = errno;
        close(fd);
        if (err == EALREADY)
            return 0;      /* 已经 up 了 */
        errno = err;
        return -1;
    }
    close(fd);
    return 0;
}

static int hci_dev_down(int dev)
{
    struct sockaddr_hci addr;
    int fd, err;

    fd = socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI);
    if (fd < 0)
        return -1;
    memset(&addr, 0, sizeof(addr));
    addr.hci_family = AF_BLUETOOTH;
    addr.hci_dev = (unsigned short)dev;
    addr.hci_channel = HCI_CHANNEL_RAW;
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }
    if (ioctl(fd, HCIDEVDOWN, dev) < 0) {
        err = errno;
        close(fd);
        errno = err;
        return -1;
    }
    close(fd);
    return 0;
}

static int hci_open(int dev, int channel)
{
    struct sockaddr_hci addr;
    int fd;

    /* 两条通道都要求 HCI_UP：hci_sock_sendmsg() 只有设备 up 时才把命令交给
     * hdev->send。USER 通道的含义是"绕过内核 cmd_queue 独占发送"，不是
     * "设备可以不 up"。对 USER 通道做 HCIDEVDOWN 会让写包静默丢弃
     * （write 成功、dmesg 里没有 bt_tx、也等不到事件）。 */
    if (hci_dev_up(dev) < 0)
        fprintf(stderr, "HCIDEVUP(hci%d) 失败: %s（继续尝试发命令）\n",
                dev, strerror(errno));

    fd = socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI);
    if (fd < 0) {
        fprintf(stderr, "socket(AF_BLUETOOTH) 失败: %s\n", strerror(errno));
        return -1;
    }
    memset(&addr, 0, sizeof(addr));
    addr.hci_family = AF_BLUETOOTH;
    addr.hci_dev = (unsigned short)dev;
    addr.hci_channel = (unsigned short)channel;

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "bind(hci%d, channel=%d) 失败: %s\n",
                dev, channel, strerror(errno));
        if (channel == HCI_CHANNEL_USER)
            fprintf(stderr, "  提示: USER 通道要求控制器处于 down；默认请用 RAW。\n");
        close(fd);
        return -1;
    }
    g_channel = channel;
    return fd;
}

/*
 * 发一条 HCI 命令并等对应的 Command Complete / Status 事件。
 * 返回 status 字节；-1 表示传输层失败。
 *
 * 包格式按通道区分：
 *   RAW  = H4 风格，首字节是 packet type(0x01)
 *   USER = 直接 hdr+params，不带 type
 */
static int hci_cmd(int fd, unsigned short opcode,
                   const unsigned char *param, unsigned char plen,
                   unsigned char *rsp, unsigned char *rsp_len)
{
    unsigned char pkt[260];
    int n = 0, ret = -1;

    /* Linux HCI socket 对 RAW 和 USER 两种通道都要求首字节是 H4 packet type，
     * 内核在 hci_sock_sendmsg() 里统一 skb_pull(1)。少写一个字节就是 EINVAL。 */
    pkt[n++] = HCI_COMMAND_PKT;

    pkt[n++] = (unsigned char)(opcode & 0xff);
    pkt[n++] = (unsigned char)(opcode >> 8);
    pkt[n++] = plen;
    if (plen && param) {
        memcpy(pkt + n, param, plen);
        n += plen;
    }
    if (g_verbose)
        hexdump("  > ", pkt, n);

    if (write(fd, pkt, n) != n) {
        fprintf(stderr, "写 HCI 命令 0x%04x 失败: %s\n", opcode, strerror(errno));
        return -1;
    }

    for (;;) {
        unsigned char ev[300];
        struct pollfd pfd;
        int got;

        pfd.fd = fd;
        pfd.events = POLLIN;
        got = poll(&pfd, 1, 3000);
        if (got <= 0) {
            fprintf(stderr, "等 0x%04x 的事件超时\n", opcode);
            return -1;
        }
        n = read(fd, ev, sizeof(ev));
        if (n < 2)
            continue;
        if (g_channel == HCI_CHANNEL_RAW) {
            memmove(ev, ev + 1, --n);      /* 丢掉 H4 type */
            if (n < 3)
                continue;
        }
        if (g_verbose)
            hexdump("  < ", ev, n);

        if (ev[0] == EVT_CMD_COMPLETE && n >= 7) {
            unsigned short op = (unsigned short)(ev[4] | (ev[5] << 8));
            unsigned char st = ev[6];

            if (op != opcode)
                continue;
            if (rsp && rsp_len) {
                int avail = n - 7;

                if (avail > *rsp_len)
                    avail = *rsp_len;
                memcpy(rsp, ev + 7, avail);
                *rsp_len = (unsigned char)avail;
            }
            ret = st;
            break;
        }
        if (ev[0] == EVT_CMD_STATUS && n >= 7) {
            unsigned short op = (unsigned short)(ev[5] | (ev[6] << 8));
            unsigned char st = ev[3];

            if (op != opcode)
                continue;
            ret = st;
            break;
        }
    }
    return ret;
}

static const char *status_str(int st)
{
    static char buf[32];

    if (st == STATUS_OK)
        return "OK";
    snprintf(buf, sizeof(buf), "失败(0x%02x)", st);
    return buf;
}

static void print_bdaddr(const unsigned char *b)
{
    printf("%02X:%02X:%02X:%02X:%02X:%02X",
           b[5], b[4], b[3], b[2], b[1], b[0]);
}

static int cmd_info(void)
{
    unsigned char rsp[64], len;
    int st;

    printf("== GAP 地址 ==\n");
    len = sizeof(rsp);
    st = hci_cmd(g_sock, OP_READ_BD_ADDR, NULL, 0, rsp, &len);
    if (st != STATUS_OK) {
        printf("  Read_BD_ADDR %s\n", status_str(st));
    } else if (len >= 6) {
        printf("  BD_ADDR : ");
        print_bdaddr(rsp + 1);
        printf("\n");
        if (rsp[1] == 0 && rsp[2] == 0 && rsp[3] == 0 &&
            rsp[4] == 0 && rsp[5] == 0 && rsp[6] == 0)
            printf("  ! 全 0：BD_ADDR 未由 CP 提供（host 侧 set_bdaddr 是空桩）\n");
    }

    printf("\n== 本地版本 ==\n");
    len = sizeof(rsp);
    st = hci_cmd(g_sock, OP_READ_LOCAL_VER, NULL, 0, rsp, &len);
    if (st == STATUS_OK && len >= 8)
        printf("  HCI %u.%u / LMP %u.%u / 厂商 0x%04X / 子版本 0x%04X\n",
               rsp[1], rsp[2], rsp[3], rsp[4],
               (unsigned)(rsp[5] | (rsp[6] << 8)),
               (unsigned)(rsp[7] | (rsp[8] << 8)));
    else
        printf("  Read_Local_Version %s\n", status_str(st));

    printf("\n== 控制器缓冲 ==\n");
    len = sizeof(rsp);
    st = hci_cmd(g_sock, OP_READ_BUF_SIZE, NULL, 0, rsp, &len);
    if (st == STATUS_OK && len >= 7)
        printf("  ACL MTU %u / SCO MTU %u / ACL 包 %u / SCO 包 %u\n",
               (unsigned)(rsp[1] | (rsp[2] << 8)),
               (unsigned)rsp[3],
               (unsigned)(rsp[4] | (rsp[5] << 8)),
               (unsigned)(rsp[6] | (rsp[7] << 8)));
    else
        printf("  Read_Buffer_Size %s\n", status_str(st));

    return 0;
}

static int cmd_reset(void)
{
    unsigned char len = 0;
    int st = hci_cmd(g_sock, OP_RESET, NULL, 0, NULL, &len);

    printf("HCI_Reset: %s\n", status_str(st));
    return st == STATUS_OK ? 0 : 1;
}

/* adv on：设广播参数 + 广播数据 + 使能；adv off：关广播。 */
static int cmd_adv(int on, const char *name)
{
    unsigned char p[32], len;
    int st;

    if (on) {
        unsigned char param[15];

        param[0] = 0x20;      /* interval 0x20 * 0.625ms = 20ms */
        param[1] = 0x00;
        param[2] = 0x20;
        param[3] = 0x00;
        param[4] = 0x00;      /* ADV_IND 可连接非定向 */
        param[5] = 0x00;      /* own addr: public */
        param[6] = 0x00;      /* peer addr type */
        memset(param + 7, 0, 6);
        param[13] = 0x07;     /* ch 37/38/39 */
        param[14] = 0x00;     /* no filter */

        len = 0;
        st = hci_cmd(g_sock, OP_LE_SET_ADV_PARAM, param, 15, NULL, &len);
        printf("LE_Set_Advertising_Parameters: %s\n", status_str(st));
        if (st != STATUS_OK)
            return 1;

        memset(p, 0, sizeof(p));
        p[0] = 0x02; p[1] = 0x01; p[2] = 0x06;          /* 可发现 */
        p[3] = (unsigned char)(strlen(name) + 1);
        p[4] = 0x09;                                     /* Complete Local Name */
        memcpy(p + 5, name, strlen(name));

        len = 0;
        st = hci_cmd(g_sock, OP_LE_SET_ADV_DATA, p, 32, NULL, &len);
        printf("LE_Set_Advertising_Data (\"%s\"): %s\n", name, status_str(st));
        if (st != STATUS_OK)
            return 1;
    }

    p[0] = on ? 0x01 : 0x00;
    len = 0;
    st = hci_cmd(g_sock, OP_LE_SET_ADV_EN, p, 1, NULL, &len);
    printf("LE_Set_Advertising_Enable(%d): %s\n", on, status_str(st));

    if (on && st == STATUS_OK)
        printf("\n>>> 广播已开启，用手机蓝牙搜索 \"%s\"\n", name);
    return st == STATUS_OK ? 0 : 1;
}

/* LE 扫描：收 LE Advertising Report，打印 MAC / RSSI / 名字 */
static int cmd_scan(int seconds)
{
    unsigned char p[8], len;
    int st, left;

    p[0] = 0x01;   /* 主动扫描 */
    p[1] = 0x10;   /* interval 10ms */
    p[2] = 0x00;
    p[3] = 0x10;   /* window */
    p[4] = 0x00;
    p[5] = 0x00;   /* own addr public */
    p[6] = 0x00;   /* filter: 全部 */

    len = 0;
    st = hci_cmd(g_sock, OP_LE_SET_SCAN_PARAM, p, 7, NULL, &len);
    printf("LE_Set_Scan_Parameters: %s\n", status_str(st));
    if (st != STATUS_OK)
        return 1;

    p[0] = 0x01;
    p[1] = 0x00;
    len = 0;
    st = hci_cmd(g_sock, OP_LE_SET_SCAN_EN, p, 2, NULL, &len);
    printf("LE_Set_Scan_Enable(1): %s\n", status_str(st));
    if (st != STATUS_OK)
        return 1;

    printf("扫描 %d 秒...\n", seconds);
    left = seconds;
    while (left > 0) {
        unsigned char ev[600];
        struct pollfd pfd;
        int n;

        pfd.fd = g_sock;
        pfd.events = POLLIN;
        if (poll(&pfd, 1, 1000) > 0) {
            n = read(g_sock, ev, sizeof(ev));
            if (n > 2 && g_channel == HCI_CHANNEL_RAW) {
                memmove(ev, ev + 1, --n);
            }
            if (n > 0 && ev[0] == EVT_LE_META && ev[1] >= 1 &&
                ev[3] == 0x02 && n >= 12) {
                int num = ev[4];
                int off = 5, i;

                for (i = 0; i < num && off + 9 <= n; i++) {
                    int evt = ev[off];
                    unsigned char addr[6];
                    int dlen = ev[off + 8];
                    char name[64];

                    memcpy(addr, ev + off + 2, 6);
                    name[0] = 0;
                    if (dlen > 0 && off + 9 + dlen <= n) {
                        unsigned char *d = ev + off + 9;
                        int k = 0;

                        while (k < dlen) {
                            int alen = d[k];

                            if (alen == 0)
                                break;
                            if (d[k + 1] == 0x09 || d[k + 1] == 0x08) {
                                int nl = alen - 1;

                                if (nl > 40)
                                    nl = 40;
                                memcpy(name, d + k + 2, nl);
                                name[nl] = 0;
                                break;
                            }
                            k += alen + 1;
                        }
                    }
                    printf("  [%d] ", i);
                    print_bdaddr(addr);
                    printf("  rssi=%d dBm  %s%s\n",
                           (signed char)ev[off + 1],
                           (evt == 0x04) ? "SCAN_RSP " : "",
                           name[0] ? name : "(无名)");
                    off += 9 + dlen;
                }
            }
        }
        left--;
    }

    p[0] = 0x00;
    p[1] = 0x00;
    len = 0;
    hci_cmd(g_sock, OP_LE_SET_SCAN_EN, p, 2, NULL, &len);
    printf("扫描结束\n");
    return 0;
}

static void usage(const char *p)
{
    printf("用法: %s [info|reset|adv on [名字]|adv off|scan [秒]] [-d N] [-U] [-v]\n", p);
    printf("  -d N  指定 hci 设备号（默认 0）\n");
    printf("  -U    用 HCI_CHANNEL_USER（控制器必须处于 down）\n");
    printf("  -v    打印收发的原始 HCI 包\n");
    printf("默认走 HCI_CHANNEL_RAW（控制器由本工具自动 HCIDEVUP）。\n");
}

int main(int argc, char **argv)
{
    const char *pos[4] = { NULL, NULL, NULL, NULL };
    const char *cmd;
    const char *adv_name = "AX-WIFI-BT";
    int channel = HCI_CHANNEL_RAW;
    int i, np = 0, rc = 0;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-d") && i + 1 < argc) {
            g_dev = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "-U")) {
            channel = HCI_CHANNEL_USER;
        } else if (!strcmp(argv[i], "-r")) {
            channel = HCI_CHANNEL_RAW;
        } else if (!strcmp(argv[i], "-v")) {
            g_verbose = 1;
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            usage(argv[0]);
            return 0;
        } else if (argv[i][0] == '-') {
            fprintf(stderr, "未知选项: %s\n", argv[i]);
            usage(argv[0]);
            return 2;
        } else if (np < 4) {
            pos[np++] = argv[i];
        }
    }

    cmd = np >= 1 ? pos[0] : "info";

    g_sock = hci_open(g_dev, channel);
    if (g_sock < 0)
        return 2;

    if (!strcmp(cmd, "info")) {
        rc = cmd_info();
    } else if (!strcmp(cmd, "reset")) {
        rc = cmd_reset();
    } else if (!strcmp(cmd, "adv")) {
        int on = (np >= 2 && !strcmp(pos[1], "off")) ? 0 : 1;

        if (np >= 3 && pos[2][0])
            adv_name = pos[2];
        rc = cmd_adv(on, adv_name);
    } else if (!strcmp(cmd, "scan")) {
        int secs = (np >= 2) ? atoi(pos[1]) : 8;

        if (secs <= 0 || secs > 120)
            secs = 8;
        rc = cmd_scan(secs);
    } else {
        usage(argv[0]);
        rc = 2;
    }

    close(g_sock);
    return rc;
}
