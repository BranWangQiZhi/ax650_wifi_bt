/*
 * linktest.c -- 极简 AF_INET 收发测试程序，板子 / PC 一份源码两用。
 *
 * 背景（为什么没有 BT 分支）：
 *   ESP32-C5 是 BLE-only 控制器，板子内核 CONFIG_BT_BNEP / RFCOMM 都没开、
 *   rootfs 也没有 BlueZ，所以 BT PAN(BNEP, bnep0) 起不来 —— 不存在一条
 *   可以 bind() 的蓝牙 IP 接口。WiFi 侧则不同：ethsta0 / ethap0 本来就是
 *   标准以太网口，AF_INET socket 直接可用。
 *   如果将来换成支持经典蓝牙的控制器并补齐 BNEP + BlueZ，只要把 client
 *   的 --host 换成 bnep0 上的 IP，本程序一行都不用改就能跑在 BT 上。
 *
 * 编译：
 *   PC   : gcc -O2 -Wall -o linktest linktest.c
 *   board: aarch64-none-linux-gnu-gcc -O2 -static -o linktest linktest.c
 *          （板子上没有 gcc，需交叉编译后 scp 上去）
 *
 * 用法：
 *   linktest server [tcp|udp] [port]
 *   linktest client <host> [tcp|udp] [port] [total_bytes] [block_bytes]
 *
 * 例：
 *   板子: linktest server tcp 9000
 *   PC  : linktest client 192.168.0.92 tcp 9000 1048576 1024
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>

#define DEFAULT_PORT 9000
#define DEFAULT_TOTAL (1024 * 1024)
#define DEFAULT_BLOCK 1024

static double now_s(void)
{
    struct timeval tv;

    gettimeofday(&tv, NULL);
    return (double)tv.tv_sec + (double)tv.tv_usec / 1000000.0;
}

static void fill_pattern(unsigned char *b, int n, unsigned int seq)
{
    int i;

    for (i = 0; i < n; i++)
        b[i] = (unsigned char)((i * 31 + seq * 17 + 7) & 0xff);
}

static int send_all(int fd, const unsigned char *b, int n)
{
    int off = 0;

    while (off < n) {
        int w = (int)send(fd, b + off, n - off, 0);

        if (w <= 0)
            return -1;
        off += w;
    }
    return 0;
}

static int server_tcp(int port)
{
    int s, one = 1;
    struct sockaddr_in a;

    s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) { perror("socket"); return 1; }
    (void)setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_ANY);
    a.sin_port = htons((unsigned short)port);
    if (bind(s, (struct sockaddr *)&a, sizeof(a)) < 0) {
        perror("bind");
        close(s);
        return 1;
    }
    if (listen(s, 4) < 0) { perror("listen"); close(s); return 1; }
    printf("server tcp 0.0.0.0:%d (Ctrl-C 结束)\n", port);
    fflush(stdout);

    for (;;) {
        unsigned char buf[65536];
        long long total = 0;
        double t0;
        int c, n;

        c = accept(s, NULL, NULL);
        if (c < 0) {
            if (errno == EINTR) continue;
            perror("accept");
            break;
        }
        t0 = now_s();
        while ((n = (int)recv(c, buf, sizeof(buf), 0)) > 0) {
            if (send_all(c, buf, n) < 0) break;
            total += n;
        }
        {
            double dt = now_s() - t0;

            printf("conn: echoed %lld bytes in %.3f s (%.2f Mbps)\n",
                   total, dt, dt > 0 ? total * 8.0 / 1000000.0 / dt : 0.0);
        }
        fflush(stdout);
        close(c);
    }
    close(s);
    return 0;
}

static int server_udp(int port)
{
    int s, one = 1;
    struct sockaddr_in a, from;
    unsigned char buf[65536];
    long long total = 0;
    double t0;

    s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) { perror("socket"); return 1; }
    (void)setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_ANY);
    a.sin_port = htons((unsigned short)port);
    if (bind(s, (struct sockaddr *)&a, sizeof(a)) < 0) {
        perror("bind");
        close(s);
        return 1;
    }
    printf("server udp 0.0.0.0:%d (Ctrl-C 结束)\n", port);
    fflush(stdout);
    t0 = now_s();
    for (;;) {
        socklen_t fl = sizeof(from);
        int n = (int)recvfrom(s, buf, sizeof(buf), 0,
                              (struct sockaddr *)&from, &fl);

        if (n <= 0) continue;
        (void)sendto(s, buf, n, 0, (struct sockaddr *)&from, fl);
        total += n;
        {
            double dt = now_s() - t0;

            if (dt >= 5.0) {
                printf(" echoed %lld bytes, %.2f Mbps\n", total,
                       total * 8.0 / 1000000.0 / dt);
                fflush(stdout);
                t0 = now_s();
                total = 0;
            }
        }
    }
    close(s);
    return 0;
}

static int run_client(const char *host, int udp, int port,
                      long long total, int block)
{
    struct addrinfo hints, *res = NULL;
    struct sockaddr_in peer;
    struct timeval to;
    char portstr[16];
    unsigned char *buf, *echo;
    int s, ok = 0, bad = 0;
    long long sent = 0;
    double t0, dt, rtt_sum = 0.0;
    unsigned int seq = 0;

    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;
    hints.ai_socktype = udp ? SOCK_DGRAM : SOCK_STREAM;
    snprintf(portstr, sizeof(portstr), "%d", port);
    if (getaddrinfo(host, portstr, &hints, &res) != 0 || !res) {
        fprintf(stderr, "getaddrinfo(%s) failed\n", host);
        return 1;
    }
    s = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (s < 0) { perror("socket"); return 1; }
    to.tv_sec = 3;
    to.tv_usec = 0;
    (void)setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &to, sizeof(to));
    memset(&peer, 0, sizeof(peer));
    memcpy(&peer, res->ai_addr, sizeof(peer));   /* UDP sendto 需要保留地址 */
    if (!udp && connect(s, res->ai_addr, res->ai_addrlen) < 0) {
        perror("connect");
        close(s);
        return 1;
    }
    freeaddrinfo(res);

    buf = (unsigned char *)malloc((size_t)block);
    echo = (unsigned char *)malloc((size_t)block);
    if (!buf || !echo) { fprintf(stderr, "malloc failed\n"); return 1; }

    printf("client -> %s:%d  proto=%s  total=%lld B  block=%d B\n",
           host, port, udp ? "udp" : "tcp", total, block);
    fflush(stdout);
    t0 = now_s();
    while (sent < total) {
        int chunk = (total - sent < block) ? (int)(total - sent) : block;
        int got = 0;
        double t1;

        fill_pattern(buf, chunk, seq++);
        t1 = now_s();
        if (udp) {
            if (sendto(s, buf, chunk, 0, (struct sockaddr *)&peer,
                       sizeof(peer)) != chunk)
                break;
        } else if (send_all(s, buf, chunk) < 0) {
            break;
        }
        while (got < chunk) {
            int n;

            if (udp) {
                struct sockaddr_in from;
                socklen_t fl = sizeof(from);

                n = (int)recvfrom(s, echo + got, chunk - got, 0,
                                  (struct sockaddr *)&from, &fl);
            } else {
                n = (int)recv(s, echo + got, chunk - got, 0);
            }
            if (n <= 0) break;      /* 超时 / 对端关闭 -> 记一次丢包 */
            got += n;
        }
        dt = now_s() - t1;
        rtt_sum += dt;
        if (got == chunk && memcmp(echo, buf, chunk) == 0)
            ok++;
        else
            bad++;
        sent += chunk;
    }
    dt = now_s() - t0;
    printf("result: %lld B sent, %d blocks ok, %d lost/bad, %.3f s, "
           "%.2f Mbps, avg RTT %.2f ms\n",
           sent, ok, bad, dt,
           dt > 0 ? (double)sent * 8.0 / 1000000.0 / dt : 0.0,
           (ok + bad) > 0 ? rtt_sum * 1000.0 / (ok + bad) : 0.0);
    free(buf);
    free(echo);
    close(s);
    return bad ? 1 : 0;
}

static void usage(const char *p)
{
    printf("usage:\n"
           "  %s server [tcp|udp] [port]\n"
           "  %s client <host> [tcp|udp] [port] [total_bytes] [block_bytes]\n",
           p, p);
}

int main(int argc, char **argv)
{
    const char *mode;
    int udp = 0, port = DEFAULT_PORT;

    if (argc < 2) { usage(argv[0]); return 2; }
    mode = argv[1];

    if (!strcmp(mode, "server")) {
        if (argc > 2) udp = !strcmp(argv[2], "udp");
        if (argc > 3) port = atoi(argv[3]);
        if (port <= 0 || port > 65535) port = DEFAULT_PORT;
        return udp ? server_udp(port) : server_tcp(port);
    }
    if (!strcmp(mode, "client") && argc > 2) {
        const char *host = argv[2];
        long long total = DEFAULT_TOTAL;
        int block = DEFAULT_BLOCK;

        if (argc > 3) udp = !strcmp(argv[3], "udp");
        if (argc > 4) port = atoi(argv[4]);
        if (argc > 5) total = atoll(argv[5]);
        if (argc > 6) block = atoi(argv[6]);
        if (port <= 0 || port > 65535) port = DEFAULT_PORT;
        if (total <= 0) total = DEFAULT_TOTAL;
        if (block <= 0 || block > 65536) block = DEFAULT_BLOCK;
        return run_client(host, udp, port, total, block);
    }
    usage(argv[0]);
    return 2;
}
