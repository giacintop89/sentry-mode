// The first program in this repository that runs on a board rather than a computer.
//
// It is deliberately small, and deliberately honest about what it does not have. There is
// no network here and no broker: the point of this binary is to prove that the modules
// under src/protocol and src/core, which until now only ever ran on a host, produce the
// same bytes on an RP2350 — a chip with a different word size for `long`, different
// alignment rules and a real 12-bit converter attached to a real die.
//
// It speaks over USB serial, one line at a time:
//
//   # ready ...            what this board is, printed once at boot
//   # ...                  anything else it wants to say
//   {"schema_version":1,…} an event, exactly as it would be published
//
// and it listens for two commands:
//
//   time <unix_ms>         what time it is, because the board has no idea
//   sample                 take a reading now instead of waiting for the next one
//
// Until something tells it the time, it publishes nothing. A board with no clock could
// stamp its readings from the moment it booted and call that a timestamp; it would be a
// number nobody measured, and the hub decides what counts as live from those.

#include <cstdio>
#include <cstring>

#include "hardware/adc.h"
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

sentry::Ticks ticks;
sentry::Timebase clock_;
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
// different on every board and the same across every boot of this one.
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
    std::printf("# no time yet; send: time <unix_ms>\n");
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

// Whatever the host has sent, up to a newline. Returns false when there is no full line.
bool read_line(char* out, size_t capacity) {
  static char pending[64];
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

#if defined(CYW43_WL_GPIO_LED_PIN)
  // The light on a W board is on the wireless chip, so bringing it up is also the first
  // proof that the chip answers at all.
  const bool wireless = cyw43_arch_init() == 0;
#elif defined(PICO_DEFAULT_LED_PIN)
  gpio_init(PICO_DEFAULT_LED_PIN);
  gpio_set_dir(PICO_DEFAULT_LED_PIN, GPIO_OUT);
  const bool wireless = false;
#else
  const bool wireless = false;
#endif

  adc_init();
  adc_set_temp_sensor_enabled(true);

  name_this_board();
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
    char line[64];
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
