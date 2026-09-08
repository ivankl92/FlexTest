/* tsn_common.h - shared helpers for the TSN-FlexTest I226 measurement tools
 *
 * Replaces the original testbed's patched-igb "one-step timestamp written into
 * the payload at byte offset 48" mechanism, which cannot work on Intel I225/I226
 * (igc driver). Instead we use the standard Linux SO_TIMESTAMPING API:
 *   - TX: SOF_TIMESTAMPING_TX_HARDWARE, timestamp read back from the socket
 *         error queue after the NIC has actually put the frame on the wire.
 *   - RX: SOF_TIMESTAMPING_RX_HARDWARE, timestamp delivered as a control message.
 * Both timestamps are raw PHC time. With gPTP (ptp4l) locking both PHCs to the
 * same grandmaster, subtracting them yields true NIC-to-NIC one-way latency.
 */
#ifndef TSN_COMMON_H
#define TSN_COMMON_H

#define _GNU_SOURCE
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <net/if.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <linux/if_packet.h>
#include <linux/if_ether.h>
#include <linux/net_tstamp.h>
#include <linux/sockios.h>
#include <linux/errqueue.h>

#define TSN_MAGIC       0x54534E46u   /* "TSNF" */
#define TSN_ETHERTYPE   0x88B5        /* IEEE 802 local experimental Ethertype 1 */
#define TSN_MIN_FRAME   64
#define TSN_MAX_FRAME   1522

/* Payload laid out immediately after the Ethernet (and optional VLAN) header. */
struct tsn_payload {
    uint32_t magic;      /* TSN_MAGIC, network order */
    uint32_t seq;        /* sequence number, network order */
    uint64_t sw_tx_ns;   /* CLOCK_TAI at send() time, network order (diagnostic) */
} __attribute__((packed));

static inline uint64_t ts_to_ns(const struct timespec *ts)
{
    return (uint64_t)ts->tv_sec * 1000000000ULL + (uint64_t)ts->tv_nsec;
}

static inline uint64_t now_ns(clockid_t clk)
{
    struct timespec ts;
    clock_gettime(clk, &ts);
    return ts_to_ns(&ts);
}

/* htonll / ntohll are not portable; define our own. */
static inline uint64_t hton64(uint64_t v)
{
    uint8_t b[8];
    for (int i = 0; i < 8; i++)
        b[i] = (uint8_t)(v >> (56 - 8 * i));
    uint64_t out;
    memcpy(&out, b, 8);
    return out;
}
static inline uint64_t ntoh64(uint64_t v) { return hton64(v); }

static inline int parse_mac(const char *s, uint8_t mac[6])
{
    unsigned int v[6];
    if (sscanf(s, "%x:%x:%x:%x:%x:%x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6)
        return -1;
    for (int i = 0; i < 6; i++) {
        if (v[i] > 0xff)
            return -1;
        mac[i] = (uint8_t)v[i];
    }
    return 0;
}

/*
 * Ask the NIC to hardware-timestamp packets.
 *
 * rx_all: request HWTSTAMP_FILTER_ALL so our experimental-Ethertype frames get
 * RX timestamps (the PTP-only filters would ignore them). FILTER_ALL is a
 * superset of the PTP filter, so ptp4l keeps working - but only if ptp4l is
 * started FIRST, since ptp4l narrows the filter back to PTP when it starts.
 */
static int hwtstamp_enable(const char *ifname, int tx_on, int rx_all)
{
    struct ifreq ifr;
    struct hwtstamp_config cfg;
    int fd, rc;

    memset(&cfg, 0, sizeof(cfg));
    cfg.flags = 0;
    cfg.tx_type = tx_on ? HWTSTAMP_TX_ON : HWTSTAMP_TX_OFF;
    cfg.rx_filter = rx_all ? HWTSTAMP_FILTER_ALL : HWTSTAMP_FILTER_NONE;

    memset(&ifr, 0, sizeof(ifr));
    strncpy(ifr.ifr_name, ifname, IFNAMSIZ - 1);
    ifr.ifr_data = (void *)&cfg;

    fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        perror("socket(AF_INET) for SIOCSHWTSTAMP");
        return -1;
    }
    rc = ioctl(fd, SIOCSHWTSTAMP, &ifr);
    if (rc < 0) {
        fprintf(stderr, "SIOCSHWTSTAMP on %s failed: %s\n", ifname, strerror(errno));
        fprintf(stderr, "  (need root; and the driver must support "
                        "HWTSTAMP_FILTER_ALL / HWTSTAMP_TX_ON)\n");
        close(fd);
        return -1;
    }
    close(fd);

    /* The kernel writes back what it actually applied. */
    fprintf(stderr, "[%s] hwtstamp applied: tx_type=%d rx_filter=%d\n",
            ifname, cfg.tx_type, cfg.rx_filter);
    if (rx_all && cfg.rx_filter != HWTSTAMP_FILTER_ALL &&
        cfg.rx_filter != HWTSTAMP_FILTER_SOME) {
        fprintf(stderr, "[%s] WARNING: driver refused FILTER_ALL (got %d); "
                        "RX hardware timestamps for measurement frames may be absent\n",
                ifname, cfg.rx_filter);
    }
    return 0;
}

static int if_index_of(const char *ifname)
{
    int idx = (int)if_nametoindex(ifname);
    if (idx == 0)
        fprintf(stderr, "unknown interface '%s': %s\n", ifname, strerror(errno));
    return idx;
}

__attribute__((unused)) static int get_if_mac(const char *ifname, uint8_t mac[6])
{
    struct ifreq ifr;
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0)
        return -1;
    memset(&ifr, 0, sizeof(ifr));
    strncpy(ifr.ifr_name, ifname, IFNAMSIZ - 1);
    if (ioctl(fd, SIOCGIFHWADDR, &ifr) < 0) {
        perror("SIOCGIFHWADDR");
        close(fd);
        return -1;
    }
    memcpy(mac, ifr.ifr_hwaddr.sa_data, 6);
    close(fd);
    return 0;
}

/*
 * Locate our payload inside a raw frame. The frame may or may not carry an
 * 802.1Q tag, and on RX the tag may have been stripped by the NIC, so we scan
 * a small window for the magic rather than assuming a fixed offset.
 */
static const struct tsn_payload *find_payload(const uint8_t *buf, size_t len)
{
    const size_t scan_start = 12;              /* right after the MAC addresses */
    const size_t scan_end = 12 + 12;           /* enough for QinQ + ethertype  */
    uint32_t magic_be = htonl(TSN_MAGIC);

    for (size_t off = scan_start; off + sizeof(struct tsn_payload) <= len && off <= scan_end; off++) {
        if (memcmp(buf + off, &magic_be, 4) == 0)
            return (const struct tsn_payload *)(buf + off);
    }
    return NULL;
}

#endif /* TSN_COMMON_H */
