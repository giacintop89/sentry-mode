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
//   deafen                 stop the scanner, on purpose, to see presence go unknown
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

#include "ble.h"
#if SENTRY_BRIDGED
#include "bridge.h"
#endif
#include "i2s.h"
#include "media.h"
#include "flash_vault.h"
#include "hardware/adc.h"
#include "hardware/watchdog.h"
#include "net.h"
#include "reset_reason.h"
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
#include "sentry/json.h"
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
// How often a node that wants a scanner and has not got one asks again. The radio is on
// the wireless chip, so until this board has joined a network there is nothing to ask.
constexpr uint32_t kRadioPatienceMs = 5000;
// How long a time source may say nothing before this node stops calling what it stamps
// synced. lwIP asks again every hour; three of those with no answer is a source that has
// gone, not one that is slow. Nothing stops being stamped — the offset is still the best
// estimate this board has — but what it claims about the stamp changes, and the hub reads
// exactly that to decide whether an event is live.
constexpr uint64_t kTimeGoesStaleUs = UINT64_C(3) * 3600 * 1000000;
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
// Why the board is running, read once before the watchdog is turned on again. A board that
// keeps coming back the same way is a board with something wrong with it, and the hub
// cannot see the difference unless it is told; the serial line is not a place a hub looks.
sentry::Woke woke = sentry::Woke::kUnknown;
// A board that was provisioned, given its credentials and told what to run comes back
// without anybody at the cable: it joins, waits to be told the time, and connects. Nothing
// here decides to do that on its own — it is what it was doing when the power went off.
bool come_back_on_its_own = false;
// When the last answer from the time source arrived, on this board's own clock.
uint64_t time_answered_at_us = 0;
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
  // What it last read, for the health message. An event is gone once it has been
  // published; what the hub's page asks is what this source is reading now, and a node
  // that only ever reported a count leaves that question to the next transition.
  // What the radio has made of one device. A watch that nobody configured is a watch that
  // has never been covered, which reads as unknown — which is what a source with no
  // scanner behind it should say.
  sentry::Watch watch;
  // What the microphone has been hearing. The sound itself is gone by the time this has
  // been updated: a level, a state, and a count of the blocks it was decided from.
  sentry::Activity activity;
  uint32_t blocks = 0;
  bool has_last = false;
  sentry::Value last;
  const char* unit = nullptr;
  sentry::Quality quality = sentry::Quality::kValid;
  uint64_t last_at_ms = 0;
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

#if !SENTRY_BRIDGED
// Bytes that have arrived and are not yet a whole packet.
uint8_t incoming[sentry::mqtt::kMaxPacketBytes * 2] = {};
size_t incoming_size = 0;
#endif

bool wanted = false;             // somebody asked this node to be connected
uint32_t next_attempt_ms = 0;    // and the backoff says not before this
bool announced_this_connection = false;
// The first announcement of a connection is the one the session waits for; a second one,
// after a configuration, is an ordinary retained publish and must not be mistaken for it.
bool announcement_opens_the_connection = false;
// What this node runs has changed and the retained state still says otherwise.
bool owes_a_declaration = false;
#if !SENTRY_BRIDGED
// The client is told a connection opened once per connection. It stays offline until the
// broker's CONNACK, so "it has not said it is connected" is not the same question.
bool client_told = false;
#endif
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

// One reading, put in the queue for the broker — and, on a board whose cable carries text,
// written out for a person to see as well. Whether the hub may have it is the lease's
// business and is decided elsewhere: what is taken is taken, and what is queued waits.
void offer_the_reading(Live& state, const sentry::Reading& reading, bool initial,
                      sentry::Kept kept) {
  // Kept before it is queued, because it is kept whether or not the hub ever sees it: a
  // node with no grant is still measuring, and health is how it says what it measures.
  // Text is the one kind not kept — nothing here produces one, and what a `Value` of that
  // sort points at is a buffer that will be something else by the next reading.
  if (reading.value.type != sentry::Value::Type::kText) {
    state.has_last = true;
    state.last = reading.value;
    state.unit = reading.unit;
    state.quality = reading.quality;
    state.last_at_ms = now_us() / 1000;
  }

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
#if !SENTRY_BRIDGED
  std::fwrite(buffer, 1, size, stdout);
  std::fputc('\n', stdout);
  std::fflush(stdout);
#else
  // Not on a bridged board. The console shares the cable with the messages there, so a
  // reading written out for a person to read is the same reading twice down one wire —
  // and the second copy is the one that arrives as text and can never be published.
  (void)size;
#endif

  if (provisioned && !spool.offer(reading, kept, now_us() / 1000, initial)) {
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
  offer_the_reading(state, reading, initial,
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

  offer_the_reading(state, reading, initial,
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

  offer_the_reading(state, reading, initial,
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

// Whether one device is here. The words are the Linux agent's and so is the judgement,
// which is in `presence.cpp`; what is here is only the part that has a clock and a queue.
//
// Unknown is published with no value and a quality that says so, exactly as the agent
// does: a node that cannot hear has not found out that the room is empty.
void take_a_presence_reading(const sentry::Planned& source, Live& state) {
  uint64_t moment = 0;
  char stamped[32] = {};
  char event_id[sentry::kUuidText] = {};
  if (!moment_of(moment, stamped, sizeof(stamped), event_id)) return;

  const sentry::Presence where = state.watch.state();
  const bool nothing_known = where == sentry::Presence::kUnknown;

  sentry::Reading reading;
  reading.event_id = event_id;
  reading.node_id = node_id;
  reading.source_id = source.source_id;
  reading.boot_id = boot_id;
  reading.sequence = sequence++;
  reading.kind = source.ble.event_kind;
  reading.occurred_at = stamped;
  reading.clock = clock_.status_at(moment);
  reading.quality = nothing_known ? sentry::Quality::kUnknown : sentry::Quality::kValid;
  if (!nothing_known) reading.value = sentry::Value::of(sentry::name_of(where));
  ++state.readings;

  // Somebody arriving is not replaceable by somebody else arriving, and the first state
  // after unknown is where things stand rather than news — the same distinction a baseline
  // makes everywhere else here.
  offer_the_reading(state, reading, !nothing_known && state.watch.is_baseline(),
                    sentry::Kept::kTransition);
}

// The radio's half of the loop: whether it is listening at all, and everything it heard
// since the last turn. Nothing is matched here — `is_the_one` is in `presence.cpp` and has
// never seen a radio.
void serve_the_radio(uint64_t now_ms) {
  bool anybody_watching = false;
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    if (source.driver == sentry::Driver::kBle && source.enabled) anybody_watching = true;
  }
  // The controller is on the wireless chip, so a board that has not joined a network yet
  // has nothing to start. Asked for again, here, rather than once at configuration time:
  // a plan taken before the radio exists is still a plan that wants one.
  static uint32_t ask_again_ms = 0;
  if (anybody_watching && !ble::listening() &&
      static_cast<int32_t>(static_cast<uint32_t>(now_ms) - ask_again_ms) >= 0) {
    ask_again_ms = static_cast<uint32_t>(now_ms) + kRadioPatienceMs;
    ble::listen();
  }
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    if (source.driver != sentry::Driver::kBle || !source.enabled) continue;
    // Told first, and every turn: a scanner that has stopped makes every watch unknown
    // before anything else is decided about them.
    if (live[index].watch.covered(ble::listening(), now_ms)) {
      take_a_presence_reading(source, live[index]);
    }
  }
  if (!anybody_watching) return;

  // Bounded, so that a room full of advertisements cannot keep this turn from ending. What
  // is left waits one more turn, which is a millisecond or two.
  constexpr size_t kMostPerTurn = 32;
  ble::Sighting sighting;
  for (size_t taken = 0; taken < kMostPerTurn && ble::next(sighting); ++taken) {
    for (size_t index = 0; index < running.size(); ++index) {
      const sentry::Planned& source = running.at(index);
      if (source.driver != sentry::Driver::kBle || !source.enabled) continue;
      if (!sentry::is_the_one(source.ble.watched, sighting.address, sighting.data,
                              sighting.size)) {
        continue;
      }
      if (live[index].watch.seen(sighting.at_ms, sighting.rssi, sighting.has_rssi)) {
        take_a_presence_reading(source, live[index]);
      }
    }
  }
}

// That something was loud. It is what a microphone produces here whether or not anybody is
// listening, and it is decided on the board: the sound itself goes out only while the hub
// has asked for it, and nothing is ever recorded here to ask for afterwards.
void take_an_acoustic_reading(const sentry::Planned& source, Live& state, bool active,
                              bool baseline) {
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
  reading.kind = source.microphone.event_kind;
  reading.occurred_at = stamped;
  reading.clock = clock_.status_at(moment);
  reading.quality = sentry::Quality::kValid;
  reading.value = sentry::Value::of(active);
  ++state.readings;

  // A noise that started is not replaceable by a noise that stopped, and the first one
  // after a configuration is where things stand rather than news.
  offer_the_reading(state, reading, baseline, sentry::Kept::kTransition);
}

// The microphone's half of the loop: every block the DMA has finished since the last turn,
// measured and handed to `acoustic.cpp`, which has never seen a pin. The samples are gone
// when this returns — read once, turned into one number, and the buffer handed back.
//
// Time is counted in the sound that was actually heard rather than on the clock. A block
// that was never captured, because this loop was busy elsewhere, is not silence: a hold
// that timed out across a gap would be this board deciding a room went quiet during the
// one stretch it could not hear it.
void serve_the_microphone(const sentry::Planned& source, Live& state) {
  constexpr size_t kMostPerTurn = 4;
  constexpr double kBlockSeconds =
      static_cast<double>(i2s::kFramesPerBlock) / static_cast<double>(i2s::kRate);
  const int16_t* samples = nullptr;
  size_t count = 0;
  for (size_t taken = 0; taken < kMostPerTurn && i2s::next(samples, count); ++taken) {
    ++state.blocks;
    // The same samples, offered to the stream if the hub is asking for one. It copies what
    // it needs and keeps nothing else: this is the only place sound goes anywhere but into
    // a number, and it happens only while a granted stream is open.
    if (media::wants(source.source_id)) media::offer(samples, count);
    const sentry::Activity::Change change =
        state.activity.step(sentry::dbfs(samples, count), kBlockSeconds);
    if (change != sentry::Activity::Change::kNothing) {
      take_an_acoustic_reading(source, state, change == sentry::Activity::Change::kStarted,
                               false);
    }
  }
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
  offer_the_reading(state, reading, report == sentry::Report::kBaseline,
                    sentry::Kept::kTransition);
}

// Every pin in the plan, read and judged. This runs on the same loop that keeps the
// connection alive, so it does no waiting of its own: the debounce is time, not sleep.
void read_the_sources() {
  const uint64_t now_ms = now_us() / 1000;
  serve_the_radio(now_ms);
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    if (!source.enabled) continue;
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
      continue;
    }
    if (source.driver == sentry::Driver::kBle) {
      // Time passing with the radio listening and nothing heard. The sightings were fed in
      // above; this is the only thing that can conclude an absence.
      if (state.watch.quiet(now_ms)) take_a_presence_reading(source, state);
      continue;
    }
    if (source.driver == sentry::Driver::kMicrophone) {
      serve_the_microphone(source, state);
    }
  }
}

// Where every source stands, taken because a hub has just granted this node or because
// what this node runs has just changed. Nothing here is news and all of it says so.
void take_every_baseline() {
  const uint64_t now_ms = now_us() / 1000;
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    if (!source.enabled) continue;
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
    } else if (source.driver == sentry::Driver::kBle) {
      // Said rather than rearmed: the watch already knows where the device stands, and a
      // hub that has just granted this node is asking for exactly that. A watch still
      // waiting for its first sighting says unknown, which is the honest answer.
      take_a_presence_reading(source, state);
    } else if (source.driver == sentry::Driver::kMicrophone) {
      // Whether the room is loud right now, which is false on a microphone that has not
      // heard a block yet. The agent says the same thing at the same moment.
      take_an_acoustic_reading(source, state, state.activity.active(), true);
    }
  }
}

// Give up the pins the old plan held and take the ones the new plan asks for. Nothing is
// touched until a plan has been agreed to, which is what `plan.cpp` is for.
void start_the_plan() {
  const uint64_t now_ms = now_us() / 1000;
  bool wants_the_radio = false;
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    Live& state = live[index];
    state = Live{};
    // A source that is in the configuration without being read takes no pin and starts no
    // driver. It is still reported, as itself and as disabled: a list the hub can see is
    // the point of writing it down at all.
    if (!source.enabled) continue;
    if (source.driver == sentry::Driver::kGpio) {
      const uint pin = static_cast<uint>(source.gpio.pin);
      gpio_init(pin);
      // A pad that has been left with no function and no pull on it floats, and on this
      // chip it can float up and stay up: a pin switched off and back on read high with
      // its pull-down enabled, for as long as it was watched, while the same pin read low
      // on a fresh boot and across a configuration that never let go of it. So the level
      // the configuration calls idle is driven onto the pad for a moment first. That is
      // not a guess about the wiring: `pull_down` is a claim that the wire idles low, and
      // this asserts what was already claimed for ten microseconds before letting go.
      if (source.gpio.bias != sentry::Bias::kNone) {
        gpio_put(pin, source.gpio.bias == sentry::Bias::kPullUp);
        gpio_set_dir(pin, GPIO_OUT);
        sleep_us(10);
      }
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
    } else if (source.driver == sentry::Driver::kBle) {
      // The plan has already agreed this is something a watch can survive; this cannot
      // fail for any other reason, and if it somehow did the watch would keep the defaults
      // rather than run with half a configuration.
      if (!state.watch.configure(source.ble.how)) {
        std::printf("# %s: the watch refused what the plan agreed to\n", source.source_id);
      }
      wants_the_radio = true;
    } else if (source.driver == sentry::Driver::kMicrophone) {
      if (!state.activity.configure(source.microphone.how)) {
        std::printf("# %s: the listener refused what the plan agreed to\n", source.source_id);
      }
      // The plan has already refused a second microphone, so this is the only one asking.
      if (!i2s::listen(static_cast<uint>(source.microphone.pin),
                       static_cast<uint>(source.microphone.clock_pin),
                       source.microphone.left)) {
        std::printf("# %s: the microphone would not start: %s\n", source.source_id,
                    i2s::trouble());
      }
    }
  }
  // One radio, however many devices are watched with it. Asked for after the loop rather
  // than inside it, so that eight sources are one scanner and not eight attempts at one.
  if (wants_the_radio) {
    if (!ble::listen()) std::printf("# the radio would not start: %s\n", ble::trouble());
  } else {
    ble::deaf();
  }
}

void stop_the_plan() {
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    if (!source.enabled) continue;
    if (source.driver == sentry::Driver::kGpio) {
      const uint pin = static_cast<uint>(source.gpio.pin);
      gpio_set_pulls(pin, false, false);
      gpio_deinit(pin);
    } else if (source.driver == sentry::Driver::kAdc) {
      gpio_deinit(static_cast<uint>(source.adc.pin));
    } else if (source.driver == sentry::Driver::kOneWire) {
      onewire::give_back(static_cast<uint>(source.onewire.pin));
    } else if (source.driver == sentry::Driver::kMicrophone) {
      // This one is given back, unlike the radio: it holds three pins and keeps a clock
      // running on one of them, and the next plan may want them for something else. A
      // stream from it ends here too — the hub asked for sound from a source that is
      // about to stop existing.
      media::stop(nullptr, "the source it was streaming from is no longer configured");
      i2s::stop();
    }
  }
  // The radio is not given back here. What comes next is `start_the_plan`, which asks for
  // it again if anything in the new plan watches a device; switching the controller off
  // between two plans that both want it would lose every watch for no reason.
}

// What this board runs when nobody has told it anything: the one thing it has without a
// wire on it. It is built as a configuration and goes through the same door one from the
// hub goes through, so there is no second way into the plan and nothing to keep in step.
void run_the_default_plan() {
  // Static because a source is nearly two kilobytes and the stack is eight: this runs once,
  // at boot, and a frame that size is one nobody should have to think about again.
  static sentry::Source source;
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

// The wall clock moved further than a correction moves it — a timezone, a daylight saving
// change, or a source that had itself been wrong. What this node has stamped and not yet
// published was written against the offset that has just been replaced, so it is out by
// about as much. The timestamps stay as they were taken; what goes is the claim that they
// are synced, which is the one of the two the hub decides anything from.
void the_clock_stepped() {
  spool.clock_stepped();
  std::printf("# the time moved by %lld ms: what is still queued no longer claims to be synced\n",
              static_cast<long long>(clock_.moved_by_us() / 1000));
}

// Whatever the network last said the time was, handed to the timebase here rather than in
// the callback: what a reading may claim about itself is decided in one place, on this
// loop, and not from lwIP's context.
void take_the_time_if_it_arrived() {
  const net::TimeAnswers answers = net::time_answers();
  if (answers.count == answers_seen || answers.count == 0) {
    if (clock_.synced() && time_answered_at_us != 0 &&
        now_us() - time_answered_at_us > kTimeGoesStaleUs) {
      clock_.lost();
    }
    return;
  }
  answers_seen = answers.count;
  // Read rather than extended: the answer was stamped inside lwIP's callback, before the
  // loop's own last reading of the counter, and handing an older value to `extend` would
  // look exactly like the counter going round.
  time_answered_at_us = ticks.just_before(static_cast<uint32_t>(answers.taken_at_us));
  if (clock_.sync(answers.last_unix_ms, time_answered_at_us)) the_clock_stepped();
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


#if !SENTRY_BRIDGED
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
#endif

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
  // The device a `ble` source watches, spelled out. It is kept here rather than on a stack
  // because what a declared option holds is a pointer, and the message is written after.
  static char watched[sentry::kMaxPlanned][40];
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
    } else if (source.driver == sentry::Driver::kBle) {
      // The device is said back the way it was written down. A watch reported as sixteen
      // raw bytes is a watch nobody can check against the file they sent.
      if (source.ble.watched.by_address) {
        sentry::write_address(source.ble.watched.address, watched[index], sizeof(watched[0]));
        options[index][at++] = {"address", sentry::Value::of(watched[index])};
      } else {
        sentry::write_uuid(source.ble.watched.uuid, watched[index], sizeof(watched[0]));
        options[index][at++] = {"ibeacon_uuid", sentry::Value::of(watched[index])};
        if (source.ble.watched.major >= 0) {
          options[index][at++] = {
              "ibeacon_major",
              sentry::Value::of(static_cast<int64_t>(source.ble.watched.major))};
        }
        if (source.ble.watched.minor >= 0) {
          options[index][at++] = {
              "ibeacon_minor",
              sentry::Value::of(static_cast<int64_t>(source.ble.watched.minor))};
        }
      }
      options[index][at++] = {
          "rssi_min", sentry::Value::of(static_cast<int64_t>(source.ble.how.rssi_min))};
      options[index][at++] = {
          "enter_sightings",
          sentry::Value::of(static_cast<int64_t>(source.ble.how.enter_sightings))};
      options[index][at++] = {
          "enter_window_seconds",
          sentry::Value::of(static_cast<double>(source.ble.how.enter_window_ms) / 1000.0, 1)};
      options[index][at++] = {
          "absent_after_seconds",
          sentry::Value::of(static_cast<double>(source.ble.how.absent_after_ms) / 1000.0, 1)};
      options[index][at++] = {"event_kind", sentry::Value::of(source.ble.event_kind)};
    } else if (source.driver == sentry::Driver::kMicrophone) {
      options[index][at++] = {"pin",
                              sentry::Value::of(static_cast<int64_t>(source.microphone.pin))};
      options[index][at++] = {
          "clock_pin", sentry::Value::of(static_cast<int64_t>(source.microphone.clock_pin))};
      options[index][at++] = {"channel",
                              sentry::Value::of(source.microphone.left ? "left" : "right")};
      options[index][at++] = {"activity_threshold_dbfs",
                              sentry::Value::of(source.microphone.how.threshold_dbfs, 1)};
      options[index][at++] = {"activity_min_seconds",
                              sentry::Value::of(source.microphone.how.min_seconds, 1)};
      options[index][at++] = {"activity_hold_seconds",
                              sentry::Value::of(source.microphone.how.hold_seconds, 1)};
      options[index][at++] = {"event_kind", sentry::Value::of(source.microphone.event_kind)};
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
#if SENTRY_BRIDGED
  // Said by the node because the node is the only one that knows: the bridge forwards what
  // it is handed and writes nothing into it. A hub reading this knows there is a second
  // thing that has to be running for this node to be heard at all.
  here.reached_by = "bridge";
#endif
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
    sources[index].has_last = live[index].has_last;
    sources[index].last = live[index].last;
    sources[index].unit = live[index].unit;
    sources[index].quality = live[index].quality;
    if (live[index].has_last) {
      const uint64_t since = moment / 1000 - live[index].last_at_ms;
      sources[index].has_age = true;
      sources[index].last_reading_age_seconds = static_cast<double>(since) / 1000.0;
    } else {
      sources[index].has_age = false;
    }
    if (running.at(index).driver == sentry::Driver::kBle) {
      // A watch is like a wire and not like a temperature: what health is asked is where
      // the device stands now, which the watch knows whether or not anything was published
      // about it. An unknown is reported as unknown rather than left out, because a source
      // with nothing to say and a source that cannot hear are different things.
      const sentry::Presence where = live[index].watch.state();
      sources[index].has_last = where != sentry::Presence::kUnknown;
      sources[index].last = sentry::Value::of(sentry::name_of(where));
      sources[index].unit = nullptr;
      sources[index].quality = where == sentry::Presence::kUnknown ? sentry::Quality::kUnknown
                                                                   : sentry::Quality::kValid;
      sources[index].has_age = live[index].watch.has_last_seen();
      if (sources[index].has_age) {
        const uint64_t since = moment / 1000 - live[index].watch.last_seen_ms();
        sources[index].last_reading_age_seconds = static_cast<double>(since) / 1000.0;
      }
      // Why it is not hearing, when it is not. The same field a probe that fell off its
      // wire would use, and the hub shows it the same way.
      if (running.at(index).enabled) sources[index].error = ble::trouble();
    }
    if (running.at(index).driver == sentry::Driver::kMicrophone) {
      // Like a wire and not like a temperature: what health is asked is whether the room
      // is loud now, which the listener knows whether or not anything was published about
      // it. A microphone that has not measured a block yet says so rather than saying the
      // room is quiet, which is a thing it has not heard.
      const bool heard = live[index].activity.has_level();
      sources[index].has_last = heard;
      sources[index].last = sentry::Value::of(live[index].activity.active());
      sources[index].unit = nullptr;
      sources[index].quality =
          heard ? sentry::Quality::kValid : sentry::Quality::kUnknown;
      sources[index].has_age = heard;
      sources[index].last_reading_age_seconds = 0.0;
      if (running.at(index).enabled) sources[index].error = i2s::trouble();
    }
    if (running.at(index).driver == sentry::Driver::kGpio && live[index].has_last) {
      // A wire is different from a measurement. The last event from a pin is the last time
      // it changed, which may have been an hour ago and may have been taken while the
      // sensor was still settling; what health is asked is how the pin is now, and this
      // loop reads it every turn. So this one is answered from the input rather than from
      // the last thing published: same value if nothing moved, and a quality that stops
      // saying `unknown` once the sensor has had its settling time.
      sources[index].last = sentry::Value::of(live[index].input.state());
      sources[index].unit = nullptr;
      sources[index].quality = live[index].input.quality(moment / 1000);
      sources[index].last_reading_age_seconds = 0.0;
    }
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
  health.board.woke = woke;
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
  uint64_t queued_at_ms = 0;
  if (!spool.front(reading, replayed, &initial, &queued_at_ms)) return false;
  sentry::Delivery delivery;
  delivery.connection_id = connection_id;
  delivery.hub_epoch = lease.hub_epoch();
  delivery.grant_id = lease.grant_id();
  // How long this reading waited here, on this node's own monotonic clock. It is the one
  // number in the envelope about the node rather than about the world, and it is the only
  // way the hub can tell a node that is falling behind from one with nothing to say. A
  // reading published as soon as it was taken says a few milliseconds; one that sat
  // through an outage says how long the outage was.
  const uint64_t now_ms = now_us() / 1000;
  delivery.queued_ms = static_cast<int64_t>(now_ms > queued_at_ms ? now_ms - queued_at_ms : 0);
  delivery.replayed = replayed;
  delivery.initial_state = initial;
  outgoing_size = sentry::write_event(reading, delivery, outgoing, sizeof(outgoing));
  saying = Saying::kEvent;
  return outgoing_size > 0;
}

#if !SENTRY_BRIDGED
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
#endif

#if !SENTRY_BRIDGED
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
#endif

#if !SENTRY_BRIDGED
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
  // The sound goes with it. A stream is permission the hub gave over this connection, and
  // a node that kept sending on the other one while the hub could not reach it would be
  // exactly the node nobody can switch off.
  media::stop(nullptr, "the connection this node was granted on is gone");
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
#endif

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
    default:
      return nullptr;
  }
}

// Which source in the running plan is a microphone that is actually listening, or none.
// A stream is asked for by source, and a source that is in the configuration but switched
// off, or that is a pin rather than a microphone, is not one this node can send.
const sentry::Planned* a_microphone_called(const char* source_id) {
  for (size_t index = 0; index < running.size(); ++index) {
    const sentry::Planned& source = running.at(index);
    if (source.driver != sentry::Driver::kMicrophone || !source.enabled) continue;
    if (std::strcmp(source.source_id, source_id) == 0) return &source;
  }
  return nullptr;
}

// A stream the hub has asked for. Everything here is a refusal except the last line: a
// node that started streaming on a command it had not checked would be a node whose
// microphone is switched on by whoever can reach the broker.
const char* answer_about_sound(const sentry::Command& command, uint64_t now_ms) {
  if (command.action == sentry::Action::kAudioStop) {
    media::stop(command.stream_id, "the hub asked it to stop");
    return nullptr;
  }
  // A stream rides on the events grant and never replaces it. A node whose permission has
  // run out does not start sending sound because a separate message said so.
  if (!lease.live(now_ms)) return "there is no live grant to stream under";
  const uint64_t until = now_ms + static_cast<uint64_t>(command.duration_seconds * 1000.0);
  if (command.action == sentry::Action::kAudioRenew) {
    const char* why_not = nullptr;
    return media::renew(command.stream_id, until, why_not) ? nullptr : why_not;
  }

  const sentry::Planned* source = a_microphone_called(command.source_id);
  if (source == nullptr) return "no microphone of that name is listening on this node";
  if (!i2s::running()) return i2s::trouble();
  const char* why_not = nullptr;
  // The host is this node's own, from the record it was provisioned with. The command
  // chose a port, which the parser has already held to the range a hub may name; it does
  // not get to choose the machine.
  if (!media::start(provisioning.mqtt_host, static_cast<uint16_t>(command.port),
                    command.stream_id, command.source_id, command.token, until, credentials,
                    why_not)) {
    return why_not;
  }
  return nullptr;
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
  if (command.action == sentry::Action::kAudioStart ||
      command.action == sentry::Action::kAudioRenew ||
      command.action == sentry::Action::kAudioStop) {
    const char* why_not = answer_about_sound(command, now_us() / 1000);
    const sentry::Answer answer =
        why_not == nullptr ? sentry::Answer::kApplied : sentry::Answer::kFailed;
    answered.remember(command.command_id, answer);
    queue_an_ack(command, answer, why_not);
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
  if (command.action == sentry::Action::kRevoke || decision.stop_requested) {
    // Whatever the sound was for, the permission behind it is gone. It stops here rather
    // than when the stream's own clock runs out: a revoked node that kept sending for
    // another two minutes would be a node the hub cannot switch off.
    media::stop(nullptr, "the grant it was streaming under is gone");
  }
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

// The one command this node has in hand at a time, and the largest thing this firmware
// holds: fourteen kilobytes with the sources in it. It lives here rather than in the three
// functions that parse one, because a local that size is a stack frame this chip does not
// have — the stack is eight kilobytes below the top of memory, and a frame that deep walks
// into the heap on its way down. There is one loop and one command in flight at a time:
// nothing here parses a command while another is being obeyed.
//
// Its zeros are in the image rather than in the section that is cleared at boot, because a
// structure whose every field has a default is initialised rather than empty as far as the
// compiler is concerned. That is fourteen kilobytes of flash, which this board has, to
// keep fourteen kilobytes off a stack it does not have.
sentry::Command command_in_hand;

// One command as it arrived from the broker.
void a_command_arrived(const char* payload, size_t size) {
  sentry::Command& command = command_in_hand;
  sentry::Refusal why = sentry::Refusal::kNone;
  if (!sentry::parse_command(payload, size, node_id, command, why)) {
    // Not answered, and deliberately: a command this node cannot read has no id it can
    // answer about, and answering about one it guessed would tell the hub something that
    // did not happen.
    std::printf("# a command arrived that this node will not act on: %s\n", sentry::name_of(why));
    return;
  }
  obey_a_command(command, payload, size);
}

// Everything the connection has to say, and everything this node has to say back.
#if !SENTRY_BRIDGED
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
        if (packet.type == sentry::mqtt::Type::kPublish) {
          a_command_arrived(reinterpret_cast<const char*>(packet.payload), packet.payload_size);
        }
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
#endif  // !SENTRY_BRIDGED

#if SENTRY_BRIDGED
// -- the cable, where a board with a radio has a broker ---------------------------------
//
// What goes down it is what would have gone to the broker: the same announcement, the same
// acknowledgements, the same readings, the same health, written by the same functions. Two
// things are different. There is no acknowledgement to wait for — the bridge is at the
// other end of a wire, and a frame written to an open port is a frame it has — so a
// message is finished when it is written. And there is no session to negotiate: a bridge
// says hello, and that is the connection.
//
// What is the same matters more than what is not. The lease still decides whether a
// reading may go, the spool still holds what cannot, an answer still goes before a
// reading, and a node told to stop still says goodbye and then nothing. None of that
// knows which of the two cables it is on.

bool the_bridge_is_here = false;
// Whether the port was open last time round, so that unplugging says so once rather than
// every turn of the loop.
bool the_port_was_open = false;

void the_cable_went_quiet(const char* why) {
  if (!the_bridge_is_here) return;
  the_bridge_is_here = false;
  media::stop(nullptr, why);
  bridge::forget();
  outgoing_size = 0;
  saying = Saying::kNothing;
  stopping = false;
  announced_this_connection = false;
  // Whatever was in flight was never carried anywhere, so it stays at the front of the
  // queue and goes again on the next connection, marked as arriving late.
  spool.link_lost();
  std::printf("# %s\n", why);
}

void a_bridge_said_hello() {
  // A new conversation, which is a new connection. Whatever the last one was in the middle
  // of saying belongs to it, and is said again to this one.
  the_cable_went_quiet("the bridge started again");
  if (!make_uuid(connection_id) || !prepare_the_connection()) {
    std::printf("# this node cannot describe a connection of its own\n");
    return;
  }
  the_bridge_is_here = true;
  announced_this_connection = false;
  owes_a_declaration = false;
  // The first thing after the announcement, rather than fifteen seconds of nothing.
  health_due_ms = monotonic_ms();
  std::printf("# a bridge is carrying this node, on connection %s\n", connection_id);
}

// `{"unix_ms": …}`, and nothing else. A board with no radio has no SNTP to ask, so this is
// the only thing that makes its readings stamped rather than uncertain — which is exactly
// why it is read with the contract's own reader and not with a search for a digit.
void the_time_arrived(const uint8_t* payload, size_t size) {
  sentry::Reader reader(reinterpret_cast<const char*>(payload), size);
  sentry::Span key;
  sentry::Kind kind = sentry::Kind::kEnd;
  int64_t unix_ms = 0;
  bool got = false;
  if (!reader.begin_object()) return;
  while (reader.member(key, kind)) {
    if (kind == sentry::Kind::kEnd) break;
    if (key.is("unix_ms") && kind == sentry::Kind::kNumber) {
      got = reader.integer(unix_ms);
    } else if (!reader.skip()) {
      return;
    }
  }
  if (!got || reader.failed()) {
    std::printf("# the bridge sent a time this node cannot read\n");
    return;
  }
  const bool first = !clock_.synced();
  const bool stepped = clock_.sync(unix_ms, now_us());
  time_answered_at_us = now_us();
  ++answers_seen;
  if (first) {
    char stamped[32] = {};
    if (sentry::write_timestamp(unix_ms, stamped, sizeof(stamped))) {
      std::printf("# the bridge says it is %s\n", stamped);
    }
  }
  if (stepped) the_clock_stepped();
}

// One frame out, carrying whatever is in the outgoing buffer. There is no acknowledgement
// coming, so what would have waited for one is let go of here.
bool send_what_is_waiting() {
  if (outgoing_size == 0) return false;
  sentry::Carries what = sentry::Carries::kEvents;
  switch (saying) {
    case Saying::kAnnouncement:
    case Saying::kFarewell:
      what = sentry::Carries::kState;
      break;
    case Saying::kAck:
      what = sentry::Carries::kAcks;
      break;
    case Saying::kHealth:
      what = sentry::Carries::kHealth;
      break;
    case Saying::kEvent:
    case Saying::kNothing:
      break;
  }
  if (!bridge::say(what, reinterpret_cast<const uint8_t*>(outgoing), outgoing_size)) {
    // No host, or a payload longer than a frame carries. Either way it is not gone, and
    // nothing here throws it away: the next turn tries again.
    return false;
  }
  if (saying == Saying::kAck) forget_the_first_ack();
  if (saying == Saying::kEvent) {
    spool.accepted();
    ++published_events;
  }
  if (saying == Saying::kAnnouncement) {
    if (announcement_opens_the_connection) std::printf("# online, as %s\n", node_id);
    announced_this_connection = true;
  }
  const bool said_goodbye = saying == Saying::kFarewell;
  outgoing_size = 0;
  saying = Saying::kNothing;
  if (said_goodbye) {
    // The last word this node had to say is across. What follows is silence, which on a
    // cable is what a DISCONNECT is on a broker: nothing more until a bridge says hello
    // again, whatever the lease still says.
    the_bridge_is_here = false;
    stopping = false;
    std::printf("# gone, as asked\n");
  }
  return true;
}

void serve_the_bridge() {
  const bool open = bridge::present();
  if (!open) {
    if (the_port_was_open) the_cable_went_quiet("the cable was unplugged");
    the_port_was_open = false;
    return;
  }
  the_port_was_open = true;

  // What arrived, a frame at a time. Console lines never reach here: they are taken inside
  // the port and handed to `getchar`, which is where a line somebody typed belongs.
  sentry::Frame frame;
  while (bridge::heard(frame)) {
    switch (frame.what) {
      case sentry::Carries::kHello:
        a_bridge_said_hello();
        break;
      case sentry::Carries::kTime:
        the_time_arrived(frame.payload, frame.size);
        break;
      case sentry::Carries::kCommands:
        if (!the_bridge_is_here) {
          // A command before a hello is a bridge that has not said which conversation this
          // is. Answering it would be answering on a connection that has no identifier.
          std::printf("# a command arrived before the bridge said hello\n");
          break;
        }
        a_command_arrived(reinterpret_cast<const char*>(frame.payload), frame.size);
        break;
      default:
        break;
    }
  }
  if (!the_bridge_is_here) return;

  // What to say, and in what order: the same ladder the broker half climbs, without the
  // session states a broker has and a cable does not.
  if (outgoing_size == 0) {
    const uint32_t now = monotonic_ms();
    if (!announced_this_connection) {
      write_announcement();
    } else if (owed_acks > 0) {
      write_the_first_ack();
    } else if (owes_a_declaration) {
      if (write_announcement()) owes_a_declaration = false;
    } else if (stopping) {
      write_the_farewell();
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
  send_what_is_waiting();
}
#endif  // SENTRY_BRIDGED

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
    if (!source.enabled) {
      std::printf("#   %s %s disabled\n", source.source_id, sentry::name_of(source.driver));
      continue;
    }
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
    } else if (source.driver == sentry::Driver::kBle) {
      char device[40] = {};
      if (source.ble.watched.by_address) {
        sentry::write_address(source.ble.watched.address, device, sizeof(device));
      } else {
        sentry::write_uuid(source.ble.watched.uuid, device, sizeof(device));
      }
      char loudness[16] = "none";
      if (live[index].watch.has_rssi()) {
        std::snprintf(loudness, sizeof(loudness), "%.0f", live[index].watch.rssi());
      }
      std::printf("#   %s ble %s %s rssi=%s sightings=%lu weak=%lu missed=%lu %sreadings=%lld\n",
                  source.source_id, device, sentry::name_of(live[index].watch.state()),
                  loudness, static_cast<unsigned long>(live[index].watch.sightings()),
                  static_cast<unsigned long>(live[index].watch.too_weak()),
                  static_cast<unsigned long>(ble::missed()),
                  ble::listening() ? "" : "not listening ",
                  static_cast<long long>(live[index].readings));
    } else if (source.driver == sentry::Driver::kMicrophone) {
      char loudness[16] = "none";
      if (live[index].activity.has_level()) {
        std::snprintf(loudness, sizeof(loudness), "%.1f", live[index].activity.level());
      }
      std::printf(
          "#   %s microphone pin=%d clock=%d %s level=%s %s blocks=%lu missed=%lu "
          "%sreadings=%lld\n",
          source.source_id, source.microphone.pin, source.microphone.clock_pin,
          source.microphone.left ? "left" : "right", loudness,
          live[index].activity.active() ? "loud" : "quiet",
          static_cast<unsigned long>(live[index].blocks),
          static_cast<unsigned long>(i2s::missed()), i2s::running() ? "" : "not listening ",
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
#if SENTRY_BRIDGED
  // The radio lines below would all say the same thing on this board — there is no radio —
  // so what is said instead is the cable: whether there is a host on it, whether a bridge
  // has said hello, and what the crossing has cost. A number that climbs here is a cable,
  // a driver or a board losing bytes, and it is the only place anybody would see it.
  const bridge::Counts crossed = bridge::counts();
  std::printf("# node=%s provisioned=%s cable=%s bridge=%s clock=%s\n", node_id,
              provisioned ? "yes" : "no", bridge::present() ? "open" : "nobody there",
              the_bridge_is_here ? "here" : "has not said hello",
              sentry::name_of(clock_.status_at(now_us())));
  std::printf("# frames sent=%lu unsent=%lu read=%lu discarded=%lu missed=%lu refused=%lu\n",
              static_cast<unsigned long>(crossed.sent),
              static_cast<unsigned long>(crossed.unsent),
              static_cast<unsigned long>(crossed.frames),
              static_cast<unsigned long>(crossed.discarded),
              static_cast<unsigned long>(crossed.missed),
              static_cast<unsigned long>(crossed.refused));
  std::printf("# heap_free=%lldkB uptime=%llus reset=%s queued=%lu coalesced=%lu dropped=%lu\n",
              static_cast<long long>(free_heap_kb()),
              static_cast<unsigned long long>(now_us() / 1000000), sentry::name_of(woke),
              static_cast<unsigned long>(spool.size()),
              static_cast<unsigned long>(spool.losses().coalesced),
              static_cast<unsigned long>(spool.losses().dropped_transitions));
#else
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
              sentry::name_of(clock_.status_at(now_us())),
              static_cast<unsigned long>(answers.count));
  std::printf("# heap_free=%lldkB uptime=%llus reset=%s\n",
              static_cast<long long>(free_heap_kb()),
              static_cast<unsigned long long>(now_us() / 1000000),
              sentry::name_of(woke));
  std::printf("# credentials=%s socket=%s mqtt=%s queued=%lu coalesced=%lu dropped=%lu\n",
              credentials.complete() ? "yes" : "no", socket_state, mqtt_state,
              static_cast<unsigned long>(spool.size()),
              static_cast<unsigned long>(spool.losses().coalesced),
              static_cast<unsigned long>(spool.losses().dropped_transitions));
#endif
  const uint64_t now_ms = now_us() / 1000;
  const media::Numbers sound = media::how_it_is_going();
  if (sound.stream_id != nullptr || sound.error != nullptr) {
    std::printf("# audio=%s%s%s blocks=%lu dropped=%lu connections=%lu%s%s\n", sound.state,
                sound.source_id != nullptr ? " from=" : "",
                sound.source_id != nullptr ? sound.source_id : "",
                static_cast<unsigned long>(sound.blocks),
                static_cast<unsigned long>(sound.dropped),
                static_cast<unsigned long>(sound.connections),
                sound.error != nullptr ? " last=" : "",
                sound.error != nullptr ? sound.error : "");
  }
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

// A board that has just been told it is a different node is not the node the plan it is
// running was written for, and the configuration in flash is addressed to a name this
// board no longer answers to. Both go: the pins are given back, the revision goes back to
// "nobody has told me", and the slot is emptied rather than left to be refused at every
// boot from here on. The new name starts with nothing, and is told what to run by the hub
// that now owns it.
void stop_being_the_node_that_was_configured() {
  stop_the_plan();
  running.clear();
  candidate.clear();
  config_revision = -1;
  uint32_t writes = 0;
  if (vault::holds(sentry::Held::kConfiguration, writes)) {
    vault::forget(sentry::Held::kConfiguration);
  }
  owes_a_declaration = true;
  std::printf("# this board has a new name: what the last one was running is not kept\n");
}

void take_provisioning(const char* document, bool keep) {
  sentry::Provisioning found;
  const size_t size = std::strlen(document);
  if (!sentry::read_provisioning(document, size, found)) {
    std::printf("# that is not a provisioning record this firmware can use\n");
    return;
  }
  const bool renamed = provisioned && std::strcmp(provisioning.node_id, found.node_id) != 0;
  provisioning = found;
  provisioned = true;
  if (renamed) stop_being_the_node_that_was_configured();
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
  sentry::Command& command = command_in_hand;
  sentry::Refusal why = sentry::Refusal::kNone;
  if (!sentry::parse_command(kept, size, node_id, command, why)) {
    if (why == sentry::Refusal::kNotForThisNode && provisioned) {
      // Addressed to a node this board is not. No boot from here on will read it any
      // differently, so it is dropped rather than refused again every morning; a board
      // whose identity slot did not come back is a different matter, and keeps it.
      std::printf("# the configuration in flash was left by another node: forgetting it\n");
      vault::forget(sentry::Held::kConfiguration);
      return;
    }
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
#if !SENTRY_BRIDGED
bool it_can_carry_on_by_itself() {
  return provisioned && provisioning.has_wifi() && credentials.complete();
}
#endif

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
  if (std::strcmp(line, "deafen") == 0) {
    // On purpose, like `tear` and `hang`: the scanner stops while something is still being
    // watched. What should follow is every watch going unknown at once — not absent, which
    // is a thing this node would have had to hear in order to know — and then, a few
    // seconds later, the loop asking for the radio again and the wait for absence starting
    // over. It is the only way to see coverage loss without unsoldering an antenna.
    ble::deaf();
    std::printf("# the scanner is off; the loop will ask for it again in about %lu ms\n",
                static_cast<unsigned long>(kRadioPatienceMs));
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
    const bool stepped = clock_.sync(unix_ms, now_us());
    std::printf("# time taken: %lld\n", static_cast<long long>(unix_ms));
    if (stepped) the_clock_stepped();
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
    sentry::Command& command = command_in_hand;
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
#if SENTRY_BRIDGED
    // The cable is the connection, and it is not this board's to hang up. What it can do
    // is the part that matters: say goodbye, and then say nothing until it is asked again.
    if (the_bridge_is_here) {
      stopping = true;
    } else {
      std::printf("# there is no bridge to say goodbye to\n");
    }
    return;
#else
    if (broker.link() == sentry::Link::kOffline) {
      end_the_connection("asked to disconnect");
      return;
    }
    // There is a connection to say goodbye on, so it is used: what is owed goes first,
    // then the retained state that says this node is gone, then the DISCONNECT.
    stopping = true;
    return;
#endif
  }
  if (line[0] != '\0') std::printf("# not a command: %s\n", line);
}

}  // namespace

int main() {
  stdio_init_all();
#if SENTRY_BRIDGED
  // Before anything is printed: on a board with one USB port and no radio, the port is the
  // cable to the bridge, and a line written to it unframed is a line a reader has to throw
  // away. From here on everything this program says goes out inside a frame.
  bridge::start();
#endif

  // Read before anything turns it on again, because turning it on is what clears it.
  woke = device::woke_because();

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
              sentry::name_of(woke));
  std::fflush(stdout);

#if !SENTRY_BRIDGED
  if (it_can_carry_on_by_itself()) {
    come_back_on_its_own = true;
    join_the_network();
  }
#endif

  // From here on the loop has to keep coming round. Everything before this point is the
  // one place where it does not — joining a network, reading flash and waiting for a
  // serial monitor are all allowed to take their time exactly once.
  watchdog_enable(kWatchdogMs, true);

  absolute_time_t next = make_timeout_time_ms(kHeartbeatBlinkMs);
  while (true) {
    watchdog_update();
    net::poll();
    take_the_time_if_it_arrived();
#if SENTRY_BRIDGED
    // No radio, so no broker: what this node has to say goes down the cable it is powered
    // by, and the machine at the other end carries it the rest of the way.
    serve_the_bridge();
#else
    if (net::socket() != net::Socket::kIdle) {
      // Whether or not this node still wants to be connected: a goodbye is said on the
      // connection it is about, and hanging up first would be the silence it is there to
      // avoid.
      serve_the_broker();
    } else if (wanted && static_cast<int32_t>(monotonic_ms() - next_attempt_ms) >= 0) {
      start_a_connection();
    }
#endif
    static char line[4096];
    if (read_line(line, sizeof(line))) obey(line);
    // Every pin, every loop: the debounce is measured in time and not in sleeping, so
    // nothing here waits on a wire while the connection waits on this.
    read_the_sources();
    // The second connection, after the sources rather than before: what it sends is what
    // they just produced, and a block finished this turn goes out this turn.
    media::serve(now_us() / 1000, credentials);
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
