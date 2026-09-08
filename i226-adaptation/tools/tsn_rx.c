/* tsn_rx.c - raw-Ethernet listener with hardware RX timestamps.
 *
 * Captures the measurement stream produced by tsn_tx and records the NIC's
 * hardware (PHC) RX timestamp for each frame. Writes "seq,rx_hw_ns" to a CSV.
 *
 * Build: gcc -O2 -Wall -Wextra -o tsn_rx tsn_rx.c
 * Run as root (raw socket + SIOCSHWTSTAMP).
 *
 * IMPORTANT: start ptp4l BEFORE this program. ptp4l narrows the NIC's RX
 * timestamp filter to PTP frames when it starts; this program then widens it to
 * HWTSTAMP_FILTER_ALL, which still covers PTP.
 */
#include "tsn_common.h"
#include <getopt.h>
#include <poll.h>
#include <signal.h>

static volatile sig_atomic_t stop_flag = 0;
static void on_signal(int sig) { (void)sig; stop_flag = 1; }

static void usage(const char *p)
{
    fprintf(stderr,
        "usage: %s -i IFACE [options]\n"
        "  -i IFACE     ingress interface (e.g. enp1s0)\n"
        "  -t SECONDS   capture duration, 0 = until SIGINT   (default 0)\n"
        "  -n COUNT     stop after COUNT matching frames     (default 0 = unlimited)\n"
        "  -o FILE      output CSV                           (default rx.csv)\n"
        "  -S           accept software RX timestamps when hardware ones are absent\n"
        "               (diagnostic / veth testing only - degrades accuracy)\n",
        p);
}

int main(int argc, char **argv)
{
    const char *ifname = NULL;
    const char *out = "rx.csv";
    int duration = 0;
    unsigned long want = 0;
    int allow_sw = 0;
    int opt;

    while ((opt = getopt(argc, argv, "i:t:n:o:Sh")) != -1) {
        switch (opt) {
        case 'i': ifname = optarg; break;
        case 't': duration = atoi(optarg); break;
        case 'n': want = strtoul(optarg, NULL, 10); break;
        case 'o': out = optarg; break;
        case 'S': allow_sw = 1; break;
        default: usage(argv[0]); return 2;
        }
    }
    if (!ifname) { usage(argv[0]); return 2; }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    if (hwtstamp_enable(ifname, 1, 1) < 0) {
        if (!allow_sw)
            return 1;
        fprintf(stderr, "[%s] continuing without hardware timestamping (-S given)\n", ifname);
    }

    int ifindex = if_index_of(ifname);
    if (ifindex <= 0)
        return 1;

    int fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (fd < 0) { perror("socket(AF_PACKET)"); return 1; }

    struct sockaddr_ll sll;
    memset(&sll, 0, sizeof(sll));
    sll.sll_family = AF_PACKET;
    sll.sll_protocol = htons(ETH_P_ALL);
    sll.sll_ifindex = ifindex;
    if (bind(fd, (struct sockaddr *)&sll, sizeof(sll)) < 0) { perror("bind"); return 1; }

    int rcvbuf = 16 * 1024 * 1024;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));

    int tsflags = SOF_TIMESTAMPING_RX_HARDWARE |
                  SOF_TIMESTAMPING_RAW_HARDWARE |
                  SOF_TIMESTAMPING_RX_SOFTWARE |
                  SOF_TIMESTAMPING_SOFTWARE;
    if (setsockopt(fd, SOL_SOCKET, SO_TIMESTAMPING, &tsflags, sizeof(tsflags)) < 0) {
        perror("setsockopt(SO_TIMESTAMPING)");
        return 1;
    }

    FILE *f = fopen(out, "w");
    if (!f) { perror("fopen"); return 1; }
    fprintf(f, "seq,rx_hw_ns,sw_tx_ns,ts_src\n");

    fprintf(stderr, "[%s] capturing%s%s -> %s\n", ifname,
            duration ? " (timed)" : " (until SIGINT)",
            want ? " (bounded)" : "", out);

    uint8_t pktbuf[2048];
    uint8_t ctrl[512];
    unsigned long matched = 0, no_hw = 0, seen = 0;
    uint64_t end_ns = duration ? now_ns(CLOCK_MONOTONIC) + (uint64_t)duration * 1000000000ULL : 0;

    while (!stop_flag) {
        if (end_ns && now_ns(CLOCK_MONOTONIC) >= end_ns)
            break;
        if (want && matched >= want)
            break;

        struct pollfd pfd = { .fd = fd, .events = POLLIN, .revents = 0 };
        int pr = poll(&pfd, 1, 200);
        if (pr <= 0)
            continue;

        struct iovec iov = { .iov_base = pktbuf, .iov_len = sizeof(pktbuf) };
        struct msghdr msg;
        memset(&msg, 0, sizeof(msg));
        msg.msg_iov = &iov;
        msg.msg_iovlen = 1;
        msg.msg_control = ctrl;
        msg.msg_controllen = sizeof(ctrl);

        ssize_t n = recvmsg(fd, &msg, MSG_DONTWAIT);
        if (n < 0) {
            if (errno == EAGAIN || errno == EINTR)
                continue;
            perror("recvmsg");
            break;
        }
        seen++;

        const struct tsn_payload *pl = find_payload(pktbuf, (size_t)n);
        if (!pl)
            continue;

        uint64_t hw_ns = 0;
        int is_sw = 0;
        for (struct cmsghdr *cm = CMSG_FIRSTHDR(&msg); cm; cm = CMSG_NXTHDR(&msg, cm)) {
            if (cm->cmsg_level == SOL_SOCKET && cm->cmsg_type == SCM_TIMESTAMPING) {
                struct scm_timestamping tss;
                memcpy(&tss, CMSG_DATA(cm), sizeof(tss));
                if (tss.ts[2].tv_sec || tss.ts[2].tv_nsec) {
                    hw_ns = ts_to_ns(&tss.ts[2]);
                    is_sw = 0;
                } else if (allow_sw && (tss.ts[0].tv_sec || tss.ts[0].tv_nsec)) {
                    hw_ns = ts_to_ns(&tss.ts[0]);
                    is_sw = 1;
                }
            }
        }
        if (!hw_ns) { no_hw++; continue; }

        fprintf(f, "%u,%llu,%llu,%s\n", ntohl(pl->seq),
                (unsigned long long)hw_ns,
                (unsigned long long)ntoh64(pl->sw_tx_ns),
                is_sw ? "sw" : "hw");
        matched++;
    }

    fclose(f);
    fprintf(stderr, "[%s] frames_on_wire=%lu matched=%lu without_hw_timestamp=%lu -> %s\n",
            ifname, seen, matched, no_hw, out);
    if (no_hw > 0)
        fprintf(stderr, "[%s] WARNING: %lu measurement frames had no hardware RX timestamp; "
                        "check that HWTSTAMP_FILTER_ALL was accepted and that ptp4l did not "
                        "re-narrow the filter after this program started\n", ifname, no_hw);
    close(fd);
    return matched > 0 ? 0 : 3;
}
