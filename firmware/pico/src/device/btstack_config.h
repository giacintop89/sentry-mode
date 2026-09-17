// What of BTstack this firmware compiles in.
//
// This node listens and never talks: no pairing, no bonding, no connection, no GATT. So
// there is no security manager, no device database and nothing kept in flash — which also
// keeps BTstack away from the sectors at the end of the part, where this firmware's own
// vault lives. The numbers below are the smallest the stack will build with for a scanner.

#ifndef SENTRY_BTSTACK_CONFIG_H
#define SENTRY_BTSTACK_CONFIG_H

// A central that scans. Nothing here advertises: a satellite that announced itself over
// the air would be one more thing in the room for somebody else to watch — and nothing in
// this firmware ever calls gap_advertisements_enable.
//
// The peripheral half is compiled in all the same, because BTstack 1.6.2 does not build
// without it: the privacy code in `hci.c` reads a field that only exists under
// ENABLE_LE_PERIPHERAL. Compiled in and never used is the smaller of the two evils; the
// alternative is a patched copy of somebody else's stack.
#define ENABLE_LE_CENTRAL
#define ENABLE_LE_PERIPHERAL

// Errors only. The scanner sees every advertisement in the building, and an info log of
// that would be a serial line nobody can read and a loop that never catches up.
#define ENABLE_LOG_ERROR
// Nothing here dumps HCI traffic. The file that would is compiled anyway and refuses to
// build without this, so it is defined and never used.
#define ENABLE_PRINTF_HEXDUMP

#define HCI_OUTGOING_PRE_BUFFER_SIZE 4
// An advertisement is at most 31 bytes and an extended one 255. Nothing here carries data
// over a connection, so there is no reason for the buffer of a full ACL packet.
#define HCI_ACL_PAYLOAD_SIZE (255 + 4)
#define HCI_ACL_CHUNK_SIZE_ALIGNMENT 4

#define MAX_NR_HCI_CONNECTIONS 1
#define MAX_NR_WHITELIST_ENTRIES 1
#define MAX_NR_LE_DEVICE_DB_ENTRIES 1
#define MAX_NR_GATT_CLIENTS 1
#define MAX_NR_L2CAP_CHANNELS 1
#define MAX_NR_L2CAP_SERVICES 1
#define MAX_NR_SM_LOOKUP_ENTRIES 1
#define MAX_ATT_DB_SIZE 64

// The radio and the wireless chip share one bus to this board. Letting the controller fill
// it with more buffers than the host has asked for is how that bus overruns.
#define MAX_NR_CONTROLLER_ACL_BUFFERS 3
#define MAX_NR_CONTROLLER_SCO_PACKETS 3
#define ENABLE_HCI_CONTROLLER_TO_HOST_FLOW_CONTROL
#define HCI_HOST_ACL_PACKET_LEN 1024
#define HCI_HOST_ACL_PACKET_NUM 3
#define HCI_HOST_SCO_PACKET_LEN 120
#define HCI_HOST_SCO_PACKET_NUM 3

// Nothing is remembered between boots: this node never bonds, and no TLV instance is ever
// given to the stack. The one entry is what the files insist on being able to compile.
#define NVM_NUM_DEVICE_DB_ENTRIES 1
#define NVM_NUM_LINK_KEYS 1

#define HAVE_EMBEDDED_TIME_MS
#define HAVE_ASSERT

#endif  // SENTRY_BTSTACK_CONFIG_H
