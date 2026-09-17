#include "net.h"

#include <cstdio>

#include "lwip/apps/sntp.h"
#include "lwip/dns.h"
#include "lwip/netif.h"
#include "pico/cyw43_arch.h"
#include "pico/stdlib.h"

namespace {

net::TimeAnswers answers;
bool radio_up = false;

}  // namespace

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
