// The first program in this repository that runs on a board rather than a computer.
//
// It is deliberately small, and deliberately honest about what it does not have. There is
// no broker here yet: the point of this binary is to prove that the modules under
// src/protocol and src/core, which until now only ever ran on a host, behave the same on
// an RP2350 — a chip with different alignment rules and a real 12-bit converter attached to
// a real die — and, now, that the board can join a network and find out what time it is.
//
// It speaks over USB serial, one line at a time:
//
//   # ready ...            what this board is, printed once at boot
//   # ...                  anything else it wants to say
//   {"schema_version":1,…} an event, exactly as it would be published
//
// and it listens for:
//
//   provision <json>       the identity and the network, as the provisioning tool writes it
//   join                   join that network and start asking the time
//   status                 what it has: address, signal, clock
//   time <unix_ms>         what time it is, for a board with no network to ask
//   sample                 take a reading now instead of waiting for the next one
//
// Until something tells it the time — SNTP, or that command — it publishes nothing. A board
// with no clock could stamp its readings from the moment it booted and call that a
// timestamp; it would be a number nobody measured, and the hub decides what counts as live
// from exactly those.
//
// The provisioning record given here lives in RAM and is gone at the next boot. Writing it
// to flash is PICO-02, and it is the part that needs the erase and program calls plus the
// two slots `store.cpp` already describes.

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "hardware/adc.h"
#include "net.h"
#include "pico/rand.h"
#include "pico/stdlib.h"
#include "pico/unique_id.h"

#if defined(CYW43_WL_GPIO_LED_PIN)
#include "pico/cyw43_arch.h"
#endif

#include "sentry/event.h"
#include "sentry/identity.h"
#include "sentry/sensors.h"
#include "sentry/timebase.h"

namespace {

// The temperature sensor is the last ADC input on both chips, and it has to be switched on
// before it reads as anything but noise.
constexpr uint kTemperatureInput = 4;
constexpr uint32_t kIntervalMs = 2000;
constexpr uint32_t kJoinPatienceMs = 20000;
// Whoever runs the pool, rather than a vendor's own: a satellite that only ever reaches the
// hub still has to agree with it about what time it is.
constexpr const char* kTimeServer = "pool.ntp.org";

sentry::Ticks ticks;
sentry::Timebase clock_;
sentry::Provisioning provisioning;
bool provisioned = false;
uint32_t answers_seen = 0;
char node_id[sentry::kMaxNodeIdText] = {};
char boot_id[sentry::kUuidText] = {};
char connection_id[sentry::kUuidText] = {};
int64_t sequence = 0;

uint64_t now_us() { return ticks.extend(time_us_32()); }

// Where randomness on an RP2350 comes from: the SDK's own generator, seeded from the ring
// oscillator and the board's unique identifier. The header that formats a UUID has no
// opinion about this on purpose, so the answer lives here, where the hardware is.
void fill_entropy(uint8_t out[16]) {
  const uint64_t first = get_rand_64();
  const uint64_t second = get_rand_64();
  std::memcpy(out, &first, 8);
  std::memcpy(out + 8, &second, 8);
}

bool make_uuid(char* out) {
  uint8_t entropy[16];
  fill_entropy(entropy);
  return sentry::write_uuid4(entropy, out, sentry::kUuidText);
}

// A name the hub could actually have registered: the board's own serial number, which is
// different on every board and the same across every boot of this one. A provisioned node
// is called whatever the hub registered it as instead.
void name_this_board() {
  pico_unique_board_id_t id;
  pico_get_unique_board_id(&id);
  char* at = node_id;
  const char* end = node_id + sizeof(node_id) - 1;
  for (const char* letter = "pico-"; *letter && at < end; ++letter) *at++ = *letter;
  static const char kHex[] = "0123456789abcdef";
  for (size_t index = 0; index < PICO_UNIQUE_BOARD_ID_SIZE_BYTES && at + 1 < end; ++index) {
    *at++ = kHex[id.id[index] >> 4];
    *at++ = kHex[id.id[index] & 0x0f];
  }
  *at = '\0';
}

void led(bool on) {
#if defined(CYW43_WL_GPIO_LED_PIN)
  cyw43_arch_gpio_put(CYW43_WL_GPIO_LED_PIN, on);
#elif defined(PICO_DEFAULT_LED_PIN)
  gpio_put(PICO_DEFAULT_LED_PIN, on);
#else
  (void)on;
#endif
}

// One reading of the die temperature, written as the event the hub would receive.
void publish_temperature() {
  adc_select_input(kTemperatureInput);
  const sentry::Measured measured = sentry::board_temperature(adc_read());

  const uint64_t moment = now_us();
  int64_t unix_ms = 0;
  if (!clock_.unix_ms(moment, unix_ms)) {
    std::printf("# no time yet; join a network, or send: time <unix_ms>\n");
    return;
  }
  char stamped[32] = {};
  if (!sentry::write_timestamp(unix_ms, stamped, sizeof(stamped))) {
    std::printf("# the time this board was given is not one it may claim\n");
    return;
  }
  char event_id[sentry::kUuidText] = {};
  if (!make_uuid(event_id)) {
    std::printf("# no entropy for an event id\n");
    return;
  }

  sentry::Reading reading;
  reading.event_id = event_id;
  reading.node_id = node_id;
  reading.source_id = "board-temperature";
  reading.boot_id = boot_id;
  reading.sequence = sequence++;
  reading.kind = "board.temperature";
  reading.occurred_at = stamped;
  reading.clock = clock_.status_at(moment);
  reading.quality = measured.quality;
  reading.unit = "\xc2\xb0" "C";
  if (measured.has_value) reading.value = sentry::Value::of(measured.value, 2);

  sentry::Delivery delivery;
  delivery.connection_id = connection_id;
  delivery.hub_epoch = 0;  // nothing has granted this board anything yet
  delivery.queued_ms = 0;

  char buffer[sentry::kMaxEventBytes];
  const size_t size = sentry::write_event(reading, delivery, buffer, sizeof(buffer));
  if (size == 0) {
    std::printf("# refused to write that reading\n");
    return;
  }
  std::fwrite(buffer, 1, size, stdout);
  std::fputc('\n', stdout);
  std::fflush(stdout);
}

// Whatever the network last said the time was, handed to the timebase here rather than in
// the callback: what a reading may claim about itself is decided in one place, on this
// loop, and not from lwIP's context.
void take_the_time_if_it_arrived() {
  const net::TimeAnswers answers = net::time_answers();
  if (answers.count == answers_seen || answers.count == 0) return;
  answers_seen = answers.count;
  clock_.sync(answers.last_unix_ms, ticks.extend(static_cast<uint32_t>(answers.taken_at_us)));
  char stamped[32] = {};
  if (sentry::write_timestamp(answers.last_unix_ms, stamped, sizeof(stamped))) {
    std::printf("# the network says it is %s (answer %lu)\n", stamped,
                static_cast<unsigned long>(answers.count));
  }
}

void join_the_network() {
  if (!provisioned || !provisioning.has_wifi()) {
    std::printf("# no network in the provisioning record; send: provision <json>\n");
    return;
  }
  std::printf("# joining %s\n", provisioning.wifi_ssid);
  std::fflush(stdout);
  const net::Joined joined =
      net::join(provisioning.wifi_ssid, provisioning.wifi_password, kJoinPatienceMs);
  if (joined != net::Joined::kYes) {
    std::printf("# %s\n", net::name_of(joined));
    return;
  }
  char address[20] = {};
  net::address(address, sizeof(address));
  std::printf("# joined %s as %s, %ld dBm\n", provisioning.wifi_ssid, address,
              static_cast<long>(net::signal_strength()));
  net::ask_the_time(kTimeServer);
}

void say_status() {
  char address[20] = {};
  const bool has_address = net::address(address, sizeof(address));
  const net::TimeAnswers answers = net::time_answers();
  std::printf("# node=%s provisioned=%s link=%s address=%s signal=%ld clock=%s answers=%lu\n",
              node_id, provisioned ? "yes" : "no", net::linked() ? "up" : "down",
              has_address ? address : "none", static_cast<long>(net::signal_strength()),
              clock_.synced() ? "synced" : "unsynced", static_cast<unsigned long>(answers.count));
}

void take_provisioning(const char* document) {
  sentry::Provisioning found;
  if (!sentry::read_provisioning(document, std::strlen(document), found)) {
    std::printf("# that is not a provisioning record this firmware can use\n");
    return;
  }
  provisioning = found;
  provisioned = true;
  // Both are the same fixed array, and the record was checked before it got here.
  std::memcpy(node_id, found.node_id, sizeof(node_id));
  // The passphrase is never printed back: a serial log is a file like any other.
  std::printf("# provisioned as %s, broker %s:%u, network %s\n", provisioning.node_id,
              provisioning.mqtt_host, static_cast<unsigned>(provisioning.mqtt_port),
              provisioning.has_wifi() ? provisioning.wifi_ssid : "none");
}

// Whatever the host has sent, up to a newline. Returns false when there is no full line.
bool read_line(char* out, size_t capacity) {
  static char pending[512];
  static size_t held = 0;
  int letter;
  while ((letter = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT) {
    if (letter == '\r') continue;
    if (letter == '\n') {
      const size_t size = held < capacity - 1 ? held : capacity - 1;
      std::memcpy(out, pending, size);
      out[size] = '\0';
      held = 0;
      return true;
    }
    if (held + 1 < sizeof(pending)) pending[held++] = static_cast<char>(letter);
  }
  return false;
}

void obey(const char* line) {
  if (std::strncmp(line, "provision ", 10) == 0) {
    take_provisioning(line + 10);
    return;
  }
  if (std::strcmp(line, "join") == 0) {
    join_the_network();
    return;
  }
  if (std::strcmp(line, "status") == 0) {
    say_status();
    return;
  }
  if (std::strncmp(line, "time ", 5) == 0) {
    int64_t unix_ms = 0;
    if (std::sscanf(line + 5, "%lld", reinterpret_cast<long long*>(&unix_ms)) != 1) {
      std::printf("# time <unix_ms>\n");
      return;
    }
    clock_.sync(unix_ms, now_us());
    std::printf("# time taken: %lld\n", static_cast<long long>(unix_ms));
    return;
  }
  if (std::strcmp(line, "sample") == 0) {
    publish_temperature();
    return;
  }
  if (line[0] != '\0') std::printf("# not a command: %s\n", line);
}

}  // namespace

int main() {
  stdio_init_all();

  name_this_board();

  // The radio is brought up whether or not there is a network to join: it is also what
  // lights the LED on a W board, and bringing it up is the first proof that it answers.
  const bool wireless = net::start(node_id);
#if !defined(CYW43_WL_GPIO_LED_PIN) && defined(PICO_DEFAULT_LED_PIN)
  gpio_init(PICO_DEFAULT_LED_PIN);
  gpio_set_dir(PICO_DEFAULT_LED_PIN, GPIO_OUT);
#endif

  adc_init();
  adc_set_temp_sensor_enabled(true);

  if (!make_uuid(boot_id) || !make_uuid(connection_id)) {
    std::printf("# this board cannot make an identifier for this boot\n");
    return 1;
  }

  // The host opens the port after the board is already running, so nothing before this is
  // ever read. Saying it again on a timer would be noise; saying it once, after a moment,
  // is what a person watching a serial monitor needs.
  sleep_ms(2000);
  std::printf("# ready %s node=%s boot=%s connection=%s wireless=%s\n", PICO_BOARD, node_id,
              boot_id, connection_id, wireless ? "up" : "no");
  std::fflush(stdout);

  absolute_time_t next = make_timeout_time_ms(kIntervalMs);
  while (true) {
    net::poll();
    take_the_time_if_it_arrived();
    char line[512];
    if (read_line(line, sizeof(line))) obey(line);
    if (absolute_time_diff_us(get_absolute_time(), next) <= 0) {
      led(true);
      publish_temperature();
      sleep_ms(20);
      led(false);
      next = make_timeout_time_ms(kIntervalMs);
    }
    sleep_ms(5);
  }
}
