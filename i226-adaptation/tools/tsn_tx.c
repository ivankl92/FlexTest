/* tsn_tx.c - paced raw-Ethernet talker with hardware TX timestamps.
 *
 * Sends a periodic stream of frames on a raw AF_PACKET socket and records the
 * NIC's hardware TX timestamp for every frame, read back from the socket error
 * queue. Writes "seq,tx_hw_ns,sw_tx_ns" to a CSV.
 *
 * Build: gcc -O2 -Wall -Wextra -o tsn_tx tsn_tx.c
 * Run as root (raw socket + SIOCSHWTSTAMP).
 */
#include "tsn_common.h"
#include <getopt.h>
#include <poll.h>
#include <signal.h>

static volatile sig_atomic_t stop_flag = 0;
static void on_signal(int sig) { (void)sig; stop_flag = 1; }

struct tx_record {
    uint64_t tx_hw_ns;
    uint64_t sw_tx_ns;
    uint8_t  have_hw;
    uint8_t  is_sw;      /* timestamp came from the software path, not the NIC */
};

static struct tx_record *records;
static uint32_t records_len;
static uint64_t hw_ts_seen;
static int allow_sw = 0;   /* -S: accept software timestamps when hardware is absent */
static uint32_t ts_every = 1;  /* -N: request a TX timestamp on every Nth frame */

static void usage(const char *p)
{
    fprintf(stderr,
        "usage: %s -i IFACE -d DST_MAC [options]\n"
        "  -i IFACE     egress interface (e.g. enp1s0)\n"
        "  -d MAC       destination MAC (aa:bb:cc:dd:ee:ff)\n"
        "  -n COUNT     number of frames to send        (default 10000)\n"
        "  -r RATE      frames per second               (default 1000)\n"
        "  -s SIZE      frame size in bytes incl. header (default 512, 64..1522)\n"
        "  -p PCP       802.1Q priority code point 0..7, -1 = send untagged (default -1)\n"
        "  -v VID       VLAN id when tagging, 0 = priority-tagged frame (default 0)\n"
        "  -q PRIO      SO_PRIORITY for host-side queue selection (default 0)\n"
        "  -o FILE      output CSV                       (default tx.csv)\n"
        "  -w SECONDS   extra time to wait for trailing TX timestamps (default 2)\n"
        "  -S           accept software TX timestamps when hardware ones are absent\n"
        "               (diagnostic / veth testing only - degrades accuracy)\n"
        "  -N EVERY     request a TX timestamp on every EVERY-th frame only\n"
        "               (default 1 = every frame). The full stream still goes on\n"
        "               the wire; only the timestamp requests are thinned. Use this\n"
        "               when ptp4l on the same interface reports 'timed out while\n"
        "               polling for tx timestamp': the NIC has a small number of TX\n"
        "               timestamp registers and this tool competes with ptp4l for\n"
        "               them. Check `ethtool -S <if> | grep tx_hwtstamp_skipped`.\n",
        p);
}

/*
 * Read whatever the kernel has queued on the error queue. Each message carries
 * SCM_TIMESTAMPING (ts[2] = raw hardware) plus a copy of the original frame,
 * from which we recover our own sequence number.
 */
static void drain_errqueue(int fd)
{
    uint8_t pktbuf[2048];
    uint8_t ctrl[512];
    struct iovec iov = { .iov_base = pktbuf, .iov_len = sizeof(pktbuf) };
    struct msghdr msg;

    for (;;) {
        memset(&msg, 0, sizeof(msg));
        msg.msg_iov = &iov;
        msg.msg_iovlen = 1;
        msg.msg_control = ctrl;
        msg.msg_controllen = sizeof(ctrl);

        ssize_t n = recvmsg(fd, &msg, MSG_ERRQUEUE | MSG_DONTWAIT);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                return;
            if (errno == EINTR)
                continue;
            return;
        }

        uint64_t hw_ns = 0;
        int is_sw = 0;
        for (struct cmsghdr *cm = CMSG_FIRSTHDR(&msg); cm; cm = CMSG_NXTHDR(&msg, cm)) {
            if (cm->cmsg_level == SOL_SOCKET && cm->cmsg_type == SCM_TIMESTAMPING) {
                struct scm_timestamping tss;
                memcpy(&tss, CMSG_DATA(cm), sizeof(tss));
                /* ts[2] is the raw hardware (PHC) timestamp. */
                if (tss.ts[2].tv_sec || tss.ts[2].tv_nsec) {
                    hw_ns = ts_to_ns(&tss.ts[2]);
                    is_sw = 0;
                } else if (allow_sw && (tss.ts[0].tv_sec || tss.ts[0].tv_nsec)) {
                    hw_ns = ts_to_ns(&tss.ts[0]);
                    is_sw = 1;
                }
            }
        }
        if (!hw_ns)
            continue;

        const struct tsn_payload *pl = find_payload(pktbuf, (size_t)n);
        if (!pl)
            continue;
        uint32_t seq = ntohl(pl->seq);
        if (seq >= records_len)
            continue;
        if (!records[seq].have_hw) {
            records[seq].tx_hw_ns = hw_ns;
            records[seq].is_sw = (uint8_t)is_sw;
            records[seq].have_hw = 1;
            hw_ts_seen++;
        }
    }
}

int main(int argc, char **argv)
{
    const char *ifname = NULL;
    const char *out = "tx.csv";
    uint8_t dst[6];
    int have_dst = 0;
    uint32_t count = 10000;
    double rate = 1000.0;
    int size = 512;
    int pcp = -1;
    int vid = 0;
    int sockprio = 0;
    int wait_s = 2;
    int opt;

    while ((opt = getopt(argc, argv, "i:d:n:r:s:p:v:q:o:w:N:Sh")) != -1) {
        switch (opt) {
        case 'i': ifname = optarg; break;
        case 'd': if (parse_mac(optarg, dst) < 0) { fprintf(stderr, "bad MAC\n"); return 2; }
                  have_dst = 1; break;
        case 'n': count = (uint32_t)strtoul(optarg, NULL, 10); break;
        case 'r': rate = atof(optarg); break;
        case 's': size = atoi(optarg); break;
        case 'p': pcp = atoi(optarg); break;
        case 'v': vid = atoi(optarg); break;
        case 'q': sockprio = atoi(optarg); break;
        case 'o': out = optarg; break;
        case 'w': wait_s = atoi(optarg); break;
        case 'S': allow_sw = 1; break;
        case 'N': ts_every = (uint32_t)strtoul(optarg, NULL, 10);
                  if (ts_every == 0) ts_every = 1;
                  break;
        default: usage(argv[0]); return 2;
        }
    }
    if (!ifname || !have_dst || count == 0 || rate <= 0) { usage(argv[0]); return 2; }
    if (size < TSN_MIN_FRAME || size > TSN_MAX_FRAME) {
        fprintf(stderr, "frame size must be %d..%d\n", TSN_MIN_FRAME, TSN_MAX_FRAME);
        return 2;
    }
    if (pcp > 7) { fprintf(stderr, "pcp must be -1..7\n"); return 2; }

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

    uint8_t src[6];
    if (get_if_mac(ifname, src) < 0)
        return 1;

    int fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (fd < 0) { perror("socket(AF_PACKET)"); return 1; }

    struct sockaddr_ll sll;
    memset(&sll, 0, sizeof(sll));
    sll.sll_family = AF_PACKET;
    sll.sll_protocol = htons(ETH_P_ALL);
    sll.sll_ifindex = ifindex;
    if (bind(fd, (struct sockaddr *)&sll, sizeof(sll)) < 0) { perror("bind"); return 1; }

    if (setsockopt(fd, SOL_SOCKET, SO_PRIORITY, &sockprio, sizeof(sockprio)) < 0)
        perror("setsockopt(SO_PRIORITY)");
    else
        fprintf(stderr, "[%s] SO_PRIORITY=%d\n", ifname, sockprio);

    /*
     * The TX *record* bits (TX_HARDWARE / TX_SOFTWARE) say "timestamp this
     * frame"; the generation bits (RAW_HARDWARE / SOFTWARE) say how to report
     * it. Only the record bits can be overridden per packet, via a
     * SO_TIMESTAMPING control message on sendmsg() -- the kernel masks a
     * per-packet cmsg with SOF_TIMESTAMPING_TX_RECORD_MASK.
     *
     * So for sampled timestamping (-N > 1) we leave the record bits OFF on the
     * socket and switch them on per frame for the sampled ones. Frames without
     * the cmsg then never occupy a TX timestamp register, which is the whole
     * point: the I226 has only a handful, and ptp4l on the same interface needs
     * one for every Pdelay_Req/Resp. Losing that race is what puts ptp4l into
     * portState FAULTY.
     *
     * With -N 1 (the default) nothing changes: the record bits stay on the
     * socket and the send path is exactly as before.
     */
    int tsflags = SOF_TIMESTAMPING_RAW_HARDWARE |
                  SOF_TIMESTAMPING_SOFTWARE;
    if (ts_every == 1)
        tsflags |= SOF_TIMESTAMPING_TX_HARDWARE | SOF_TIMESTAMPING_TX_SOFTWARE;
    if (setsockopt(fd, SOL_SOCKET, SO_TIMESTAMPING, &tsflags, sizeof(tsflags)) < 0) {
        perror("setsockopt(SO_TIMESTAMPING)");
        return 1;
    }
    if (ts_every > 1)
        fprintf(stderr, "[%s] sampled timestamping: 1 frame in %u\n", ifname, ts_every);

    /* Build the frame template. */
    uint8_t frame[TSN_MAX_FRAME];
    memset(frame, 0, sizeof(frame));
    size_t off = 0;
    memcpy(frame + off, dst, 6); off += 6;
    memcpy(frame + off, src, 6); off += 6;
    if (pcp >= 0) {
        uint16_t tpid = htons(0x8100);
        uint16_t tci = htons((uint16_t)((pcp & 0x7) << 13 | (vid & 0x0FFF)));
        memcpy(frame + off, &tpid, 2); off += 2;
        memcpy(frame + off, &tci, 2); off += 2;
    }
    uint16_t et = htons(TSN_ETHERTYPE);
    memcpy(frame + off, &et, 2); off += 2;
    size_t payload_off = off;
    if (payload_off + sizeof(struct tsn_payload) > (size_t)size) {
        fprintf(stderr, "frame size too small for header+payload\n");
        return 2;
    }
    /* Deterministic filler so frames are not compressible-empty. */
    for (size_t i = payload_off + sizeof(struct tsn_payload); i < (size_t)size; i++)
        frame[i] = (uint8_t)(i & 0xff);

    records = calloc(count, sizeof(*records));
    if (!records) { perror("calloc"); return 1; }
    records_len = count;

    const uint64_t interval_ns = (uint64_t)(1e9 / rate);
    fprintf(stderr, "[%s] sending %u frames, %.0f pps, %d B, pcp=%d vid=%d -> "
                    "%02x:%02x:%02x:%02x:%02x:%02x\n",
            ifname, count, rate, size, pcp, vid,
            dst[0], dst[1], dst[2], dst[3], dst[4], dst[5]);

    struct timespec next;
    clock_gettime(CLOCK_TAI, &next);
    /* Start on a round 100 ms boundary so repeated runs are comparable. */
    next.tv_nsec = 0;
    next.tv_sec += 1;

    uint64_t sent = 0, send_err = 0, ts_requested = 0;
    for (uint32_t seq = 0; seq < count && !stop_flag; seq++) {
        clock_nanosleep(CLOCK_TAI, TIMER_ABSTIME, &next, NULL);

        struct tsn_payload pl;
        pl.magic = htonl(TSN_MAGIC);
        pl.seq = htonl(seq);
        pl.sw_tx_ns = hton64(now_ns(CLOCK_TAI));
        memcpy(frame + payload_off, &pl, sizeof(pl));
        records[seq].sw_tx_ns = ntoh64(pl.sw_tx_ns);

        ssize_t n;
        if (ts_every == 1) {
            n = send(fd, frame, (size_t)size, 0);
        } else {
            struct iovec iov = { .iov_base = frame, .iov_len = (size_t)size };
            struct msghdr msg = { .msg_iov = &iov, .msg_iovlen = 1 };
            union {
                char buf[CMSG_SPACE(sizeof(uint32_t))];
                struct cmsghdr align;
            } cbuf;
            if (seq % ts_every == 0) {
                uint32_t req = SOF_TIMESTAMPING_TX_HARDWARE;
                if (allow_sw) req |= SOF_TIMESTAMPING_TX_SOFTWARE;
                memset(&cbuf, 0, sizeof(cbuf));
                msg.msg_control = cbuf.buf;
                msg.msg_controllen = sizeof(cbuf.buf);
                struct cmsghdr *cm = CMSG_FIRSTHDR(&msg);
                cm->cmsg_level = SOL_SOCKET;
                cm->cmsg_type  = SO_TIMESTAMPING;
                cm->cmsg_len   = CMSG_LEN(sizeof(uint32_t));
                memcpy(CMSG_DATA(cm), &req, sizeof(req));
                ts_requested++;
            }
            n = sendmsg(fd, &msg, 0);
        }
        if (n < 0) {
            send_err++;
            if (send_err < 10)
                perror("send");
        } else {
            sent++;
        }

        drain_errqueue(fd);

        uint64_t t = ts_to_ns(&next) + interval_ns;
        next.tv_sec = (time_t)(t / 1000000000ULL);
        next.tv_nsec = (long)(t % 1000000000ULL);
    }

    /* Collect trailing timestamps. */
    struct pollfd pfd = { .fd = fd, .events = POLLERR, .revents = 0 };
    uint64_t deadline = now_ns(CLOCK_MONOTONIC) + (uint64_t)wait_s * 1000000000ULL;
    uint64_t expect_ts = (ts_every == 1) ? sent : ts_requested;
    while (now_ns(CLOCK_MONOTONIC) < deadline && hw_ts_seen < expect_ts) {
        poll(&pfd, 1, 100);
        drain_errqueue(fd);
    }
    drain_errqueue(fd);

    FILE *f = fopen(out, "w");
    if (!f) { perror("fopen"); return 1; }
    fprintf(f, "seq,tx_hw_ns,sw_tx_ns,ts_src\n");
    uint64_t written = 0;
    for (uint32_t i = 0; i < records_len; i++) {
        if (!records[i].have_hw)
            continue;
        fprintf(f, "%u,%llu,%llu,%s\n", i,
                (unsigned long long)records[i].tx_hw_ns,
                (unsigned long long)records[i].sw_tx_ns,
                records[i].is_sw ? "sw" : "hw");
        written++;
    }
    fclose(f);

    /* Yield is timestamps recovered over timestamps REQUESTED. With -N > 1 the
     * two differ by construction, and dividing by `sent` would report a
     * healthy sampled run as a catastrophic failure. */
    fprintf(stderr, "[%s] sent=%llu send_errors=%llu ts_requested=%llu "
            "hw_tx_timestamps=%llu (%.2f%%) -> %s\n",
            ifname, (unsigned long long)sent, (unsigned long long)send_err,
            (unsigned long long)expect_ts, (unsigned long long)written,
            expect_ts ? 100.0 * (double)written / (double)expect_ts : 0.0, out);

    /* If the kernel ignored the per-packet cmsg it would have timestamped every
     * frame, so we would see far more timestamps than we asked for. Say so
     * rather than silently reporting a sampled run that never sampled. */
    if (ts_every > 1 && written > expect_ts + expect_ts / 10)
        fprintf(stderr, "[%s] WARNING: %llu timestamps for %llu requests - the kernel "
                        "appears to ignore per-packet SO_TIMESTAMPING on this socket, "
                        "so -N did not reduce TX-timestamp pressure\n",
                ifname, (unsigned long long)written, (unsigned long long)expect_ts);

    if (expect_ts && (double)written / (double)expect_ts < 0.9)
        fprintf(stderr, "[%s] WARNING: many TX timestamps missing - lower the rate (-r), "
                        "raise -N, or the I226 TX timestamp registers are saturating "
                        "(check: ethtool -S %s | grep tx_hwtstamp_skipped)\n",
                ifname, ifname);

    free(records);
    close(fd);
    return 0;
}
