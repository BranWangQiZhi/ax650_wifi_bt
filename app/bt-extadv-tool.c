/*
 * bt-extadv-tool -- BLE 广播（extended advertising）最小工具，不依赖 BlueZ
 *
 * 为什么必须用 ext adv：
 *   CP 固件开了 CONFIG_BT_LE_EXT_ADV=y，控制器只应答 0x2036/0x2037/0x2039；
 *   老的 legacy 命令 0x2006/0x2008/0x200a 不会回应答（现场实测超时）。
 *
 * 为什么不能靠 read() 判成败：
 *   RPC/802.3 路由下 hci_send_to_sock() 的 promisc 分发对 raw socket 不生效，
 *   命令和应答在内核里都是通的（dmesg 可见 BTDBG bt_tx / bt_rx），
 *   但用户态 read() 拿不到事件。请用：
 *       dmesg | grep 'BTDBG bt_rx' | tail
 *   核对 opcode 与 status（0x00 = 成功）。
 *
 * 编译（aarch64 静态链接）：
 *   aarch64-none-linux-gnu-gcc -O2 -static -o bt-extadv-tool bt-extadv-tool.c
 *
 * 用法：
 *   bt-extadv-tool <名字> [秒]     开广播（不给秒数则一直播，Ctrl-C 结束）
 *   bt-extadv-tool off             关广播
 *
 * 前提：esp32_sdio.ko 已加载且含 BT；hci0 已上电（先 SET_POWERED(1)）。
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
#define HCI_CHANNEL_RAW 0
#define HCIDEVUP _IOW('H', 201, int)

struct sockaddr_hci {
	unsigned short hci_family;
	unsigned short hci_dev;
	unsigned short hci_channel;
};

#define OP_LE_SET_EXT_ADV_PARAM 0x2036
#define OP_LE_SET_EXT_ADV_DATA  0x2037
#define OP_LE_SET_EXT_ADV_EN    0x2039

static void dump(const char *tag, const unsigned char *d, int n)
{
	int i;

	printf("%s", tag);
	for (i = 0; i < n; i++)
		printf("%02x ", d[i]);
	printf("\n");
	fflush(stdout);	/* 重定向到文件时是全缓冲，不刷会丢 */
}

static int send_cmd(int fd, unsigned short opcode, const unsigned char *param, int plen)
{
	unsigned char pkt[300];
	int n = 0, i;

	pkt[n++] = 0x01;                               /* H4 command */
	pkt[n++] = (unsigned char)(opcode & 0xff);
	pkt[n++] = (unsigned char)(opcode >> 8);
	pkt[n++] = (unsigned char)plen;
	if (plen)
		memcpy(pkt + n, param, plen);
	n += plen;

	dump("  > ", pkt, n);
	if (write(fd, pkt, n) != n) {
		printf("  write 失败: %s\n", strerror(errno));
		return -1;
	}

	/* 事件回读：802.3(RPC) 路由下 hdev->promisc 分发对 RAW socket 不可靠，
	 * 收不到事件不代表命令失败。命令的 status 以内核 dmesg 的
	 * "BTDBG bt_rx ... status=0x00" 为准，这里只尽力读一下。 */
	for (i = 0; i < 2; i++) {
		unsigned char ev[300];
		struct pollfd pfd = { .fd = fd, .events = POLLIN };
		int got, k;

		got = poll(&pfd, 1, 150);
		if (got <= 0)
			break;
		got = read(fd, ev, sizeof(ev));
		if (got <= 0)
			continue;
		dump("  < ", ev, got);
		k = (ev[0] == 0x04) ? 1 : 0;
		if (ev[k] == 0x0e || ev[k] == 0x0f) {
			int stt = (ev[k] == 0x0e) ? ev[got - 1] : ev[k + 2];

			printf("  => 应答 status=0x%02x\n", stt);
			return stt;
		}
	}
	printf("  .. 未从 socket 读到事件（以 dmesg 的 BTDBG bt_rx 为准）\n");
	return 0;
}

static int adv_on(int fd, const char *name)
{
	unsigned char param[25];
	unsigned char data[40];
	int dlen, st;

	/* 0x2036 LE Set Extended Advertising Parameters (25 字节) */
	memset(param, 0, sizeof(param));
	param[0] = 0x00;                        /* adv handle 0 */
	param[1] = 0x13;                        /* props: connectable|scannable|legacy */
	param[2] = 0x00;
	param[3] = 0x20;                        /* prim min interval 0x20*0.625=20ms */
	param[6] = 0x20;                        /* prim max interval */
	param[9] = 0x07;                        /* ch 37/38/39 */
	param[10] = 0x00;                       /* own addr public */
	param[11] = 0x00;                       /* peer addr type */
	/* param[12..17] peer addr = 0 */
	param[18] = 0x00;                       /* filter policy */
	param[19] = 0x7f;                       /* tx power 无偏好 */
	param[20] = 0x01;                       /* prim phy LE 1M */
	param[22] = 0x01;                       /* sec phy LE 1M */
	st = send_cmd(fd, OP_LE_SET_EXT_ADV_PARAM, param, 25);
	printf("LE_Set_Extended_Advertising_Parameters: status=0x%02x\n", st);
	if (st < 0)
		return 1;

	/* 0x2037 LE Set Extended Advertising Data */
	memset(data, 0, sizeof(data));
	data[0] = 0x00;                         /* handle */
	data[1] = 0x03;                         /* complete data */
	data[2] = 0x01;                         /* frag pref */
	data[3] = (unsigned char)(2 + 2 + strlen(name));
	data[4] = 0x02; data[5] = 0x01; data[6] = 0x06;   /* flags: LE general discoverable */
	data[7] = (unsigned char)(strlen(name) + 1);
	data[8] = 0x09;                         /* complete local name */
	memcpy(data + 9, name, strlen(name));
	dlen = 4 + data[3];
	st = send_cmd(fd, OP_LE_SET_EXT_ADV_DATA, data, dlen);
	printf("LE_Set_Extended_Advertising_Data(\"%s\"): status=0x%02x\n", name, st);
	if (st < 0)
		return 1;

	return 0;
}

static int adv_en(int fd, int on)
{
	unsigned char p[6];

	memset(p, 0, sizeof(p));
	p[0] = on ? 0x01 : 0x00;                /* enable */
	p[1] = 0x01;                            /* num sets */
	p[2] = 0x00;                            /* handle 0 */
	p[3] = 0x00; p[4] = 0x00;               /* duration 0 = 一直 */
	p[5] = 0x00;                            /* max events 0 */
	return send_cmd(fd, OP_LE_SET_EXT_ADV_EN, p, 6);
}

int main(int argc, char **argv)
{
	struct sockaddr_hci addr;
	int fd, dev = 0;

	/* 强制行缓冲：stdout 重定向到文件时默认是全缓冲，进程被 SIGKILL
	 * 结束时整段缓冲区会丢失，看起来像"命令没执行"。广播是常驻
	 * 进程，被 kill 是常态，所以必须设。 */
	setvbuf(stdout, NULL, _IOLBF, 0);

	if (argc < 2) {
		printf("用法: %s <名字> [秒]  |  %s off\n", argv[0], argv[0]);
		return 2;
	}

	/* 确保 hci0 处于 up（内核 BT core 会跑一遍 init，这一步本身就是通路证据） */
	{
		int f = socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI);

		if (f >= 0) {
			memset(&addr, 0, sizeof(addr));
			addr.hci_family = AF_BLUETOOTH;
			addr.hci_dev = (unsigned short)dev;
			addr.hci_channel = HCI_CHANNEL_RAW;
			if (bind(f, (struct sockaddr *)&addr, sizeof(addr)) == 0)
				ioctl(f, HCIDEVUP, dev);
			close(f);
		}
	}

	fd = socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI);
	if (fd < 0) {
		printf("socket 失败: %s\n", strerror(errno));
		return 1;
	}
	memset(&addr, 0, sizeof(addr));
	addr.hci_family = AF_BLUETOOTH;
	addr.hci_dev = (unsigned short)dev;
	addr.hci_channel = HCI_CHANNEL_RAW;
	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		printf("bind 失败: %s\n", strerror(errno));
		return 1;
	}

	if (!strcmp(argv[1], "off")) {
		printf("关闭广播: status=0x%02x\n", adv_en(fd, 0));
		return 0;
	}

	if (adv_on(fd, argv[1]))
		return 1;

	printf("使能广播: status=0x%02x\n", adv_en(fd, 1));
	printf(">>> 广播中，名字 \"%s\"，按 Ctrl-C 结束\n", argv[1]);

	if (argc >= 3) {
		sleep((unsigned)atoi(argv[2]));
		printf("关闭广播: status=0x%02x\n", adv_en(fd, 0));
	} else {
		for (;;)
			pause();
	}
	return 0;
}
