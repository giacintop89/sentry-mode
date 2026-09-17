#include "net.h"

#include <cstdio>
#include <cstring>

#include "lwip/altcp.h"
#include "lwip/altcp_tls.h"
#include "lwip/apps/sntp.h"
#include "lwip/dns.h"
#include "lwip/netif.h"
#include "mbedtls/platform_time.h"
#include "pico/cyw43_arch.h"
#include "pico/stdlib.h"

namespace {

net::TimeAnswers answers;
bool radio_up = false;

// One connection, so one of everything it needs. A board that could open two would need
// twice the TLS memory to prove it, and there is nothing here to say to two brokers.
altcp_pcb* connection = nullptr;
altcp_tls_config* trust = nullptr;
net::Socket state = net::Socket::kIdle;
const char* closed_because = "";
uint16_t broker_port = 0;
char broker_name[64] = {};

// What has arrived and not yet been read. An MQTT packet is at most 2560 bytes here, and
// a command and an acknowledgement can be in the same read.
constexpr size_t kInboxBytes = 4096;
uint8_t inbox[kInboxBytes];
size_t inbox_size = 0;
size_t inbox_taken = 0;

void forget_connection(const char* why) {
  closed_because = why;
  connection = nullptr;
  state = net::Socket::kClosed;
}

// Everything below runs in lwIP's context, not the loop's.
void on_error(void* argument, err_t error) {
  (void)argument;
  // The pcb is already gone by the time this is called; saying so is all there is to do.
  switch (error) {
    case ERR_ABRT:
      forget_connection("the connection was aborted");
      break;
    case ERR_RST:
      forget_connection("the broker reset the connection");
      break;
    case ERR_CLSD:
      forget_connection("the broker closed the connection");
      break;
    default:
      forget_connection("the connection failed, which with TLS is usually the handshake");
      break;
  }
}

err_t on_connected(void* argument, altcp_pcb* pcb, err_t error) {
  (void)argument;
  (void)pcb;
  if (error != ERR_OK) {
    forget_connection("the handshake did not complete");
    return ERR_OK;
  }
  state = net::Socket::kOpen;
  return ERR_OK;
}

err_t on_received(void* argument, altcp_pcb* pcb, pbuf* buffer, err_t error) {
  (void)argument;
  if (buffer == nullptr) {
    // The broker said it was finished. Whatever is already in the inbox stays readable.
    forget_connection("the broker ended the connection");
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
  if (inbox_taken > 0 && inbox_taken == inbox_size) {
    inbox_size = 0;
    inbox_taken = 0;
  }
  if (inbox_size + buffer->tot_len > kInboxBytes) {
    return ERR_MEM;
  }
  const u16_t copied = pbuf_copy_partial(buffer, inbox + inbox_size, buffer->tot_len, 0);
  inbox_size += copied;
  altcp_recved(pcb, copied);
  pbuf_free(buffer);
  return ERR_OK;
}

void on_resolved(const char* name, const ip_addr_t* found, void* argument);

// With an address in hand: make the TLS connection and start the handshake.
bool open_to(const ip_addr_t* where) {
  connection = altcp_tls_new(trust, IPADDR_TYPE_V4);
  if (connection == nullptr) {
    forget_connection("there was not enough memory for a TLS connection");
    return false;
  }
  // The name this node checks the broker's certificate against. It is the name it dialled,
  // which is the only thing that makes the certificate mean anything.
  mbedtls_ssl_context* ssl = static_cast<mbedtls_ssl_context*>(altcp_tls_context(connection));
  if (ssl != nullptr) mbedtls_ssl_set_hostname(ssl, broker_name);
  altcp_err(connection, on_error);
  altcp_recv(connection, on_received);
  state = net::Socket::kConnecting;
  if (altcp_connect(connection, where, broker_port, on_connected) != ERR_OK) {
    altcp_close(connection);
    forget_connection("the connection could not be started");
    return false;
  }
  return true;
}

void on_resolved(const char* name, const ip_addr_t* found, void* argument) {
  (void)name;
  (void)argument;
  if (found == nullptr) {
    forget_connection("the broker's name did not resolve");
    return;
  }
  open_to(found);
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

Dialled dial(const char* host, uint16_t port, const sentry::Credentials& credentials) {
  if (!linked()) return Dialled::kNotLinked;
  // Not "connect without a certificate if there is none": there is no such connection to
  // make. A node that cannot prove who it is waits to be provisioned.
  if (!credentials.complete()) return Dialled::kNoCredentials;
  if (connection != nullptr || state == Socket::kResolving || state == Socket::kConnecting) {
    return Dialled::kBusy;
  }

  std::snprintf(broker_name, sizeof(broker_name), "%s", host);
  broker_port = port;
  inbox_size = 0;
  inbox_taken = 0;
  closed_because = "";

  cyw43_arch_lwip_begin();
  if (trust != nullptr) {
    altcp_tls_free_config(trust);
    trust = nullptr;
  }
  // Two-way: the authority this node checks the broker against, and the certificate and
  // key it answers with. mbedTLS copies all three, which is why the key is handed over
  // here and nowhere else.
  trust = altcp_tls_create_config_client_2wayauth(
      reinterpret_cast<const u8_t*>(credentials.authority()),
      static_cast<size_t>(credentials.authority_size()),
      reinterpret_cast<const u8_t*>(credentials.private_key()),
      static_cast<size_t>(credentials.private_key_size()), nullptr, 0,
      reinterpret_cast<const u8_t*>(credentials.certificate()),
      static_cast<size_t>(credentials.certificate_size()));
  if (trust == nullptr) {
    cyw43_arch_lwip_end();
    state = Socket::kIdle;
    return Dialled::kNoMemory;
  }

  ip_addr_t address_of_broker;
  const err_t looked_up =
      dns_gethostbyname(broker_name, &address_of_broker, on_resolved, nullptr);
  bool started = true;
  if (looked_up == ERR_OK) {
    started = open_to(&address_of_broker);
  } else if (looked_up == ERR_INPROGRESS) {
    state = Socket::kResolving;
  } else {
    state = Socket::kIdle;
    started = false;
  }
  cyw43_arch_lwip_end();
  if (!started) return looked_up == ERR_OK ? Dialled::kNoMemory : Dialled::kNoAddress;
  return Dialled::kOpening;
}

Socket socket() {
  cyw43_arch_lwip_begin();
  const Socket now = state;
  cyw43_arch_lwip_end();
  return now;
}

const char* why_closed() { return closed_because; }

size_t send(const uint8_t* bytes, size_t size) {
  if (bytes == nullptr || size == 0) return 0;
  cyw43_arch_lwip_begin();
  size_t taken = 0;
  if (state == Socket::kOpen && connection != nullptr) {
    const size_t room = altcp_sndbuf(connection);
    taken = size <= room ? size : 0;  // a packet is sent whole or not at all
    if (taken > 0) {
      const err_t written =
          altcp_write(connection, bytes, static_cast<u16_t>(taken), TCP_WRITE_FLAG_COPY);
      if (written != ERR_OK) {
        taken = 0;
      } else if (altcp_output(connection) != ERR_OK) {
        taken = 0;
      }
    }
  }
  cyw43_arch_lwip_end();
  return taken;
}

size_t receive(uint8_t* out, size_t capacity) {
  if (out == nullptr || capacity == 0) return 0;
  cyw43_arch_lwip_begin();
  const size_t waiting = inbox_size - inbox_taken;
  const size_t handed = waiting < capacity ? waiting : capacity;
  if (handed > 0) {
    std::memcpy(out, inbox + inbox_taken, handed);
    inbox_taken += handed;
    if (inbox_taken == inbox_size) {
      inbox_size = 0;
      inbox_taken = 0;
    }
  }
  cyw43_arch_lwip_end();
  return handed;
}

void hang_up() {
  cyw43_arch_lwip_begin();
  if (connection != nullptr) {
    altcp_recv(connection, nullptr);
    altcp_err(connection, nullptr);
    if (altcp_close(connection) != ERR_OK) altcp_abort(connection);
    connection = nullptr;
  }
  if (trust != nullptr) {
    // This is what holds mbedTLS's copy of the private key.
    altcp_tls_free_config(trust);
    trust = nullptr;
  }
  inbox_size = 0;
  inbox_taken = 0;
  state = Socket::kIdle;
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
      return "the broker's name did not resolve";
    case Dialled::kNoMemory:
      return "not enough memory for a TLS connection";
    case Dialled::kBusy:
      return "a connection is already open";
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
