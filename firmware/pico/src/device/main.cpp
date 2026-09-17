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

#include "sentry/client.h"
#include "sentry/control.h"
#include "sentry/credentials.h"
#include "sentry/event.h"
#include "sentry/identity.h"
#include "sentry/sensors.h"
#include "sentry/spool.h"
#include "sentry/timebase.h"
#include "sentry/topics.h"

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
sentry::Credentials credentials;
bool provisioned = false;
uint32_t answers_seen = 0;
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
char goodbye[sentry::kMaxWillBytes] = {};
sentry::mqtt::Will will;
sentry::mqtt::Connect connect_record;
sentry::mqtt::Client broker(connect_record, commands_topic);

// The queue between a reading and a broker. Sixteen events, and what it gives up is
// counted rather than hidden.
sentry::Spool spool;

// One publish is in flight at a time, so one buffer holds it: the same bytes are sent
// again if no acknowledgement arrives, because a retransmission is the same event.
char outgoing[sentry::kMaxEventBytes] = {};
size_t outgoing_size = 0;
bool outgoing_is_announcement = false;

// Bytes that have arrived and are not yet a whole packet.
uint8_t incoming[sentry::mqtt::kMaxPacketBytes * 2] = {};
size_t incoming_size = 0;

bool wanted = false;             // somebody asked this node to be connected
uint32_t next_attempt_ms = 0;    // and the backoff says not before this
bool announced_this_connection = false;
// The client is told a connection opened once per connection. It stays offline until the
// broker's CONNACK, so "it has not said it is connected" is not the same question.
bool client_told = false;

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

  static char buffer[sentry::kMaxEventBytes];
  const size_t size = sentry::write_event(reading, delivery, buffer, sizeof(buffer));
  if (size == 0) {
    std::printf("# refused to write that reading\n");
    return;
  }
  std::fwrite(buffer, 1, size, stdout);
  std::fputc('\n', stdout);
  std::fflush(stdout);

  // And into the queue, which is what a broker eventually reads from. A reading offered
  // while the link is down waits there; one the queue had no room for is counted rather
  // than forgotten quietly.
  if (provisioned && !spool.offer(reading, sentry::Kept::kPeriodic)) {
    std::printf("# the queue is full: %lu given up so far\n",
                static_cast<unsigned long>(spool.losses().refused));
  }
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

uint32_t monotonic_ms() { return static_cast<uint32_t>(now_us() / 1000); }

// The three topics this node uses and the goodbye the broker holds on its behalf. Built
// when a record arrives, because every one of them is made out of the node's own name.
bool prepare_the_connection() {
  if (!provisioned) return false;
  if (!sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kState, state_topic,
                     sizeof(state_topic)) ||
      !sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kEvents, events_topic,
                     sizeof(events_topic)) ||
      !sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kCommands, commands_topic,
                     sizeof(commands_topic))) {
    return false;
  }
  const size_t size = sentry::write_goodbye(node_id, boot_id, connection_id,
                                            std::strlen(state_topic), goodbye, sizeof(goodbye));
  if (size == 0) return false;
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
  sentry::State here;
  here.node_id = node_id;
  here.boot_id = boot_id;
  here.connection_id = connection_id;
  here.online = true;
  here.firmware_version = "pico-0.1.0";
  here.profile = "sensor-presence";
  outgoing_size = sentry::write_state(here, outgoing, sizeof(outgoing));
  outgoing_is_announcement = true;
  return outgoing_size > 0;
}

// The oldest reading still waiting, written once and kept until the broker acknowledges
// it: a retransmission is the same bytes and the same event id, not a second reading.
bool write_the_front_of_the_queue() {
  sentry::Reading reading;
  bool replayed = false;
  if (!spool.front(reading, replayed)) return false;
  sentry::Delivery delivery;
  delivery.connection_id = connection_id;
  delivery.hub_epoch = 0;  // nothing has granted this node anything yet
  delivery.replayed = replayed;
  outgoing_size = sentry::write_event(reading, delivery, outgoing, sizeof(outgoing));
  outgoing_is_announcement = false;
  return outgoing_size > 0;
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
    std::printf("# %s\n", net::name_of(dialled));
    next_attempt_ms = monotonic_ms() + 5000;
    return;
  }
  client_told = false;
  std::printf("# connecting to %s:%u\n", provisioning.mqtt_host,
              static_cast<unsigned>(provisioning.mqtt_port));
}

void end_the_connection(const char* why) {
  if (why != nullptr && why[0] != '\0') std::printf("# %s\n", why);
  net::hang_up();
  broker.closed();
  incoming_size = 0;
  outgoing_size = 0;
  announced_this_connection = false;
  client_told = false;
  // Whatever was in flight was never acknowledged, so it stays at the front of the queue
  // and goes again on the next connection, marked as arriving late.
  spool.link_lost();
  next_attempt_ms = monotonic_ms() + broker.backoff_ms();
}

// One command from the hub. Answering it is PICO-03's other half — the lease and the ACK —
// and until that is here, saying what arrived is better than pretending nothing did.
void a_command_arrived(const sentry::mqtt::Incoming& packet) {
  std::printf("# a command arrived (%u bytes); answering them is not built yet\n",
              static_cast<unsigned>(packet.payload_size));
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
        break;
      case sentry::mqtt::Effect::kDelivered:
        // The broker has it. Only now does the queue let go of it.
        spool.accepted();
        outgoing_size = 0;
        break;
      case sentry::mqtt::Effect::kRefused:
        end_the_connection("the broker refused this connection");
        return;
      case sentry::mqtt::Effect::kNothing:
        if (packet.type == sentry::mqtt::Type::kPublish) a_command_arrived(packet);
        break;
    }
  }

  // What to say. The announcement comes first because nothing else may go before it.
  if (outgoing_size == 0) {
    if (broker.session().ready_to_announce() && !announced_this_connection) {
      if (write_announcement()) announced_this_connection = true;
    } else if (broker.session().may_publish_events()) {
      write_the_front_of_the_queue();
    }
  }

  sentry::mqtt::Pending pending;
  pending.topic = outgoing_is_announcement ? state_topic : events_topic;
  pending.payload = reinterpret_cast<const uint8_t*>(outgoing);
  pending.size = outgoing_size;
  pending.retain = outgoing_is_announcement;
  pending.announcement = outgoing_is_announcement;

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
    if (!outgoing_is_announcement) spool.accepted();
    outgoing_size = 0;
    return;
  }
  if (size == 0) return;
  if (net::send(packet, size) != size) {
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
  std::printf("# credentials=%s socket=%s mqtt=%s queued=%lu coalesced=%lu dropped=%lu\n",
              credentials.complete() ? "yes" : "no", socket_state, mqtt_state,
              static_cast<unsigned long>(spool.size()),
              static_cast<unsigned long>(spool.losses().coalesced),
              static_cast<unsigned long>(spool.losses().dropped_transitions));
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
    next_attempt_ms = monotonic_ms();
    return;
  }
  if (std::strcmp(line, "disconnect") == 0) {
    wanted = false;
    end_the_connection("asked to disconnect");
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
    if (wanted) {
      if (net::socket() == net::Socket::kIdle) {
        if (static_cast<int32_t>(monotonic_ms() - next_attempt_ms) >= 0) start_a_connection();
      } else {
        serve_the_broker();
      }
    }
    static char line[4096];
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
