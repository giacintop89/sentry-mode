// Print events built by the firmware's own serializer, one per line.
//
// The firmware cannot run Pydantic and the hub cannot run the firmware, so this is where
// the two meet: `tools/check_against_contracts.py` runs this program and validates every
// line against the hub's models. A serializer that agrees with a schema it was written
// from proves less than one a second implementation has read.

#include <cstdio>
#include <cstdlib>

#include "sentry/event.h"

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

int main() {
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
