// Print what the firmware's own serializers produce, one message per line.
//
// The firmware cannot run Pydantic and the hub cannot run the firmware, so this is where
// the two meet: `tools/check_against_contracts.py` runs this program and validates every
// line against the hub's models. A serializer that agrees with a schema it was written
// from proves less than one a second implementation has read.
//
//   sentry_emit           one event per line
//   sentry_emit control   `<message>\t<json>` per line: state, health, ack
//   sentry_emit topics    `<channel>\t<topic>` per line

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "sentry/control.h"
#include "sentry/event.h"
#include "sentry/topics.h"

using sentry::Clock;
using sentry::Delivery;
using sentry::Quality;
using sentry::Reading;
using sentry::Value;

namespace {

Reading base() {
  Reading reading;
  reading.event_id = "0f6c4a1e-9a5b-4c2d-8e11-5b7c9d0a1f23";
  reading.node_id = "pico-ingresso";
  reading.source_id = "pir-1";
  reading.boot_id = "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611";
  reading.sequence = 41;
  reading.kind = "sensor.motion";
  reading.occurred_at = "2026-09-16T21:04:07.512Z";
  reading.clock = Clock::kSynced;
  reading.value = Value::of(true);
  return reading;
}

Delivery link() {
  Delivery delivery;
  delivery.connection_id = "9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb";
  delivery.hub_epoch = 7;
  return delivery;
}

void emit(const Reading& reading, const Delivery& delivery) {
  char buffer[sentry::kMaxEventBytes];
  size_t size = sentry::write_event(reading, delivery, buffer, sizeof(buffer));
  if (size == 0) {
    std::fprintf(stderr, "refused to write an event that was meant to be written\n");
    std::exit(1);
  }
  std::fwrite(buffer, 1, size, stdout);
  std::fputc('\n', stdout);
}

}  // namespace

namespace {

void emit_control(const char* what, const char* buffer, size_t size) {
  if (size == 0) {
    std::fprintf(stderr, "refused to write a %s that was meant to be written\n", what);
    std::exit(1);
  }
  std::printf("%s\t", what);
  std::fwrite(buffer, 1, size, stdout);
  std::fputc('\n', stdout);
}

int control() {
  using sentry::Value;
  char buffer[2048];

  sentry::State state;
  state.node_id = "pico-ingresso";
  state.boot_id = "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611";
  state.connection_id = "9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb";
  state.online = true;
  state.firmware_version = "0.1.0";
  state.profile = "sensor-presence";
  state.config_revision = 3;
  sentry::DeclaredOption options[] = {
      {"pin", Value::of(static_cast<int64_t>(17))},
      {"debounce_ms", Value::of(static_cast<int64_t>(200))},
      {"zone", Value::of("entrance")},
      {"inverted", Value::of(false)},
      {"interval_seconds", Value::of(30.5, 1)},
      {"calibration", Value{}},
  };
  sentry::DeclaredSource sources[] = {
      {"pir-1", "gpio", true, options, 6},
      {"board-temperature", "board", true, nullptr, 0},
      {"door-1", "gpio", false, nullptr, 0},
  };
  state.sources = sources;
  state.source_count = 3;
  emit_control("state", buffer, sentry::write_state(state, buffer, sizeof(buffer)));

  // The same node, on a board with no radio: something else is carrying what it says, and
  // it is the node that says so. The hub's model has to take that word and no other.
  state.reached_by = "bridge";
  emit_control("state", buffer, sentry::write_state(state, buffer, sizeof(buffer)));
  state.reached_by = nullptr;

  char topic[sentry::kMaxTopicText] = {};
  if (!sentry::topic(sentry::kTopicPrefix, state.node_id, sentry::Channel::kState, topic,
                     sizeof(topic))) {
    return 1;
  }
  emit_control("state", buffer,
               sentry::write_goodbye(state.node_id, state.boot_id, state.connection_id,
                                     std::strlen(topic), buffer, sizeof(buffer)));

  sentry::Health health;
  health.node_id = "pico-ingresso";
  health.boot_id = state.boot_id;
  health.uptime_seconds = 61.0;
  health.clock = sentry::Clock::kUnsynced;
  health.queue.events = 3;
  health.queue.bytes = 512;
  emit_control("health", buffer, sentry::write_health(health, buffer, sizeof(buffer)));

  sentry::SourceHealth reported[2];
  reported[0].source_id = "pir-1";
  reported[0].readings = 12;
  reported[0].driver = "gpio";
  // What it last read, as health reports it: the same value, unit and quality the event
  // carried, so the hub can compare the two and find them the same measurement.
  reported[0].has_last = true;
  reported[0].last = sentry::Value::of(true);
  reported[0].quality = sentry::Quality::kValid;
  reported[0].has_age = true;
  reported[0].last_reading_age_seconds = 4.5;
  reported[1].source_id = "ds18b20-1";
  reported[1].readings = 0;
  reported[1].driver = "onewire";
  reported[1].error = "no sensor answered on the bus";
  // Nothing has been read here, so there is no `last` at all — not a null value with a
  // unit beside it, which is what a bus that answered with nothing would look like.
  health.clock = sentry::Clock::kSynced;
  health.queue.published = 40;
  health.queue.refused = 1;
  health.queue.drops_count = 2;
  health.queue.drops_total = 2;
  health.queue.granted = true;
  health.board.has_uptime = true;
  health.board.uptime_seconds = 61.0;
  health.board.has_temperature = true;
  health.board.temperature_c = 24.5;
  health.board.has_free_heap = true;
  health.board.memory_available_kb = 58;
  health.board.woke = sentry::Woke::kWatchdog;
  health.sources = reported;
  health.source_count = 2;
  emit_control("health", buffer, sentry::write_health(health, buffer, sizeof(buffer)));

  emit_control("ack", buffer,
               sentry::write_ack("3f1b7c0e-8d4a-4e2b-9f61-0a5c8d7e4b12", state.node_id,
                                 sentry::Outcome::kApplied, nullptr, buffer, sizeof(buffer)));
  emit_control("ack", buffer,
               sentry::write_ack("c-2", state.node_id, sentry::Outcome::kReceived, nullptr,
                                 buffer, sizeof(buffer)));
  emit_control("ack", buffer,
               sentry::write_ack("c-3", state.node_id, sentry::Outcome::kFailed,
                                 "pir-1: pin 17 is already taken by door-1", buffer,
                                 sizeof(buffer)));
  return 0;
}

int topics() {
  const sentry::Channel kChannels[] = {sentry::Channel::kEvents, sentry::Channel::kState,
                                       sentry::Channel::kHealth, sentry::Channel::kAcks,
                                       sentry::Channel::kCommands};
  for (sentry::Channel channel : kChannels) {
    char topic[sentry::kMaxTopicText] = {};
    if (!sentry::topic(sentry::kTopicPrefix, "pico-ingresso", channel, topic, sizeof(topic))) {
      return 1;
    }
    std::printf("%s\t%s\n", sentry::name_of(channel), topic);
  }
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc > 1 && std::strcmp(argv[1], "control") == 0) return control();
  if (argc > 1 && std::strcmp(argv[1], "topics") == 0) return topics();

  emit(base(), link());

  Reading contact = base();
  contact.source_id = "door-1";
  contact.kind = "sensor.contact";
  contact.sequence = 0;
  contact.value = Value::of(false);
  Delivery snapshot = link();
  snapshot.initial_state = true;
  emit(contact, snapshot);

  Reading temperature = base();
  temperature.source_id = "board-temperature";
  temperature.kind = "board.temperature";
  temperature.value = Value::of(42.25, 1);
  temperature.unit = "\xc2\xb0"
                     "C";
  emit(temperature, link());

  Reading heap = base();
  heap.source_id = "board-memory";
  heap.kind = "board.free_heap";
  heap.value = Value::of(static_cast<int64_t>(58112));
  heap.unit = "bytes";
  emit(heap, link());

  Reading beacon = base();
  beacon.source_id = "ble-1";
  beacon.kind = "presence.beacon";
  beacon.value = Value::of("away");
  beacon.quality = Quality::kDegraded;
  beacon.clock = Clock::kUnsynced;
  emit(beacon, link());

  Reading nothing = base();
  nothing.source_id = "adc-1";
  nothing.kind = "sensor.level";
  nothing.value = Value{};
  nothing.quality = Quality::kUnavailable;
  Delivery waited = link();
  waited.grant_id = "025a1b1f-2969-48c2-b14e-079c8b955f7c";
  waited.queued_ms = 4200;
  waited.replayed = true;
  emit(nothing, waited);

  // Every word in both vocabularies is written at least once, so that the check on the
  // other side judges the whole of what this node can say and not a sample of it.
  Reading unsure = base();
  unsure.source_id = "adc-2";
  unsure.kind = "sensor.level";
  unsure.value = Value::of(static_cast<int64_t>(3));
  unsure.quality = Quality::kUnknown;
  unsure.clock = Clock::kUnknown;
  emit(unsure, link());

  char stamped[32] = {};
  if (!sentry::write_timestamp(1789592647512, stamped, sizeof(stamped))) return 1;
  Reading timed = base();
  timed.occurred_at = stamped;
  timed.sequence = 42;
  emit(timed, link());
  return 0;
}
