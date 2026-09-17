// What of lwIP this firmware compiles in, and what it leaves out.
//
// The board has 520 kB of RAM for everything: the stack, the buffers, the queue of
// readings waiting for a broker, and the wireless firmware's own working memory. So this
// is a small configuration on purpose — no sockets, no threads, no server of any kind.
// What a satellite does is open one outbound connection and ask what time it is.

#ifndef SENTRY_LWIPOPTS_H
#define SENTRY_LWIPOPTS_H

#include <stdint.h>

// No operating system under it: lwIP runs from the SDK's background context, and nothing
// in this firmware may call into it from anywhere else.
#define NO_SYS 1
#define LWIP_SOCKET 0
#define LWIP_NETCONN 0

#define MEM_LIBC_MALLOC 0
#define MEM_ALIGNMENT 4
#define MEM_SIZE 4000
#define MEMP_NUM_TCP_SEG 32
#define MEMP_NUM_ARP_QUEUE 10
#define PBUF_POOL_SIZE 24
// One more timer than lwIP's own, for the poll that asks the time again.
#define MEMP_NUM_SYS_TIMEOUT (LWIP_NUM_SYS_TIMEOUT_INTERNAL + 1)

#define LWIP_ARP 1
#define LWIP_ETHERNET 1
#define LWIP_ICMP 1
#define LWIP_RAW 0
#define LWIP_IPV4 1
#define LWIP_IPV6 0
#define LWIP_TCP 1
#define LWIP_UDP 1
#define LWIP_DNS 1
#define LWIP_DHCP 1
#define LWIP_TCP_KEEPALIVE 1
#define LWIP_NETIF_HOSTNAME 1
#define LWIP_NETIF_STATUS_CALLBACK 1
#define LWIP_NETIF_LINK_CALLBACK 1
#define LWIP_NETIF_TX_SINGLE_PBUF 1
#define DHCP_DOES_ARP_CHECK 0
#define LWIP_DHCP_DOES_ACD_CHECK 0

#define TCP_MSS 1460
#define TCP_WND (8 * TCP_MSS)
#define TCP_SND_BUF (8 * TCP_MSS)
#define TCP_SND_QUEUELEN ((4 * (TCP_SND_BUF) + (TCP_MSS - 1)) / (TCP_MSS))

// The clock. lwIP hands the answer to this, and this firmware does not set a system clock
// with it: it tells the timebase, which is the only thing allowed to say what a reading may
// claim about its own timestamp.
#ifdef __cplusplus
extern "C" {
#endif
void sentry_sntp_set_time_us(uint32_t seconds, uint32_t microseconds);
#ifdef __cplusplus
}
#endif

#define SNTP_SERVER_DNS 1
#define SNTP_STARTUP_DELAY 0
// Asking once an hour is enough for a node whose readings are stamped from a monotonic
// counter between answers, and it is polite to whoever runs the pool.
#define SNTP_UPDATE_DELAY 3600000
#define SNTP_SET_SYSTEM_TIME_US(sec, us) sentry_sntp_set_time_us((uint32_t)(sec), (uint32_t)(us))

#endif  // SENTRY_LWIPOPTS_H
