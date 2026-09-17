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
//   credentials <json>     the authority, the certificate and the key, for the handshake
//   connect                open the one connection this node makes, and keep it open
//   disconnect             close it and stop trying
//   keeping                what is in flash, by name and by sequence number, never by value
//   forget [what]          erase one of those, or all of them: the recovery verb
//   tear <what>            write half a record, on purpose, to see the next boot refuse it
//   hang                   stop feeding the watchdog, on purpose, to watch it fire
//
// Until something tells it the time — SNTP, or that command — it publishes nothing. A board
// with no clock could stamp its readings from the moment it booted and call that a
// timestamp; it would be a number nobody measured, and the hub decides what counts as live
// from exactly those.
//
// What it is given it keeps. The identity, the three parts of the handshake and the last
// configuration the hub sent are written to the flash at the end of the part, two slots
// each, and read back at the next boot — so a board that loses power comes back as itself,
// running what it was told to run, without a cable. Nothing of that is in the image: the
// UF2 is the same on every board, and `flash_vault.cpp` is the only file that writes.
//
// The boot id is the opposite, and deliberately: it is made fresh every time, so a hub
// that sees a sequence number start again at zero can tell a reboot from a replay.

#include <cstdio>
#include <malloc.h>
#include <cstdlib>
#include <cstring>

#include "flash_vault.h"
#include "hardware/adc.h"
#include "hardware/watchdog.h"
#include "net.h"
#include "pico/rand.h"
#include "pico/stdlib.h"
#include "pico/unique_id.h"

#if defined(CYW43_WL_GPIO_LED_PIN)
#include "pico/cyw43_arch.h"
#endif

#include "sentry/client.h"
#include "sentry/control.h"
#include "sentry/credentials.h"
#include "sentry/event.h"
#include "sentry/identity.h"
#include "sentry/sensors.h"
#include "sentry/lease.h"
#include "onewire.h"
#include "sentry/plan.h"
#include "sentry/spool.h"
#include "sentry/timebase.h"
#include "sentry/topics.h"
#include "sentry/vault.h"

namespace {

// The temperature sensor is the last ADC input on both chips, and it has to be switched on
// before it reads as anything but noise.
constexpr uint kTemperatureInput = 4;
constexpr uint32_t kHeartbeatBlinkMs = 2000;
// The same beat the Linux agent keeps by default, and what the hub's staleness is written
// against: a node that says nothing for three of these is a node to wonder about.
constexpr uint32_t kHeartbeatMs = 15000;
constexpr uint32_t kJoinPatienceMs = 20000;
// How long the loop may go round without saying anything before the chip resets itself.
// Everything in this program is measured in time rather than waited on, and the longest
// thing it ever does in one turn is a TLS handshake; eight seconds is what the hardware
// allows and several times what a turn takes.
constexpr uint32_t kWatchdogMs = 8000;
// Whoever runs the pool, rather than a vendor's own: a satellite that only ever reaches the
// hub still has to agree with it about what time it is.
constexpr const char* kTimeServer = "pool.ntp.org";

// Which board this is, which decides which pins belong to the radio rather than to a
// configuration. It is the build's answer, not a guess made at runtime.
#if defined(PICO_RP2350)
#if defined(CYW43_WL_GPIO_LED_PIN)
constexpr sentry::Board kThisBoard = sentry::Board::kPico2W;
#else
constexpr sentry::Board kThisBoard = sentry::Board::kPico2;
#endif
#else
#if defined(CYW43_WL_GPIO_LED_PIN)
constexpr sentry::Board kThisBoard = sentry::Board::kPicoW;
#else
constexpr sentry::Board kThisBoard = sentry::Board::kPico;
#endif
#endif

sentry::Ticks ticks;
sentry::Timebase clock_;
sentry::Provisioning provisioning;
sentry::Credentials credentials;
bool provisioned = false;
uint32_t answers_seen = 0;
// Whether the last reset was the watchdog's doing, read once before it is turned on again.
// A board that keeps coming back this way is a board with something wrong with it, and the
// hub cannot see the difference unless it is told.
bool woke_from_watchdog = false;
// A board that was provisioned, given its credentials and told what to run comes back
// without anybody at the cable: it joins, waits to be told the time, and connects. Nothing
// here decides to do that on its own — it is what it was doing when the power went off.
bool come_back_on_its_own = false;
char node_id[sentry::kMaxNodeIdText] = {};
char boot_id[sentry::kUuidText] = {};
char connection_id[sentry::kUuidText] = {};
int64_t sequence = 0;

// The broker, and everything one connection to it is made of. The topics and the will are
// built once a record is provisioned; the client reads them every time it opens a
// connection, which is why they are here and not on a stack somewhere.
char state_topic[sentry::kMaxTopicText] = {};
char events_topic[sentry::kMaxTopicText] = {};
char commands_topic[sentry::kMaxTopicText] = {};
char acks_topic[sentry::kMaxTopicText] = {};
char health_topic[sentry::kMaxTopicText] = {};
char goodbye[sentry::kMaxWillBytes] = {};
size_t goodbye_size = 0;
sentry::mqtt::Will will;
sentry::mqtt::Connect connect_record;
sentry::mqtt::Client broker(connect_record, commands_topic);

// The queue between a reading and a broker. Sixteen events, and what it gives up is
// counted rather than hidden.
sentry::Spool spool;

// The hub's permission to speak, and the commands already answered. Both outlive a
// connection: a grant runs out on its own clock rather than when a link drops, and a
// command answered before must not be acted on twice because the answer went missing.
sentry::Lease lease;
sentry::Answered answered;

// The answers this node owes. An ack is what the hub is waiting for, so it never queues
// behind readings; there are never many at once, and a fifth would be a hub that stopped
// listening rather than a node that fell behind.
constexpr size_t kMaxAckBytes = 512;
constexpr size_t kAckSlots = 4;
char acks[kAckSlots][kMaxAckBytes] = {};
size_t ack_sizes[kAckSlots] = {};
size_t first_ack = 0;
size_t owed_acks = 0;
uint32_t acks_lost = 0;

// One publish is in flight at a time, so one buffer holds it: the same bytes are sent
// again if no acknowledgement arrives, because a retransmission is the same event.
char outgoing[sentry::kMaxEventBytes] = {};
size_t outgoing_size = 0;
// What is in that buffer, which decides where it goes and whether the broker keeps it.
enum class Saying { kNothing, kAnnouncement, kAck, kEvent, kHealth, kFarewell };
Saying saying = Saying::kNothing;
// The hub said stop. What is owed is said first, and then this node goes quiet.
bool stopping = false;
// The sources this node is running, and the same thing half-built while a configuration
// is being judged. Two of them, so that a configuration refused leaves the running one
// alone: `Plan::take` rewrites whatever it is called on.
sentry::Plan running(kThisBoard);
sentry::Plan candidate(kThisBoard);
int64_t config_revision = -1;  // below zero: this node is not saying, because nobody told it

// What a planned source needs while it runs: a debounced view of a wire, and when the next
// periodic reading is due.
struct Live {
  sentry::DigitalInput input;
  uint32_t due_ms = 0;
  bool level = false;  // the last level read, for `status` to show
  int64_t readings = 0;
  // A 1-Wire probe takes three quarters of a second to measure anything. The loop starts
  // the conversion and comes back for it, rather than standing at the pin waiting.
  bool converting = false;
  uint32_t ready_ms = 0;
  bool owes_a_baseline = false;
};
Live live[sentry::kMaxPlanned];

// What the health message counts, which nothing else on this board does: readings the
// broker has acknowledged, and when the last report went out.
uint32_t published_events = 0;
uint32_t health_due_ms = 0;
// The last temperature this board measured, for the health message to report as the
// board's own rather than as a reading nobody asked for.
bool has_last_temperature = false;
double last_temperature = 0.0;

// Bytes that have arrived and are not yet a whole packet.
uint8_t incoming[sentry::mqtt::kMaxPacketBytes * 2] = {};
size_t incoming_size = 0;

bool wanted = false;             // somebody asked this node to be connected
uint32_t next_attempt_ms = 0;    // and the backoff says not before this
bool announced_this_connection = false;
// The first announcement of a connection is the one the session waits for; a second one,
// after a configuration, is an ordinary retained publish and must not be mistaken for it.
bool announcement_opens_the_connection = false;
// What this node runs has changed and the retained state still says otherwise.
bool owes_a_declaration = false;
// The client is told a connection opened once per connection. It stays offline until the
// broker's CONNACK, so "it has not said it is connected" is not the same question.
bool client_told = false;
// Connections that ended before this node was online, since the last one that worked. A
// handshake the broker refuses fails before the MQTT client is told anything at all, so
// the waiting between attempts is counted here rather than by the client: a board with a
// certificate the broker will not take must not spend the afternoon asking.
uint32_t attempts_since_online = 0;

uint64_t now_us() { return ticks.extend(time_us_32()); }

// What is left of the heap, in kilobytes. This program allocates nothing, but lwIP and
// mbedTLS do, and a node whose heap is being eaten by a connection that keeps failing has
// no other way of saying so before it stops. The arena is what the linker left between the
// end of the data and the bottom of the stack; what is taken is what malloc says it has
// handed out. Both are the SDK's own numbers, not an estimate.
extern "C" char __StackLimit;  // NOLINT: the linker's, and spelled the linker's way
extern "C" char __bss_end__;

int64_t free_heap_kb() {
  const struct mallinfo info = mallinfo();
  const ptrdiff_t arena = &__StackLimit - &__bss_end__;
  const ptrdiff_t taken = static_cast<ptrdiff_t>(info.uordblks);
  if (arena <= 0 || taken < 0 || taken > arena) return 0;
  return static_cast<int64_t>((arena - taken) / 1024);
}

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

// The time and the identifier every reading needs, or nothing and a reason. A board that
// cannot say when something happened does not publish that it happened.
bool moment_of(uint64_t& moment, char* stamped, size_t capacity, char* event_id) {
  moment = now_us();
  int64_t unix_ms = 0;
  if (!clock_.unix_ms(moment, unix_ms)) {
    std::printf("# no time yet; join a network, or send: time <unix_ms>\n");
    return false;
  }
  if (!sentry::write_timestamp(unix_ms, stamped, capacity)) {
    std::printf("# the time this board was given is not one it may claim\n");
    return false;
  }
  if (!make_uuid(event_id)) {
    std::printf("# no entropy for an event id\n");
    return false;
  }
  return true;
}

// One reading, written for a person to see on the cable and put in the queue for the
// broker. Whether the hub may have it is the lease's business and is decided elsewhere:
// what is taken is taken, and what is queued waits.
void offer_the_reading(const sentry::Reading& reading, bool initial, sentry::Kept kept) {
  sentry::Delivery delivery;
  delivery.connection_id = connection_id;
  delivery.hub_epoch = lease.hub_epoch();
  delivery.grant_id = lease.grant_id();
  delivery.queued_ms = 0;
  delivery.initial_state = initial;

  static char buffer[sentry::kMaxEventBytes];
  const size_t size = sentry::write_event(reading, delivery, buffer, sizeof(buffer));
  if (size == 0) {
    std::printf("# refused to write that reading\n");
    return;
  }
  std::fwrite(buffer, 1, size, stdout);
  std::fputc('\n', stdout);
  std::fflush(stdout);

  if (provisioned && !spool.offer(reading, kept, initial)) {
    std::printf("# the queue is full: %lu given up so far\n",
                static_cast<unsigned long>(spool.losses().refused));
  }
}

// One reading of the die temperature. A baseline is the same reading taken because a hub
// has just granted this node and needs to know where the source stands before it can tell
// news from the way things already were.
void take_a_board_reading(const sentry::Planned& source, Live& state, bool initial) {
  adc_select_input(kTemperatureInput);
  const sentry::Measured measured = sentry::board_temperature(adc_read());
  if (measured.has_value) {
    has_last_temperature = true;
    last_temperature = measured.value;
  }

  uint64_t moment = 0;
  char stamped[32] = {};
  char event_id[sentry::kUuidText] = {};
  if (!moment_of(moment, stamped, sizeof(stamped), event_id)) return;

  sentry::Reading reading;
  reading.event_id = event_id;
  reading.node_id = node_id;
  reading.source_id = source.source_id;
  reading.boot_id = boot_id;
  reading.sequence = sequence++;
  reading.kind = "board.temperature";
  reading.occurred_at = stamped;
  reading.clock = clock_.status_at(moment);
  reading.quality = measured.quality;
  reading.unit = "\xc2\xb0" "C";
  if (measured.has_value) reading.value = sentry::Value::of(measured.value, 2);
  ++state.readings;

  // A baseline is kept like a thing that happened once: nothing is coming to replace it,
  // and a temperature taken a minute later must not quietly stand in for it.
  offer_the_reading(reading, initial,
                    initial ? sentry::Kept::kTransition : sentry::Kept::kPeriodic);
}

// One reading from a pin with a converter behind it. What it is worth is decided in
// `sensors.cpp`: a count the converter could not have produced is not a measurement, and a
// fraction of full scale is labelled as a fraction and never as lux.
void take_an_adc_reading(const sentry::Planned& source, Live& state, bool initial) {
  adc_select_input(static_cast<uint>(source.adc.pin - sentry::kFirstAdcPin));
  const uint16_t raw = adc_read();
  const sentry::Measured measured =
      source.adc.volts ? sentry::adc_volts(raw) : sentry::relative_brightness(raw);

  uint64_t moment = 0;
  char stamped[32] = {};
  char event_id[sentry::kUuidText] = {};
  if (!moment_of(moment, stamped, sizeof(stamped), event_id)) return;

  sentry::Reading reading;
  reading.event_id = event_id;
  reading.node_id = node_id;
  reading.source_id = source.source_id;
  reading.boot_id = boot_id;
  reading.sequence = sequence++;
  reading.kind = source.adc.event_kind;
  reading.occurred_at = stamped;
  reading.clock = clock_.status_at(moment);
  reading.quality = measured.quality;
  reading.unit = source.adc.volts ? "V" : "ratio";
  if (measured.has_value) {
    reading.value = sentry::Value::of(measured.value, source.adc.volts ? 3 : 4);
  }
  ++state.readings;

  offer_the_reading(reading, initial,
                    initial ? sentry::Kept::kTransition : sentry::Kept::kPeriodic);
}

// One reading from a probe on a 1-Wire bus. The bus work is in `onewire.cpp` and what the
// nine bytes mean is in `sensors.cpp`; what is here is neither, and deliberately.
void take_a_onewire_reading(const sentry::Planned& source, Live& state,
                            const sentry::Measured& measured, bool initial) {
  uint64_t moment = 0;
  char stamped[32] = {};
  char event_id[sentry::kUuidText] = {};
  if (!moment_of(moment, stamped, sizeof(stamped), event_id)) return;

  sentry::Reading reading;
  reading.event_id = event_id;
  reading.node_id = node_id;
  reading.source_id = source.source_id;
  reading.boot_id = boot_id;
  reading.sequence = sequence++;
  reading.kind = source.onewire.event_kind;
  reading.occurred_at = stamped;
  reading.clock = clock_.status_at(moment);
  reading.quality = measured.quality;
  reading.unit = "\xc2\xb0" "C";
  if (measured.has_value) reading.value = sentry::Value::of(measured.value, 2);
  ++state.readings;

  offer_the_reading(reading, initial,
                    initial ? sentry::Kept::kTransition : sentry::Kept::kPeriodic);
}

// The probe's half of the loop: start a conversion, come back for it when it is done.
// Nothing here waits three quarters of a second, because the connection would be waiting
// with it.
void serve_a_onewire_probe(const sentry::Planned& source, Live& state, uint64_t now_ms) {
  const uint pin = static_cast<uint>(source.onewire.pin);
  const uint8_t* rom = source.onewire.has_rom ? source.onewire.rom : nullptr;
  if (state.converting) {
    if (static_cast<int32_t>(static_cast<uint32_t>(now_ms) - state.ready_ms) < 0) return;
    state.converting = false;
    uint8_t scratchpad[9] = {};
    onewire::read_the_scratchpad(pin, rom, scratchpad);
    take_a_onewire_reading(source, state, sentry::ds18b20_temperature(scratchpad),
                           state.owes_a_baseline);
    state.owes_a_baseline = false;
    return;
  }
  if (static_cast<int32_t>(static_cast<uint32_t>(now_ms) - state.due_ms) < 0) return;
  // Counted from the start of the conversion rather than from the reading, or every
  // interval would quietly be three quarters of a second longer than it says.
  state.due_ms = static_cast<uint32_t>(now_ms) + source.onewire.interval_ms;
  if (!onewire::start_a_conversion(pin, rom)) {
    // Nothing answered the reset. Said out loud, because a probe that has fallen off its
    // wire and a probe nobody asked about look identical from the hub.
    take_a_onewire_reading(source, state, sentry::Measured{}, state.owes_a_baseline);
    state.owes_a_baseline = false;
    return;
  }
  state.converting = true;
  state.ready_ms = static_cast<uint32_t>(now_ms) + onewire::kConversionMs;
}

// What a wire came to mean. The level is read here and judged in `input.cpp`, which is
// where the settling, the debounce and the polarity live — and which has never seen a pin.
void take_a_gpio_reading(const sentry::Planned& source, Live& state, sentry::Report report,
                         uint64_t now_ms) {
  uint64_t moment = 0;
  char stamped[32] = {};
  char event_id[sentry::kUuidText] = {};
  if (!moment_of(moment, stamped, sizeof(stamped), event_id)) return;

  sentry::Reading reading;
  reading.event_id = event_id;
  reading.node_id = node_id;
  reading.source_id = source.source_id;
  reading.boot_id = boot_id;
  reading.sequence = sequence++;
  reading.kind = source.gpio.event_kind;
  reading.occurred_at = stamped;
  reading.clock = clock_.status_at(moment);
  reading.quality = state.input.quality(now_ms);
  reading.value = sentry::Value::of(state.input.state());
  ++state.readings;

  // Both are things that happened once and neither may be dropped for a temperature: a
  // door that opened is not replaced by anything, and a baseline is what the hub is
  // waiting for before it believes the next one.
  offer_the_reading(reading, report == sentry::Report::kBaseline, sentry::Kept::kTransition);
}

// Every pin in the plan, read and judged. This runs on the same loop that keeps the
// connection alive, so it does no waiting of its own: the debounce is time, not sleep.
void read_the_sources() {
  const uint64_t now_ms = now_us() / 1000;
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    Live& state = live[index];
    if (source.driver == sentry::Driver::kGpio) {
      state.level = gpio_get(static_cast<uint>(source.gpio.pin));
      const sentry::Report report = state.input.sample(state.level, now_ms);
      if (report != sentry::Report::kNothing) {
        take_a_gpio_reading(source, state, report, now_ms);
      }
      continue;
    }
    if (source.driver == sentry::Driver::kBoard) {
      if (static_cast<int32_t>(static_cast<uint32_t>(now_ms) - state.due_ms) < 0) continue;
      state.due_ms = static_cast<uint32_t>(now_ms) + source.board.interval_ms;
      take_a_board_reading(source, state, false);
      continue;
    }
    if (source.driver == sentry::Driver::kAdc) {
      if (static_cast<int32_t>(static_cast<uint32_t>(now_ms) - state.due_ms) < 0) continue;
      state.due_ms = static_cast<uint32_t>(now_ms) + source.adc.interval_ms;
      take_an_adc_reading(source, state, false);
      continue;
    }
    if (source.driver == sentry::Driver::kOneWire) {
      serve_a_onewire_probe(source, state, now_ms);
    }
  }
}

// Where every source stands, taken because a hub has just granted this node or because
// what this node runs has just changed. Nothing here is news and all of it says so.
void take_every_baseline() {
  const uint64_t now_ms = now_us() / 1000;
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    Live& state = live[index];
    if (source.driver == sentry::Driver::kBoard) {
      take_a_board_reading(source, state, true);
      state.due_ms = static_cast<uint32_t>(now_ms) + source.board.interval_ms;
    } else if (source.driver == sentry::Driver::kAdc) {
      take_an_adc_reading(source, state, true);
      state.due_ms = static_cast<uint32_t>(now_ms) + source.adc.interval_ms;
    } else if (source.driver == sentry::Driver::kOneWire) {
      // Asked for rather than taken: the conversion is three quarters of a second away,
      // and the reading that comes back from it is the baseline.
      state.converting = false;
      state.owes_a_baseline = true;
      state.due_ms = static_cast<uint32_t>(now_ms);
    } else if (source.driver == sentry::Driver::kGpio) {
      // Rearmed rather than read: the next sample is the baseline, and a PIR that has not
      // settled yet will say so instead of pretending the room is empty.
      state.input.rearm(now_ms);
    }
  }
}

// Give up the pins the old plan held and take the ones the new plan asks for. Nothing is
// touched until a plan has been agreed to, which is what `plan.cpp` is for.
void start_the_plan() {
  const uint64_t now_ms = now_us() / 1000;
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    Live& state = live[index];
    state = Live{};
    if (source.driver == sentry::Driver::kGpio) {
      const uint pin = static_cast<uint>(source.gpio.pin);
      gpio_init(pin);
      gpio_set_dir(pin, GPIO_IN);
      gpio_set_pulls(pin, source.gpio.bias == sentry::Bias::kPullUp,
                     source.gpio.bias == sentry::Bias::kPullDown);
      state.input.configure(source.gpio.input);
      state.input.rearm(now_ms);
    } else if (source.driver == sentry::Driver::kAdc) {
      // The pad is given to the converter: no pulls, no digital input, nothing driving it.
      adc_gpio_init(static_cast<uint>(source.adc.pin));
      state.due_ms = static_cast<uint32_t>(now_ms);
    } else if (source.driver == sentry::Driver::kOneWire) {
      onewire::take(static_cast<uint>(source.onewire.pin));
      state.due_ms = static_cast<uint32_t>(now_ms);
    } else if (source.driver == sentry::Driver::kBoard) {
      state.due_ms = static_cast<uint32_t>(now_ms);
    }
  }
}

void stop_the_plan() {
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    if (source.driver == sentry::Driver::kGpio) {
      const uint pin = static_cast<uint>(source.gpio.pin);
      gpio_set_pulls(pin, false, false);
      gpio_deinit(pin);
    } else if (source.driver == sentry::Driver::kAdc) {
      gpio_deinit(static_cast<uint>(source.adc.pin));
    } else if (source.driver == sentry::Driver::kOneWire) {
      onewire::give_back(static_cast<uint>(source.onewire.pin));
    }
  }
}

// What this board runs when nobody has told it anything: the one thing it has without a
// wire on it. It is built as a configuration and goes through the same door one from the
// hub goes through, so there is no second way into the plan and nothing to keep in step.
void run_the_default_plan() {
  sentry::Source source;
  std::strncpy(source.id, "board-temperature", sizeof(source.id) - 1);
  std::strncpy(source.kind, "board", sizeof(source.kind) - 1);
  sentry::Unplanned why = sentry::Unplanned::kNone;
  char detail[sentry::kMaxPlanDetail] = {};
  if (!running.take(&source, 1, why, detail, sizeof(detail))) {
    std::printf("# this board could not plan its own temperature: %s\n", detail);
    return;
  }
  start_the_plan();
}

uint32_t monotonic_ms() { return static_cast<uint32_t>(now_us() / 1000); }

// Whatever the network last said the time was, handed to the timebase here rather than in
// the callback: what a reading may claim about itself is decided in one place, on this
// loop, and not from lwIP's context.
void take_the_time_if_it_arrived() {
  const net::TimeAnswers answers = net::time_answers();
  if (answers.count == answers_seen || answers.count == 0) return;
  answers_seen = answers.count;
  clock_.sync(answers.last_unix_ms, ticks.extend(static_cast<uint32_t>(answers.taken_at_us)));
  if (come_back_on_its_own && !wanted) {
    // Not before now: a certificate has dates on it, and a board that does not know what
    // time it is cannot tell an expired one from a good one.
    come_back_on_its_own = false;
    wanted = true;
    attempts_since_online = 0;
    next_attempt_ms = monotonic_ms();
    std::printf("# it knows the time and has what it needs: connecting on its own\n");
  }
  char stamped[32] = {};
  if (sentry::write_timestamp(answers.last_unix_ms, stamped, sizeof(stamped))) {
    std::printf("# the network says it is %s (answer %lu)\n", stamped,
                static_cast<unsigned long>(answers.count));
  }
}


// A second, then two, up to a minute, plus a little of this board's own randomness so that
// a houseful of them coming back after a power cut does not arrive in step.
uint32_t wait_before_trying_again() {
  if (attempts_since_online == 0) return 0;
  uint32_t wait = 1000;
  for (uint32_t attempt = 1; attempt < attempts_since_online && wait < 60000; ++attempt) {
    wait *= 2;
  }
  if (wait > 60000) wait = 60000;
  return wait + (get_rand_32() % 500);
}

// The three topics this node uses and the goodbye the broker holds on its behalf. Built
// when a record arrives, because every one of them is made out of the node's own name.
bool prepare_the_connection() {
  if (!provisioned) return false;
  if (!sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kState, state_topic,
                     sizeof(state_topic)) ||
      !sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kEvents, events_topic,
                     sizeof(events_topic)) ||
      !sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kCommands, commands_topic,
                     sizeof(commands_topic)) ||
      !sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kAcks, acks_topic,
                     sizeof(acks_topic)) ||
      !sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kHealth, health_topic,
                     sizeof(health_topic))) {
    return false;
  }
  const size_t size = sentry::write_goodbye(node_id, boot_id, connection_id,
                                            std::strlen(state_topic), goodbye, sizeof(goodbye));
  if (size == 0) return false;
  goodbye_size = size;
  will.topic = state_topic;
  will.payload = reinterpret_cast<const uint8_t*>(goodbye);
  will.size = size;
  connect_record.client_id = node_id;
  connect_record.keepalive_seconds = 30;
  connect_record.will = &will;
  return true;
}

// The retained state that says this node is here. It is the one thing published before the
// node is online, because it is what makes it online.
bool write_announcement() {
  // Every source in the running plan, with the options it is actually running with rather
  // than the ones that were asked for: a hub reading this back sees what this board did
  // with what it was told, which is the only version of it that matters.
  static sentry::DeclaredOption options[sentry::kMaxPlanned][sentry::kMaxOptionsPerSource];
  static sentry::DeclaredSource declared[sentry::kMaxPlanned];
  const size_t count = running.size();
  for (size_t index = 0; index < count; ++index) {
    const sentry::Planned& source = running.at(index);
    size_t at = 0;
    if (source.driver == sentry::Driver::kBoard) {
      options[index][at++] = {"measure", sentry::Value::of(source.board.measure)};
      options[index][at++] = {
          "interval_seconds",
          sentry::Value::of(static_cast<double>(source.board.interval_ms) / 1000.0, 1)};
    } else if (source.driver == sentry::Driver::kAdc) {
      options[index][at++] = {"pin", sentry::Value::of(static_cast<int64_t>(source.adc.pin))};
      options[index][at++] = {"output", sentry::Value::of(source.adc.volts ? "volts" : "ratio")};
      options[index][at++] = {
          "interval_seconds",
          sentry::Value::of(static_cast<double>(source.adc.interval_ms) / 1000.0, 1)};
      options[index][at++] = {"event_kind", sentry::Value::of(source.adc.event_kind)};
    } else if (source.driver == sentry::Driver::kOneWire) {
      options[index][at++] = {"pin",
                              sentry::Value::of(static_cast<int64_t>(source.onewire.pin))};
      if (source.onewire.has_rom) {
        options[index][at++] = {"device", sentry::Value::of(source.onewire.device)};
      }
      options[index][at++] = {
          "interval_seconds",
          sentry::Value::of(static_cast<double>(source.onewire.interval_ms) / 1000.0, 1)};
      options[index][at++] = {"event_kind", sentry::Value::of(source.onewire.event_kind)};
    } else if (source.driver == sentry::Driver::kGpio) {
      options[index][at++] = {"pin", sentry::Value::of(static_cast<int64_t>(source.gpio.pin))};
      options[index][at++] = {"active_high", sentry::Value::of(source.gpio.input.active_high)};
      options[index][at++] = {"bias", sentry::Value::of(sentry::name_of(source.gpio.bias))};
      options[index][at++] = {
          "debounce_ms", sentry::Value::of(static_cast<int64_t>(source.gpio.input.debounce_ms))};
      options[index][at++] = {
          "settle_seconds",
          sentry::Value::of(static_cast<double>(source.gpio.input.settle_ms) / 1000.0, 1)};
      options[index][at++] = {"event_kind", sentry::Value::of(source.gpio.event_kind)};
    }
    declared[index].source_id = source.source_id;
    declared[index].kind = sentry::name_of(source.driver);
    declared[index].enabled = source.enabled;
    declared[index].options = options[index];
    declared[index].option_count = at;
  }

  sentry::State here;
  here.node_id = node_id;
  here.boot_id = boot_id;
  here.connection_id = connection_id;
  here.online = true;
  here.firmware_version = "pico-0.1.0";
  here.profile = "sensor-presence";
  // Below zero until a configuration has been applied: a board running what it boots with
  // has no revision to claim, and claiming one would be claiming somebody sent it.
  here.config_revision = config_revision;
  here.sources = declared;
  here.source_count = count;
  outgoing_size = sentry::write_state(here, outgoing, sizeof(outgoing));
  saying = Saying::kAnnouncement;
  // Only the first one of a connection is what makes this node online; a later one is the
  // same retained state saying something else is running now.
  announcement_opens_the_connection = !announced_this_connection;
  return outgoing_size > 0;
}

// The answer at the front of the queue of answers. It stays in that queue until the broker
// acknowledges it, for the same reason a reading does.
bool write_the_first_ack() {
  if (owed_acks == 0) return false;
  const size_t size = ack_sizes[first_ack];
  if (size == 0 || size > sizeof(outgoing)) return false;
  std::memcpy(outgoing, acks[first_ack], size);
  outgoing_size = size;
  saying = Saying::kAck;
  return true;
}

void forget_the_first_ack() {
  if (owed_acks == 0) return;
  ack_sizes[first_ack] = 0;
  first_ack = (first_ack + 1) % kAckSlots;
  --owed_acks;
}

// The same words the will carries, said while there is still a connection to say them on:
// a node told to stop is not a node that crashed, and the retained state should not read
// as though it were.
bool write_the_farewell() {
  const size_t size = goodbye_size;
  if (size == 0 || size > sizeof(outgoing)) return false;
  std::memcpy(outgoing, goodbye, size);
  outgoing_size = size;
  saying = Saying::kFarewell;
  return true;
}

// How this node is doing, which is not a reading and does not wait for a grant: a hub
// that has granted nothing still needs to know the node is there and what its queue is
// doing. Anything this board cannot measure is left out rather than sent as a zero.
bool write_a_health_report() {
  const uint64_t moment = now_us();
  const sentry::Losses losses = spool.losses();

  // One entry per source this node is running, counted by that source rather than by the
  // node: a board with a pin that has never changed and a temperature every thirty seconds
  // is a different board from one where both are silent, and one number cannot say which.
  static sentry::SourceHealth sources[sentry::kMaxPlanned];
  for (size_t index = 0; index < running.size(); ++index) {
    sources[index].source_id = running.at(index).source_id;
    sources[index].readings = live[index].readings;
    sources[index].driver = sentry::name_of(running.at(index).driver);
    sources[index].error = nullptr;
  }

  sentry::Health health;
  health.node_id = node_id;
  health.boot_id = boot_id;
  health.uptime_seconds = static_cast<double>(moment) / 1000000.0;
  health.clock = clock_.status_at(moment);
  health.queue.events = static_cast<int64_t>(spool.size());
  health.queue.bytes = static_cast<int64_t>(spool.bytes());
  health.queue.published = published_events;
  health.queue.refused = losses.refused;
  // A periodic reading replaced by a newer one from the same source is a reading nobody
  // will ever see, which is what a drop is. The contract has no separate word for it, and
  // leaving it out would show a node losing nothing while it quietly lost twenty.
  health.queue.drops_count = losses.dropped_transitions + losses.coalesced;
  health.queue.drops_bytes = 0;  // this queue is bounded by events, not by bytes
  health.queue.drops_age = 0;    // and nothing in it is dropped for being old
  health.queue.drops_total = health.queue.drops_count;
  health.queue.granted = lease.live(moment / 1000);
  health.board.has_uptime = true;
  health.board.uptime_seconds = health.uptime_seconds;
  health.board.has_temperature = has_last_temperature;
  health.board.temperature_c = last_temperature;
  health.board.has_free_heap = true;
  health.board.memory_available_kb = free_heap_kb();
  health.sources = sources;
  health.source_count = running.size();

  outgoing_size = sentry::write_health(health, outgoing, sizeof(outgoing));
  saying = Saying::kHealth;
  return outgoing_size > 0;
}

// The oldest reading still waiting, written once and kept until the broker acknowledges
// it: a retransmission is the same bytes and the same event id, not a second reading.
bool write_the_front_of_the_queue() {
  sentry::Reading reading;
  bool replayed = false;
  bool initial = false;
  if (!spool.front(reading, replayed, &initial)) return false;
  sentry::Delivery delivery;
  delivery.connection_id = connection_id;
  delivery.hub_epoch = lease.hub_epoch();
  delivery.grant_id = lease.grant_id();
  delivery.replayed = replayed;
  delivery.initial_state = initial;
  outgoing_size = sentry::write_event(reading, delivery, outgoing, sizeof(outgoing));
  saying = Saying::kEvent;
  return outgoing_size > 0;
}

// Where what is in the buffer belongs, and whether the broker should keep it for whoever
// subscribes next. Only what a node is — here and gone — is kept; answers and readings
// are addressed to a hub that is listening now.
const char* where_it_goes(Saying what) {
  switch (what) {
    case Saying::kAnnouncement:
    case Saying::kFarewell:
      return state_topic;
    case Saying::kAck:
      return acks_topic;
    case Saying::kHealth:
      return health_topic;
    case Saying::kEvent:
    case Saying::kNothing:
      break;
  }
  return events_topic;
}

bool is_retained(Saying what) {
  return what == Saying::kAnnouncement || what == Saying::kFarewell;
}

void start_a_connection() {
  if (!make_uuid(connection_id) || !prepare_the_connection()) {
    std::printf("# this node cannot describe a connection of its own\n");
    wanted = false;
    return;
  }
  const net::Dialled dialled =
      net::dial(provisioning.mqtt_host, provisioning.mqtt_port, credentials);
  if (dialled != net::Dialled::kOpening) {
    ++attempts_since_online;
    std::printf("# %s\n", net::name_of(dialled));
    next_attempt_ms = monotonic_ms() + wait_before_trying_again();
    return;
  }
  client_told = false;
  // The first thing after the announcement, rather than fifteen seconds of nothing: a hub
  // that has just seen a node appear is the hub most in need of knowing how it is.
  health_due_ms = monotonic_ms();
  std::printf("# connecting to %s:%u\n", provisioning.mqtt_host,
              static_cast<unsigned>(provisioning.mqtt_port));
}

void end_the_connection(const char* why) {
  if (broker.link() != sentry::Link::kOnline) ++attempts_since_online;
  const uint32_t wait = wait_before_trying_again();
  if (why != nullptr && why[0] != '\0') {
    if (wanted) {
      std::printf("# %s (trying again in %lums)\n", why, static_cast<unsigned long>(wait));
    } else {
      std::printf("# %s\n", why);
    }
  }
  net::hang_up();
  broker.closed();
  incoming_size = 0;
  outgoing_size = 0;
  saying = Saying::kNothing;
  stopping = false;
  announced_this_connection = false;
  client_told = false;
  // Whatever was in flight was never acknowledged, so it stays at the front of the queue
  // and goes again on the next connection, marked as arriving late.
  spool.link_lost();
  next_attempt_ms = monotonic_ms() + wait;
}

// One answer, written now and sent when there is a connection to send it on. Nothing is
// thrown out to make room: the oldest answer is the one the hub has waited longest for.
void queue_an_ack(const sentry::Command& command, sentry::Answer answer, const char* detail) {
  sentry::Outcome outcome = sentry::Outcome::kReceived;
  if (answer == sentry::Answer::kApplied) outcome = sentry::Outcome::kApplied;
  if (answer == sentry::Answer::kFailed) outcome = sentry::Outcome::kFailed;
  if (owed_acks == kAckSlots) {
    ++acks_lost;
    std::printf("# no room to answer %s\n", command.command_id);
    return;
  }
  const size_t slot = (first_ack + owed_acks) % kAckSlots;
  const size_t size =
      sentry::write_ack(command.command_id, node_id, outcome, detail, acks[slot], kMaxAckBytes);
  if (size == 0) {
    ++acks_lost;
    std::printf("# an answer to %s could not be written\n", command.command_id);
    return;
  }
  ack_sizes[slot] = size;
  ++owed_acks;
  std::printf("# %s %s: %s%s%s\n", sentry::name_of(command.action), command.command_id,
              sentry::name_of(outcome), detail != nullptr ? ", " : "",
              detail != nullptr ? detail : "");
}

// What this board does not have, said as itself rather than as a lease refusal. The lease
// answers for grant, renew, revoke and stop, and `configure` is answered by the plan;
// what is left is hardware this node was not built with.
const char* beyond_this_firmware(sentry::Action action) {
  switch (action) {
    case sentry::Action::kVideoStart:
    case sentry::Action::kVideoRenew:
    case sentry::Action::kVideoStop:
      return "this node has no camera";
    case sentry::Action::kAudioStart:
    case sentry::Action::kAudioRenew:
    case sentry::Action::kAudioStop:
      return "this node has no microphone";
    default:
      return nullptr;
  }
}

// A configuration, judged whole on a copy and then adopted whole. Nothing is touched until
// `Plan::take` has agreed to all of it, so a refused configuration leaves this board
// running exactly what it was running, and says which source it could not have.
bool take_a_configuration(const sentry::Command& command, const char*& detail,
                          const char* as_it_arrived, size_t arrived_size) {
  static char why_not[sentry::kMaxPlanDetail];
  sentry::Unplanned why = sentry::Unplanned::kNone;
  if (!candidate.take(command.sources, command.source_count, why, why_not, sizeof(why_not))) {
    detail = why_not[0] != '\0' ? why_not : sentry::name_of(why);
    return false;
  }
  // The pins the old plan held are given back before the new plan asks for any, or a pin
  // moving from one source to another would be claimed by a source that already let it go.
  stop_the_plan();
  running = candidate;
  candidate.clear();
  start_the_plan();
  config_revision = command.revision;
  if (as_it_arrived != nullptr) {
    // Kept as it arrived, rather than as a plan written back out. What comes off the flash
    // at the next boot then goes through the same parser and the same `Plan::take` the hub
    // is answered from, so there is no second reading of a configuration to keep in step.
    if (!vault::save(sentry::Held::kConfiguration, as_it_arrived, arrived_size)) {
      std::printf("# this configuration is running but could not be kept for the next boot\n");
    }
    // A hub that has just been told what this node runs does not know where any of it
    // stands, and the retained state still describes what was running a moment ago.
    take_every_baseline();
    owes_a_declaration = true;
  }
  detail = nullptr;
  return true;
}

// One command, from the hub or from the cable. Everything it changes is changed here, and
// every one of them is answered.
void obey_a_command(const sentry::Command& command, const char* as_it_arrived,
                    size_t arrived_size) {
  const sentry::Answer* before = answered.recall(command.command_id);
  if (before != nullptr) {
    // The same command twice is answered the same way and acted on once. A hub whose ack
    // went missing may ask again; it may not extend a lease by asking again.
    queue_an_ack(command, *before, "already handled");
    return;
  }
  const char* missing = beyond_this_firmware(command.action);
  if (missing != nullptr) {
    answered.remember(command.command_id, sentry::Answer::kFailed);
    queue_an_ack(command, sentry::Answer::kFailed, missing);
    return;
  }
  if (command.action == sentry::Action::kConfigure) {
    const char* detail = nullptr;
    const bool taken = take_a_configuration(command, detail, as_it_arrived, arrived_size);
    const sentry::Answer answer = taken ? sentry::Answer::kApplied : sentry::Answer::kFailed;
    answered.remember(command.command_id, answer);
    queue_an_ack(command, answer, detail);
    return;
  }
  const sentry::Decision decision = lease.apply(command, now_us() / 1000);
  answered.remember(command.command_id, decision.answer);
  queue_an_ack(command, decision.answer, decision.detail);
  if (decision.stop_requested) {
    // Said, then gone: the answer and the retained state go out on this connection, and
    // nothing asks for another one until somebody at the cable does.
    stopping = true;
    wanted = false;
  }
  if (decision.newly_granted) {
    // A hub that has just granted this node does not know where any of its sources
    // stand. All of them say so at once, before whatever happens next is a change.
    take_every_baseline();
  }
}

// One command as it arrived from the broker.
void a_command_arrived(const sentry::mqtt::Incoming& packet) {
  sentry::Command command;
  sentry::Refusal why = sentry::Refusal::kNone;
  if (!sentry::parse_command(reinterpret_cast<const char*>(packet.payload), packet.payload_size,
                             node_id, command, why)) {
    // Not answered, and deliberately: a command this node cannot read has no id it can
    // answer about, and answering about one it guessed would tell the hub something that
    // did not happen.
    std::printf("# a command arrived that this node will not act on: %s\n", sentry::name_of(why));
    return;
  }
  obey_a_command(command, reinterpret_cast<const char*>(packet.payload), packet.payload_size);
}

// Everything the connection has to say, and everything this node has to say back.
void serve_the_broker() {
  const net::Socket socket = net::socket();
  if (socket == net::Socket::kClosed) {
    end_the_connection(net::why_closed());
    return;
  }
  if (socket != net::Socket::kOpen) return;

  const uint32_t now = monotonic_ms();
  if (!client_told) {
    if (!broker.opened(now, connection_id)) {
      end_the_connection("this connection could not be started as one");
      return;
    }
    client_told = true;
  }

  // What arrived, a packet at a time. Anything left over is the front of one that has not
  // finished arriving, and is kept.
  size_t room = sizeof(incoming) - incoming_size;
  if (room > 0) incoming_size += net::receive(incoming + incoming_size, room);
  while (incoming_size > 0) {
    sentry::mqtt::Incoming packet;
    sentry::mqtt::Effect effect = sentry::mqtt::Effect::kNothing;
    size_t used = 0;
    const sentry::mqtt::Refusal answer =
        broker.take(incoming, incoming_size, packet, used, effect);
    if (answer == sentry::mqtt::Refusal::kIncomplete) break;
    if (answer != sentry::mqtt::Refusal::kNone) {
      end_the_connection("the broker said something this node cannot read");
      return;
    }
    incoming_size -= used;
    if (incoming_size > 0) std::memmove(incoming, incoming + used, incoming_size);
    switch (effect) {
      case sentry::mqtt::Effect::kConnected:
        std::printf("# the broker accepted this node\n");
        break;
      case sentry::mqtt::Effect::kListening:
        std::printf("# listening on %s\n", commands_topic);
        break;
      case sentry::mqtt::Effect::kAnnounced:
        std::printf("# online, as %s\n", node_id);
        outgoing_size = 0;
        saying = Saying::kNothing;
        attempts_since_online = 0;
        break;
      case sentry::mqtt::Effect::kDelivered:
        // The broker has it. Only now does whatever was waiting let go of it.
        if (saying == Saying::kAck) forget_the_first_ack();
        if (saying == Saying::kEvent) {
          spool.accepted();
          ++published_events;
        }
        outgoing_size = 0;
        if (saying == Saying::kFarewell) {
          // The hub has the last word this node had to say. What follows is a DISCONNECT,
          // so the broker knows this was meant and keeps the will to itself.
          broker.leave();
        }
        saying = Saying::kNothing;
        break;
      case sentry::mqtt::Effect::kRefused:
        end_the_connection("the broker refused this connection");
        return;
      case sentry::mqtt::Effect::kNothing:
        if (packet.type == sentry::mqtt::Type::kPublish) a_command_arrived(packet);
        break;
    }
  }

  // What to say, and in what order. The announcement comes first because nothing else may
  // go before it; then what the hub is waiting for; then, if this node was told to stop,
  // the last thing it has to say; and only then a reading, and only if it is allowed to.
  if (outgoing_size == 0 && broker.link() != sentry::Link::kOffline) {
    if (broker.session().ready_to_announce() && !announced_this_connection) {
      if (write_announcement()) announced_this_connection = true;
    } else if (broker.session().may_publish_events()) {
      if (owed_acks > 0) {
        write_the_first_ack();
      } else if (owes_a_declaration) {
        // What this node runs has changed; the retained state is what says so, and it is
        // said before the readings that only make sense once it has been.
        if (write_announcement()) owes_a_declaration = false;
      } else if (stopping) {
        // Nothing after the farewell, whatever the lease still says: a node that was told
        // to stop and went on reporting would be a node that was not told.
        if (!broker.leaving()) write_the_farewell();
      } else if (static_cast<int32_t>(now - health_due_ms) >= 0) {
        health_due_ms = now + kHeartbeatMs;
        if (!write_a_health_report()) {
          saying = Saying::kNothing;
          std::printf("# this node could not write a health message about itself\n");
        }
      } else if (lease.live(now_us() / 1000)) {
        write_the_front_of_the_queue();
      }
    }
  }

  sentry::mqtt::Pending pending;
  pending.topic = where_it_goes(saying);
  pending.payload = reinterpret_cast<const uint8_t*>(outgoing);
  pending.size = outgoing_size;
  pending.retain = is_retained(saying);
  pending.announcement = saying == Saying::kAnnouncement && announcement_opens_the_connection;

  // Static, like every buffer in this program: the stack here is four kilobytes and a
  // packet is two and a half of them.
  static uint8_t packet[sentry::mqtt::kMaxPacketBytes];
  sentry::mqtt::Todo todo = sentry::mqtt::Todo::kNothing;
  const size_t size =
      broker.next(now, outgoing_size > 0 ? &pending : nullptr, packet, sizeof(packet), todo);
  if (todo == sentry::mqtt::Todo::kGiveUp) {
    end_the_connection("the broker stopped answering");
    return;
  }
  if (todo == sentry::mqtt::Todo::kDrop) {
    // The client will not write this one at all. Holding it would stop everything behind
    // it, so it is let go of and counted by whoever wrote it.
    std::printf("# a message could not be written as a packet and was dropped\n");
    if (saying == Saying::kEvent) spool.accepted();
    if (saying == Saying::kAck) forget_the_first_ack();
    outgoing_size = 0;
    saying = Saying::kNothing;
    return;
  }
  if (size == 0) return;
  const size_t sent = net::send(packet, size);
  if (todo == sentry::mqtt::Todo::kLeave && sent == size) {
    // Said, and heard or not: a broker that has the DISCONNECT will keep the will to
    // itself, and one that missed it will publish a goodbye this node has already sent.
    end_the_connection(wanted ? "the connection was ended by this node" : "gone, as asked");
    return;
  }
  if (sent != size) {
    // The window was full, and nothing was half written: send() takes a packet whole or
    // not at all. The client believes it went, so this shows up as an answer that never
    // came — which is what it is — and the connection is given up on and made again.
    std::printf("# a packet could not be sent; the connection will be made again\n");
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

// What is running, one line each: the driver, what it is reading and how often, and for a
// pin the level it is actually at. A plan that cannot be seen at the cable is a plan that
// has to be guessed at from the hub.
void say_the_plan() {
  std::printf("# plan=%lu revision=%lld\n", static_cast<unsigned long>(running.size()),
              static_cast<long long>(config_revision));
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    if (source.driver == sentry::Driver::kGpio) {
      std::printf("#   %s gpio pin=%d level=%s state=%s readings=%lld\n", source.source_id,
                  source.gpio.pin, live[index].level ? "high" : "low",
                  live[index].input.state() ? "on" : "off",
                  static_cast<long long>(live[index].readings));
    } else if (source.driver == sentry::Driver::kOneWire) {
      std::printf("#   %s onewire pin=%d device=%s every=%lus %sreadings=%lld\n",
                  source.source_id, source.onewire.pin,
                  source.onewire.has_rom ? source.onewire.device : "the one on the bus",
                  static_cast<unsigned long>(source.onewire.interval_ms / 1000),
                  live[index].converting ? "converting " : "",
                  static_cast<long long>(live[index].readings));
    } else if (source.driver == sentry::Driver::kAdc) {
      std::printf("#   %s adc pin=%d output=%s every=%lus readings=%lld\n", source.source_id,
                  source.adc.pin, source.adc.volts ? "volts" : "ratio",
                  static_cast<unsigned long>(source.adc.interval_ms / 1000),
                  static_cast<long long>(live[index].readings));
    } else {
      std::printf("#   %s board measure=%s every=%lus readings=%lld\n", source.source_id,
                  source.board.measure,
                  static_cast<unsigned long>(source.board.interval_ms / 1000),
                  static_cast<long long>(live[index].readings));
    }
  }
}

void say_status() {
  char address[20] = {};
  const bool has_address = net::address(address, sizeof(address));
  const net::TimeAnswers answers = net::time_answers();
  const char* socket_state = "idle";
  switch (net::socket()) {
    case net::Socket::kResolving:
      socket_state = "resolving";
      break;
    case net::Socket::kConnecting:
      socket_state = "connecting";
      break;
    case net::Socket::kOpen:
      socket_state = "open";
      break;
    case net::Socket::kClosed:
      socket_state = "closed";
      break;
    case net::Socket::kIdle:
      break;
  }
  const char* mqtt_state = "offline";
  switch (broker.link()) {
    case sentry::Link::kSubscribing:
      mqtt_state = "connected";
      break;
    case sentry::Link::kListening:
      mqtt_state = "listening";
      break;
    case sentry::Link::kOnline:
      mqtt_state = "online";
      break;
    case sentry::Link::kOffline:
      break;
  }
  std::printf("# node=%s provisioned=%s link=%s address=%s signal=%ld clock=%s answers=%lu\n",
              node_id, provisioned ? "yes" : "no", net::linked() ? "up" : "down",
              has_address ? address : "none", static_cast<long>(net::signal_strength()),
              clock_.synced() ? "synced" : "unsynced", static_cast<unsigned long>(answers.count));
  std::printf("# heap_free=%lldkB uptime=%llus reset=%s\n",
              static_cast<long long>(free_heap_kb()),
              static_cast<unsigned long long>(now_us() / 1000000),
              woke_from_watchdog ? "watchdog" : "power");
  std::printf("# credentials=%s socket=%s mqtt=%s queued=%lu coalesced=%lu dropped=%lu\n",
              credentials.complete() ? "yes" : "no", socket_state, mqtt_state,
              static_cast<unsigned long>(spool.size()),
              static_cast<unsigned long>(spool.losses().coalesced),
              static_cast<unsigned long>(spool.losses().dropped_transitions));
  const uint64_t now_ms = now_us() / 1000;
  const char* grant = lease.grant_id();
  long long left = 0;
  if (lease.live(now_ms)) left = static_cast<long long>(lease.expires_at() - now_ms);
  std::printf("# grant=%s epoch=%lld expires_in=%llds owed=%lu answered=%lu unanswered=%lu\n",
              grant != nullptr ? grant : "none", static_cast<long long>(lease.hub_epoch()),
              left / 1000, static_cast<unsigned long>(owed_acks),
              static_cast<unsigned long>(answered.size()),
              static_cast<unsigned long>(acks_lost));
  say_the_plan();
}

// A wire this board has not got. A planned pin is driven from the cable so that the
// debounce, the settling and the transition are exercised on the real pad rather than in a
// test: the pin is read back the same way a sensor's would be, because it is the same pin.
// It stays an output until something says otherwise, which is what a sensor holding a line
// looks like.
void drive_a_pin(const char* rest) {
  int pin = -1;
  int level = -1;
  if (std::sscanf(rest, "%d %d", &pin, &level) != 2 || (level != 0 && level != 1)) {
    std::printf("# say: drive <pin> <0|1>\n");
    return;
  }
  const char* holder = running.holder(pin);
  if (holder == nullptr) {
    std::printf("# nothing in the plan is on pin %d\n", pin);
    return;
  }
  gpio_set_pulls(static_cast<uint>(pin), false, false);
  gpio_put(static_cast<uint>(pin), level != 0);
  gpio_set_dir(static_cast<uint>(pin), GPIO_OUT);
  std::printf("# pin %d (%s) driven %s\n", pin, holder, level != 0 ? "high" : "low");
}

void take_provisioning(const char* document, bool keep) {
  sentry::Provisioning found;
  const size_t size = std::strlen(document);
  if (!sentry::read_provisioning(document, size, found)) {
    std::printf("# that is not a provisioning record this firmware can use\n");
    return;
  }
  provisioning = found;
  provisioned = true;
  if (keep && !vault::save(sentry::Held::kIdentity, document, size)) {
    // Worth saying out loud: this node is provisioned now and will not be after a reset,
    // which is exactly the sort of thing a board does quietly and nobody finds out about
    // until it comes back as a stranger.
    std::printf("# this node is provisioned, but the record could not be written to flash\n");
  }
  // Both are the same fixed array, and the record was checked before it got here.
  std::memcpy(node_id, found.node_id, sizeof(node_id));
  // The passphrase is never printed back: a serial log is a file like any other.
  std::printf("# provisioned as %s, broker %s:%u, network %s\n", provisioning.node_id,
              provisioning.mqtt_host, static_cast<unsigned>(provisioning.mqtt_port),
              provisioning.has_wifi() ? provisioning.wifi_ssid : "none");
}

// Everything a node keeps, in the order it is needed at boot.
constexpr sentry::Held kEverythingKept[] = {
    sentry::Held::kIdentity, sentry::Held::kAuthority, sentry::Held::kCertificate,
    sentry::Held::kPrivateKey, sentry::Held::kConfiguration};

// The three parts of the handshake, written one to a pair of slots. The text is written
// without the terminator the TLS layer counts: what is kept is the PEM, and what reads it
// back hands it to the same function the cable does.
void keep_the_credentials() {
  const struct {
    sentry::Held held;
    bool present;
    const char* text;
    size_t size;
  } parts[] = {
      {sentry::Held::kAuthority, credentials.has_authority(), credentials.authority(),
       credentials.authority_size()},
      {sentry::Held::kCertificate, credentials.has_certificate(), credentials.certificate(),
       credentials.certificate_size()},
      // The key goes to flash and is not read back out by anything but the handshake. A
      // board somebody walks away with gives it up; that is what the certificate being
      // this node's own, and revocable, is for.
      {sentry::Held::kPrivateKey, credentials.has_private_key(), credentials.private_key(),
       credentials.private_key_size()},
  };
  for (const auto& part : parts) {
    if (!part.present) continue;
    if (!vault::save(part.held, part.text, part.size - 1)) {
      std::printf("# the %s is in use but could not be written to flash\n",
                  sentry::name_of(part.held));
    }
  }
}

// What is in the flash, said without saying what is in it. A sequence number and a name
// are enough to tell a board that was provisioned from one that was not; the authority is
// the only one of these that would be harmless to print, and it is not printed either,
// because a rule with an exception in it is a rule somebody edits later.
void say_what_is_kept() {
  std::printf("# vault at 0x%08lx, %lu bytes, %u slots of %u\n",
              static_cast<unsigned long>(vault::begins_at()),
              static_cast<unsigned long>(sentry::kVaultBytes),
              static_cast<unsigned>(sentry::kHeldKinds * sentry::kVaultSlots),
              static_cast<unsigned>(sentry::kVaultSlotBytes));
  for (sentry::Held held : kEverythingKept) {
    uint32_t writes = 0;
    if (vault::holds(held, writes)) {
      std::printf("#   %-13s kept, write %lu\n", sentry::name_of(held),
                  static_cast<unsigned long>(writes));
    } else {
      std::printf("#   %-13s empty\n", sentry::name_of(held));
    }
  }
}

// The recovery verb. A board whose identity is wrong, or whose key must not stay on it, is
// erased here rather than reflashed: the image has none of this in it, so a new image
// would come up with exactly the same flash behind it.
void forget_what_is_kept(const char* rest) {
  while (*rest == ' ') ++rest;
  const bool everything = *rest == '\0' || std::strcmp(rest, "everything") == 0;
  bool done = false;
  for (sentry::Held held : kEverythingKept) {
    if (!everything && std::strcmp(rest, sentry::name_of(held)) != 0) continue;
    std::printf("# forgetting the %s: %s\n", sentry::name_of(held),
                vault::forget(held) ? "gone" : "the flash would not let go of it");
    done = true;
  }
  if (!done) {
    std::printf("# forget what? identity, authority, certificate, key, configuration, or"
                " everything\n");
    return;
  }
  // And out of memory as well as out of flash, or the next connection would be made with
  // what this node was just told to forget.
  if (everything || std::strcmp(rest, "identity") == 0) {
    provisioning = sentry::Provisioning{};
    provisioned = false;
    name_this_board();
  }
  if (everything || std::strcmp(rest, "authority") == 0 ||
      std::strcmp(rest, "certificate") == 0 || std::strcmp(rest, "key") == 0) {
    credentials.forget();
  }
  wanted = false;
  std::printf("# this board is %s; it will come back this way after a reset\n",
              provisioned ? "still provisioned" : "no longer provisioned");
}

// What this board was left with, read back before anything is started. The order matters:
// the name comes first, because the configuration in flash is addressed to it and is
// refused by the same parser the hub's commands go through if it is not this node's.
void take_back_what_was_kept() {
  static char kept[sentry::kVaultSlotBytes];
  size_t size = vault::load(sentry::Held::kIdentity, kept, sizeof(kept) - 1);
  if (size > 0) {
    kept[size] = '\0';
    take_provisioning(kept, false);
  }
  size = vault::load(sentry::Held::kAuthority, kept, sizeof(kept));
  if (size > 0 && !credentials.take_authority(kept, size)) {
    std::printf("# the authority in flash is not one this firmware can use\n");
  }
  size = vault::load(sentry::Held::kCertificate, kept, sizeof(kept));
  if (size > 0 && !credentials.take_certificate(kept, size)) {
    std::printf("# the certificate in flash is not one this firmware can use\n");
  }
  size = vault::load(sentry::Held::kPrivateKey, kept, sizeof(kept));
  if (size > 0 && !credentials.take_private_key(kept, size)) {
    std::printf("# the key in flash is not one this firmware can use\n");
  }
  if (credentials.complete()) {
    std::printf("# credentials: read back from flash, all three\n");
  }
  size = vault::load(sentry::Held::kConfiguration, kept, sizeof(kept));
  if (size == 0) return;
  sentry::Command command;
  sentry::Refusal why = sentry::Refusal::kNone;
  if (!sentry::parse_command(kept, size, node_id, command, why)) {
    std::printf("# the configuration in flash is not one this node can read: %s\n",
                sentry::name_of(why));
    return;
  }
  const char* detail = nullptr;
  if (!take_a_configuration(command, detail, nullptr, 0)) {
    // The board this configuration was written for is this board, so this means the
    // firmware changed under it. It keeps its own temperature and waits to be told again.
    std::printf("# the configuration in flash was refused: %s\n", detail != nullptr ? detail : "");
    return;
  }
  std::printf("# running what it was left with: revision %lld, %u sources\n",
              static_cast<long long>(config_revision), static_cast<unsigned>(running.size()));
}

// Whether this board has everything it needs to be a satellite again without being told.
bool it_can_carry_on_by_itself() {
  return provisioned && provisioning.has_wifi() && credentials.complete();
}

// Whatever the host has sent, up to a newline. Returns false when there is no full line.
bool read_line(char* out, size_t capacity) {
  // Long enough for a line carrying three PEM blocks, which is the longest thing anybody
  // sends this board.
  static char pending[4096];
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
    take_provisioning(line + 10, true);
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
  if (std::strcmp(line, "keeping") == 0) {
    say_what_is_kept();
    return;
  }
  if (std::strncmp(line, "tear ", 5) == 0) {
    const char* what = line + 5;
    for (sentry::Held held : kEverythingKept) {
      if (std::strcmp(what, sentry::name_of(held)) != 0) continue;
      std::printf("# half a %s written to the slot that is not in use: %s\n",
                  sentry::name_of(held),
                  vault::tear(held) ? "unreadable, as an interrupted write is"
                                    : "readable, which is not what a torn write looks like");
      return;
    }
    std::printf("# tear what? identity, authority, certificate, key or configuration\n");
    return;
  }
  if (std::strncmp(line, "forget", 6) == 0 && (line[6] == '\0' || line[6] == ' ')) {
    forget_what_is_kept(line + 6);
    return;
  }
  if (std::strcmp(line, "hang") == 0) {
    // On purpose, and the only way to see the watchdog do its job: the loop stops going
    // round, nothing feeds it, and the chip resets itself a few seconds later. What comes
    // back says so, with a new boot id and the same identity.
    std::printf("# not feeding the watchdog; this board should reset in about %lu ms\n",
                static_cast<unsigned long>(kWatchdogMs));
    std::fflush(stdout);
    while (true) tight_loop_contents();
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
    // Everything this node runs, now rather than when it was next due.
    for (size_t index = 0; index < running.size(); ++index) {
      if (running.at(index).driver != sentry::Driver::kGpio) {
        live[index].due_ms = static_cast<uint32_t>(now_us() / 1000);
      }
    }
    read_the_sources();
    return;
  }
  if (std::strncmp(line, "drive ", 6) == 0) {
    drive_a_pin(line + 6);
    return;
  }
  if (std::strncmp(line, "command ", 8) == 0) {
    // The same path a command from the broker takes, so that what the lease does can be
    // tried on a bench with no hub at the other end — and so that what is tried there is
    // the code that runs when there is one.
    sentry::Command command;
    sentry::Refusal why = sentry::Refusal::kNone;
    if (!sentry::parse_command(line + 8, std::strlen(line + 8), node_id, command, why)) {
      std::printf("# not a command for this node: %s\n", sentry::name_of(why));
      return;
    }
    obey_a_command(command, line + 8, std::strlen(line + 8));
    return;
  }
  if (std::strncmp(line, "credentials ", 12) == 0) {
    // Nothing of what arrives here is printed back. The certificate would be harmless and
    // the key would not, and a serial log is a file like any other.
    if (!sentry::read_credentials(line + 12, std::strlen(line + 12), credentials)) {
      std::printf("# that is not a set of credentials this firmware can use\n");
      return;
    }
    std::printf("# credentials: authority %s, certificate %s, key %s\n",
                credentials.has_authority() ? "yes" : "no",
                credentials.has_certificate() ? "yes" : "no",
                credentials.has_private_key() ? "yes" : "no");
    keep_the_credentials();
    return;
  }
  if (std::strcmp(line, "connect") == 0) {
    if (!provisioned) {
      std::printf("# no broker in the provisioning record; send: provision <json>\n");
      return;
    }
    if (!credentials.complete()) {
      std::printf("# no certificate to connect with; send: credentials <json>\n");
      return;
    }
    // The certificate's dates are judged against the time the network gave this board. A
    // node that has not been told the time cannot tell an expired certificate from a good
    // one, so it does not try.
    if (!clock_.synced()) {
      std::printf("# no time yet, so an expiry date means nothing here; join first\n");
      return;
    }
    wanted = true;
    attempts_since_online = 0;
    next_attempt_ms = monotonic_ms();
    return;
  }
  if (std::strcmp(line, "disconnect") == 0) {
    wanted = false;
    if (broker.link() == sentry::Link::kOffline) {
      end_the_connection("asked to disconnect");
      return;
    }
    // There is a connection to say goodbye on, so it is used: what is owed goes first,
    // then the retained state that says this node is gone, then the DISCONNECT.
    stopping = true;
    return;
  }
  if (line[0] != '\0') std::printf("# not a command: %s\n", line);
}

}  // namespace

int main() {
  stdio_init_all();

  // Read before anything turns it on again, because turning it on is what clears it.
  woke_from_watchdog = watchdog_caused_reboot();

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

  run_the_default_plan();
  // And then whatever it was told to run instead, if it was told anything before the
  // power went off. A board with nothing in its flash keeps its own temperature.
  take_back_what_was_kept();

  if (!make_uuid(boot_id) || !make_uuid(connection_id)) {
    std::printf("# this board cannot make an identifier for this boot\n");
    return 1;
  }

  // The host opens the port after the board is already running, so nothing before this is
  // ever read. Saying it again on a timer would be noise; saying it once, after a moment,
  // is what a person watching a serial monitor needs.
  sleep_ms(2000);
  std::printf("# ready %s node=%s boot=%s connection=%s wireless=%s reset=%s\n", PICO_BOARD,
              node_id, boot_id, connection_id, wireless ? "up" : "no",
              woke_from_watchdog ? "watchdog" : "power");
  std::fflush(stdout);

  if (it_can_carry_on_by_itself()) {
    come_back_on_its_own = true;
    join_the_network();
  }

  // From here on the loop has to keep coming round. Everything before this point is the
  // one place where it does not — joining a network, reading flash and waiting for a
  // serial monitor are all allowed to take their time exactly once.
  watchdog_enable(kWatchdogMs, true);

  absolute_time_t next = make_timeout_time_ms(kHeartbeatBlinkMs);
  while (true) {
    watchdog_update();
    net::poll();
    take_the_time_if_it_arrived();
    if (net::socket() != net::Socket::kIdle) {
      // Whether or not this node still wants to be connected: a goodbye is said on the
      // connection it is about, and hanging up first would be the silence it is there to
      // avoid.
      serve_the_broker();
    } else if (wanted && static_cast<int32_t>(monotonic_ms() - next_attempt_ms) >= 0) {
      start_a_connection();
    }
    static char line[4096];
    if (read_line(line, sizeof(line))) obey(line);
    // Every pin, every loop: the debounce is measured in time and not in sleeping, so
    // nothing here waits on a wire while the connection waits on this.
    read_the_sources();
    if (absolute_time_diff_us(get_absolute_time(), next) <= 0) {
      // A sign of life, and nothing more: it says the loop is turning, not that anything
      // was published.
      led(true);
      sleep_ms(20);
      led(false);
      next = make_timeout_time_ms(kHeartbeatBlinkMs);
    }
    sleep_ms(5);
  }
}
