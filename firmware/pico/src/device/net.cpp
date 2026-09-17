#include "net.h"

#include <cstdio>
#include <cstring>

#include "lwip/altcp.h"
#include "lwip/altcp_tls.h"
#include "lwip/apps/sntp.h"
#include "lwip/dns.h"
#include "lwip/netif.h"
#include "mbedtls/platform_time.h"
#include "mbedtls/ssl.h"
#include "pico/cyw43_arch.h"
#include "pico/stdlib.h"
#include "psa/crypto.h"
#include "sentry/mqtt.h"

namespace {

net::TimeAnswers answers;
bool radio_up = false;

// The certificate, the key and the authority, in the form mbedTLS keeps them. One copy
// for both lines: they are the same node proving the same thing to the same hub, and a
// second copy of a private key is a second thing to lose.
altcp_tls_config* trust = nullptr;
size_t trusting = 0;  // how many lines are holding it

// What has arrived and not yet been read. An MQTT packet is at most 2560 bytes here, and
// a command and an acknowledgement can be in the same read. The media line is written to
// rather than read from: all it ever receives is one short answer.
constexpr size_t kInboxBytes = 4096;
constexpr size_t kMediaInboxBytes = 256;

// One end of one connection. Everything the callbacks touch lives here, so that two lines
// are two of these rather than two copies of this file.
struct Link {
  altcp_pcb* connection = nullptr;
  net::Socket state = net::Socket::kIdle;
  const char* closed_because = "";
  uint16_t port = 0;
  char host[64] = {};
  uint8_t* inbox = nullptr;
  size_t room = 0;
  size_t size = 0;
  size_t taken = 0;
};

uint8_t broker_inbox[kInboxBytes];
uint8_t media_inbox[kMediaInboxBytes];
Link lines[2];

Link& line_of(net::Line which) { return lines[which == net::Line::kBroker ? 0 : 1]; }

void forget_connection(Link& link, const char* why) {
  link.closed_because = why;
  link.connection = nullptr;
  link.state = net::Socket::kClosed;
}

// Everything below runs in lwIP's context, not the loop's.
void on_error(void* argument, err_t error) {
  Link* link = static_cast<Link*>(argument);
  if (link == nullptr) return;
  // The pcb is already gone by the time this is called; saying so is all there is to do.
  switch (error) {
    case ERR_ABRT:
      forget_connection(*link, "the connection was aborted");
      break;
    case ERR_RST:
      forget_connection(*link, "the hub reset the connection");
      break;
    case ERR_CLSD:
      forget_connection(*link, "the hub closed the connection");
      break;
    default:
      forget_connection(*link, "the connection failed, which with TLS is usually the handshake");
      break;
  }
}

err_t on_connected(void* argument, altcp_pcb* pcb, err_t error) {
  (void)pcb;
  Link* link = static_cast<Link*>(argument);
  if (link == nullptr) return ERR_OK;
  if (error != ERR_OK) {
    forget_connection(*link, "the handshake did not complete");
    return ERR_OK;
  }
  link->state = net::Socket::kOpen;
  return ERR_OK;
}

err_t on_received(void* argument, altcp_pcb* pcb, pbuf* buffer, err_t error) {
  Link* link = static_cast<Link*>(argument);
  if (link == nullptr) {
    if (buffer != nullptr) pbuf_free(buffer);
    return ERR_OK;
  }
  if (buffer == nullptr) {
    // The other end said it was finished. Whatever is already in the inbox stays readable.
    forget_connection(*link, "the hub ended the connection");
    altcp_close(pcb);
    return ERR_OK;
  }
  if (error != ERR_OK) {
    pbuf_free(buffer);
    return error;
  }
  // Room is made by what has already been read, and never by dropping what has not: a
  // packet cut in half is a connection that cannot be resynchronised, so a full inbox
  // leaves the bytes with lwIP to offer again.
  if (link->taken > 0 && link->taken == link->size) {
    link->size = 0;
    link->taken = 0;
  }
  if (link->size + buffer->tot_len > link->room) {
    return ERR_MEM;
  }
  const u16_t copied = pbuf_copy_partial(buffer, link->inbox + link->size, buffer->tot_len, 0);
  link->size += copied;
  altcp_recved(pcb, copied);
  pbuf_free(buffer);
  return ERR_OK;
}

void on_resolved(const char* name, const ip_addr_t* found, void* argument);

// With an address in hand: make the TLS connection and start the handshake.
bool open_to(Link& link, const ip_addr_t* where) {
  link.connection = altcp_tls_new(trust, IPADDR_TYPE_V4);
  if (link.connection == nullptr) {
    forget_connection(link, "there was not enough memory for a TLS connection");
    return false;
  }
  // The name this node checks the certificate against. It is the name it dialled, which is
  // the only thing that makes the certificate mean anything.
  mbedtls_ssl_context* ssl = static_cast<mbedtls_ssl_context*>(altcp_tls_context(link.connection));
  if (ssl != nullptr) mbedtls_ssl_set_hostname(ssl, link.host);
  altcp_arg(link.connection, &link);
  altcp_err(link.connection, on_error);
  altcp_recv(link.connection, on_received);
  link.state = net::Socket::kConnecting;
  if (altcp_connect(link.connection, where, link.port, on_connected) != ERR_OK) {
    altcp_close(link.connection);
    forget_connection(link, "the connection could not be started");
    return false;
  }
  return true;
}

void on_resolved(const char* name, const ip_addr_t* found, void* argument) {
  (void)name;
  Link* link = static_cast<Link*>(argument);
  if (link == nullptr) return;
  if (found == nullptr) {
    forget_connection(*link, "the hub's name did not resolve");
    return;
  }
  open_to(*link, found);
}

}  // namespace

// The only clock mbedTLS is given, and it is not a clock: it is what the network last said
// the time was, moved on by the monotonic counter since. Before the first answer it is
// zero, which makes every certificate look not-yet-valid — a node that does not know the
// time cannot judge an expiry date, and pretending otherwise is how an expired certificate
// gets accepted at three in the morning.
extern "C" long long sentry_unix_seconds(long long* out) {
  long long seconds = 0;
  if (answers.count > 0) {
    const uint64_t since_us = time_us_64() - answers.taken_at_us;
    seconds = (answers.last_unix_ms + static_cast<int64_t>(since_us / 1000)) / 1000;
  }
  if (out != nullptr) *out = seconds;
  return seconds;
}

// What mbedTLS measures its own timeouts with: the counter on the chip, which only ever
// counts up, and not the time of day, which an SNTP answer can move.
extern "C" mbedtls_ms_time_t mbedtls_ms_time(void) {
  return static_cast<mbedtls_ms_time_t>(time_us_64() / 1000);
}

// Called by lwIP's SNTP client, from the background context, whenever the network answers.
// It records what was said; the timebase is told on the main loop's next turn, because the
// thing that decides what a reading may claim should not be written to from an interrupt.
extern "C" void sentry_sntp_set_time_us(uint32_t seconds, uint32_t microseconds) {
  answers.count += 1;
  answers.last_unix_ms =
      static_cast<int64_t>(seconds) * 1000 + static_cast<int64_t>(microseconds / 1000);
  answers.taken_at_us = time_us_64();
}

namespace net {

bool start(const char* hostname) {
  if (radio_up) return true;
  if (cyw43_arch_init() != 0) return false;
  cyw43_arch_enable_sta_mode();
  if (hostname != nullptr && hostname[0] != '\0') {
    netif_set_hostname(&cyw43_state.netif[CYW43_ITF_STA], hostname);
  }
  radio_up = true;
  return true;
}

Joined join(const char* ssid, const char* passphrase, uint32_t patience_ms) {
  if (!radio_up) return Joined::kNoRadio;
  const bool open = passphrase == nullptr || passphrase[0] == '\0';
  const int result = cyw43_arch_wifi_connect_timeout_ms(
      ssid, open ? nullptr : passphrase, open ? CYW43_AUTH_OPEN : CYW43_AUTH_WPA2_AES_PSK,
      patience_ms);
  if (result == 0) return Joined::kYes;
  // The driver tells apart "the network said no" from "nothing answered in time", and the
  // difference is the difference between a wrong passphrase and a board out of range.
  if (result == PICO_ERROR_BADAUTH) return Joined::kRefused;
  return Joined::kTimedOut;
}

bool linked() {
  if (!radio_up) return false;
  return cyw43_tcpip_link_status(&cyw43_state, CYW43_ITF_STA) == CYW43_LINK_UP;
}

bool address(char* out, size_t capacity) {
  if (!linked() || capacity < 16) return false;
  const ip4_addr_t* given = netif_ip4_addr(&cyw43_state.netif[CYW43_ITF_STA]);
  if (given == nullptr || ip4_addr_get_u32(given) == 0) return false;
  const char* text = ip4addr_ntoa(given);
  if (text == nullptr) return false;
  return std::snprintf(out, capacity, "%s", text) > 0;
}

int32_t signal_strength() {
  if (!linked()) return 0;
  int32_t rssi = 0;
  if (cyw43_wifi_get_rssi(&cyw43_state, &rssi) != 0) return 0;
  return rssi;
}

void ask_the_time(const char* server) {
  sntp_setoperatingmode(SNTP_OPMODE_POLL);
  sntp_setservername(0, server);
  sntp_init();
}

TimeAnswers time_answers() {
  // The background context writes this; reading it in one piece is worth the pause.
  cyw43_arch_lwip_begin();
  const TimeAnswers copy = answers;
  cyw43_arch_lwip_end();
  return copy;
}

void poll() {
  if (radio_up) cyw43_arch_poll();
}

Dialled dial(const char* host, uint16_t port, const sentry::Credentials& credentials,
             Line which) {
  if (!linked()) return Dialled::kNotLinked;
  // Not "connect without a certificate if there is none": there is no such connection to
  // make. A node that cannot prove who it is waits to be provisioned.
  if (!credentials.complete()) return Dialled::kNoCredentials;
  Link& link = line_of(which);
  if (link.connection != nullptr || link.state == Socket::kResolving ||
      link.state == Socket::kConnecting) {
    return Dialled::kBusy;
  }

  std::snprintf(link.host, sizeof(link.host), "%s", host);
  link.port = port;
  link.inbox = which == Line::kBroker ? broker_inbox : media_inbox;
  link.room = which == Line::kBroker ? kInboxBytes : kMediaInboxBytes;
  link.size = 0;
  link.taken = 0;
  link.closed_because = "";

  cyw43_arch_lwip_begin();
  // Two-way: the authority this node checks the other end against, and the certificate and
  // key it answers with. mbedTLS copies all three, which is why the key is handed over
  // here and nowhere else. Both lines share the one copy, and it is made once: building a
  // second would cost another copy of the key for no reason anybody could name.
  if (trust == nullptr) {
    // TLS 1.3 keeps its key schedule behind PSA, which wants its one-time setup done
    // before any handshake and does not do it itself. It is idempotent, and it is here
    // rather than at boot so that a board that never connects never pays for it.
    if (psa_crypto_init() != PSA_SUCCESS) {
      cyw43_arch_lwip_end();
      link.state = Socket::kIdle;
      return Dialled::kNoMemory;
    }
    trust = altcp_tls_create_config_client_2wayauth(
        reinterpret_cast<const u8_t*>(credentials.authority()),
        static_cast<size_t>(credentials.authority_size()),
        reinterpret_cast<const u8_t*>(credentials.private_key()),
        static_cast<size_t>(credentials.private_key_size()), nullptr, 0,
        reinterpret_cast<const u8_t*>(credentials.certificate()),
        static_cast<size_t>(credentials.certificate_size()));
    if (trust == nullptr) {
      cyw43_arch_lwip_end();
      link.state = Socket::kIdle;
      return Dialled::kNoMemory;
    }
  }
  ++trusting;

  ip_addr_t address_of_hub;
  const err_t looked_up = dns_gethostbyname(link.host, &address_of_hub, on_resolved, &link);
  bool started = true;
  if (looked_up == ERR_OK) {
    started = open_to(link, &address_of_hub);
  } else if (looked_up == ERR_INPROGRESS) {
    link.state = Socket::kResolving;
  } else {
    link.state = Socket::kIdle;
    started = false;
  }
  cyw43_arch_lwip_end();
  if (!started) {
    if (trusting > 0) --trusting;
    return looked_up == ERR_OK ? Dialled::kNoMemory : Dialled::kNoAddress;
  }
  return Dialled::kOpening;
}

Socket socket(Line which) {
  cyw43_arch_lwip_begin();
  const Socket now = line_of(which).state;
  cyw43_arch_lwip_end();
  return now;
}

const char* why_closed(Line which) { return line_of(which).closed_because; }

namespace {

// One TLS record is as much as a single write can carry. mbedTLS takes what fits and says
// how much it took, and the layer between it and lwIP treats a short write as impossible
// and stops the program rather than returning — so nothing here ever hands it more than a
// record will hold. Under 1.3 that is less than the buffer: the record also carries the
// real content type and padding to the next step of sixteen, and mbedTLS rounds its limit
// down to that step. An MQTT packet has to fit in one record, because it is offered whole
// or not at all; sound does not, because it is a stream and the hub reads it as one.
constexpr size_t kMostInOneWrite =
    (MBEDTLS_SSL_OUT_CONTENT_LEN / MBEDTLS_SSL_CID_TLS1_3_PADDING_GRANULARITY) *
        MBEDTLS_SSL_CID_TLS1_3_PADDING_GRANULARITY -
    1;
static_assert(sentry::mqtt::kMaxPacketBytes <= kMostInOneWrite,
              "an MQTT packet must fit in one TLS record, or it could never be sent whole");

// What the connection will take right now, bounded by `most`. Called with lwIP held.
size_t put(Link& link, const uint8_t* bytes, size_t most) {
  if (link.state != Socket::kOpen || link.connection == nullptr || most == 0) return 0;
  if (altcp_write(link.connection, bytes, static_cast<u16_t>(most), TCP_WRITE_FLAG_COPY) !=
      ERR_OK) {
    return 0;
  }
  if (altcp_output(link.connection) != ERR_OK) return 0;
  return most;
}

}  // namespace

size_t send(const uint8_t* bytes, size_t size, Line which) {
  if (bytes == nullptr || size == 0) return 0;
  cyw43_arch_lwip_begin();
  Link& link = line_of(which);
  size_t taken = 0;
  if (link.state == Socket::kOpen && link.connection != nullptr) {
    // Whole or not at all: half a packet is a connection that cannot be resynchronised.
    if (size <= kMostInOneWrite && size <= altcp_sndbuf(link.connection)) {
      taken = put(link, bytes, size);
    }
  }
  cyw43_arch_lwip_end();
  return taken;
}

size_t send_some(const uint8_t* bytes, size_t size, Line which) {
  if (bytes == nullptr || size == 0) return 0;
  cyw43_arch_lwip_begin();
  Link& link = line_of(which);
  size_t taken = 0;
  if (link.state == Socket::kOpen && link.connection != nullptr) {
    size_t room = altcp_sndbuf(link.connection);
    if (room > kMostInOneWrite) room = kMostInOneWrite;
    taken = put(link, bytes, size < room ? size : room);
  }
  cyw43_arch_lwip_end();
  return taken;
}

size_t receive(uint8_t* out, size_t capacity, Line which) {
  if (out == nullptr || capacity == 0) return 0;
  cyw43_arch_lwip_begin();
  Link& link = line_of(which);
  const size_t waiting = link.size - link.taken;
  const size_t handed = waiting < capacity ? waiting : capacity;
  if (handed > 0) {
    std::memcpy(out, link.inbox + link.taken, handed);
    link.taken += handed;
    if (link.taken == link.size) {
      link.size = 0;
      link.taken = 0;
    }
  }
  cyw43_arch_lwip_end();
  return handed;
}

void hang_up(Line which) {
  cyw43_arch_lwip_begin();
  Link& link = line_of(which);
  const bool was_holding = link.connection != nullptr || link.state != Socket::kIdle;
  if (link.connection != nullptr) {
    altcp_arg(link.connection, nullptr);
    altcp_recv(link.connection, nullptr);
    altcp_err(link.connection, nullptr);
    if (altcp_close(link.connection) != ERR_OK) altcp_abort(link.connection);
    link.connection = nullptr;
  }
  if (was_holding && trusting > 0) --trusting;
  if (trusting == 0 && trust != nullptr) {
    // This is what holds mbedTLS's copy of the private key. It goes when the last line
    // that was using it goes, and not while the other one is still proving who this is.
    altcp_tls_free_config(trust);
    trust = nullptr;
  }
  link.size = 0;
  link.taken = 0;
  link.state = Socket::kIdle;
  cyw43_arch_lwip_end();
}

const char* name_of(Dialled dialled) {
  switch (dialled) {
    case Dialled::kOpening:
      return "opening";
    case Dialled::kNotLinked:
      return "no network to reach the broker over";
    case Dialled::kNoCredentials:
      return "no certificate to connect with, so there is no connection to make";
    case Dialled::kNoAddress:
      return "the hub's name did not resolve";
    case Dialled::kNoMemory:
      return "not enough memory for a TLS connection";
    case Dialled::kBusy:
      return "that line is already open";
  }
  return "unknown";
}

const char* name_of(Joined joined) {
  switch (joined) {
    case Joined::kYes:
      return "joined";
    case Joined::kNoRadio:
      return "no radio";
    case Joined::kRefused:
      return "refused: the network did not take that passphrase";
    case Joined::kTimedOut:
      return "no answer: out of range, or that network is not here";
  }
  return "unknown";
}

}  // namespace net
