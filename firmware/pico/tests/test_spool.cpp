// A queue that has to choose what to lose, and says what it lost.

#include <cstdio>
#include <cstring>
#include <string>

#include "harness.h"
#include "sentry/spool.h"

using sentry::Kept;
using sentry::Reading;
using sentry::Spool;
using sentry::Value;

namespace {

Reading a_reading(const char* source, const char* kind, int64_t sequence,
                  const char* occurred_at = "2026-09-16T21:04:07.512Z") {
  Reading reading;
  reading.event_id = "0f6c4a1e-9a5b-4c2d-8e11-5b7c9d0a1f23";
  reading.node_id = "pico-ingresso";
  reading.source_id = source;
  reading.boot_id = "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611";
  reading.sequence = sequence;
  reading.kind = kind;
  reading.occurred_at = occurred_at;
  reading.value = Value::of(true);
  return reading;
}

// The clock these tests queue against. Most of them do not care what it says — the wait a
// reading reports is one test of its own, below — so they all take things in at the same
// moment and read nothing into the number.
constexpr uint64_t kAtOnce = 1000;

void fill_with_transitions(Spool& spool, size_t how_many) {
  for (size_t index = 0; index < how_many; ++index) {
    spool.offer(a_reading("door-1", "sensor.contact", static_cast<int64_t>(index)),
                Kept::kTransition, kAtOnce);
  }
}

}  // namespace

TEST(a_reading_waits_until_the_broker_says_it_arrived) {
  // A successful publish is not an acknowledgement, and an event dropped on the way out
  // would be a reading nobody ever knows was taken.
  Spool spool;
  CHECK(spool.offer(a_reading("pir-1", "sensor.motion", 1), Kept::kTransition, kAtOnce));
  Reading front;
  bool replayed = true;
  CHECK(spool.front(front, replayed));
  CHECK(!replayed);
  CHECK(spool.size() == 1);
  CHECK(spool.front(front, replayed));  // still there: nothing acknowledged it
  CHECK(spool.size() == 1);
  spool.accepted();
  CHECK(spool.empty());
  CHECK(!spool.front(front, replayed));
}

TEST(a_retry_is_the_same_event_and_not_a_second_one) {
  Spool spool;
  spool.offer(a_reading("pir-1", "sensor.motion", 1), Kept::kTransition, kAtOnce);
  Reading first;
  Reading again;
  bool replayed = false;
  CHECK(spool.front(first, replayed));
  std::string id(first.event_id);
  CHECK(spool.front(again, replayed));
  CHECK(id == again.event_id);
  CHECK(first.sequence == again.sequence);
}

TEST(periodic_readings_from_one_source_collapse_into_the_newest) {
  Spool spool;
  fill_with_transitions(spool, 0);
  for (int64_t index = 0; index < static_cast<int64_t>(sentry::kSpoolCapacity) + 4; ++index) {
    CHECK(spool.offer(a_reading("board-temperature", "board.temperature", index),
                      Kept::kPeriodic, kAtOnce));
  }
  CHECK(spool.size() == sentry::kSpoolCapacity);
  CHECK(spool.losses().coalesced == 4);
  CHECK(spool.losses().dropped_transitions == 0);
  Reading front;
  bool replayed = false;
  CHECK(spool.front(front, replayed));
  CHECK(front.sequence == 4);  // the four oldest went, the rest are in order
}

TEST(a_temperature_never_pushes_out_a_door_opening) {
  Spool spool;
  CHECK(spool.offer(a_reading("door-1", "sensor.contact", 1), Kept::kTransition, kAtOnce));
  for (int64_t index = 0; index < static_cast<int64_t>(sentry::kSpoolCapacity) * 2; ++index) {
    spool.offer(a_reading("board-temperature", "board.temperature", index), Kept::kPeriodic, kAtOnce);
  }
  Reading front;
  bool replayed = false;
  CHECK(spool.front(front, replayed));
  CHECK_TEXT(front.source_id, "door-1");
  CHECK(spool.losses().dropped_transitions == 0);
}

TEST(when_only_transitions_are_left_a_periodic_reading_is_refused_rather_than_kept) {
  Spool spool;
  fill_with_transitions(spool, sentry::kSpoolCapacity);
  CHECK(spool.size() == sentry::kSpoolCapacity);
  CHECK(!spool.offer(a_reading("board-temperature", "board.temperature", 99), Kept::kPeriodic, kAtOnce));
  CHECK(spool.losses().refused == 1);
  CHECK(spool.losses().dropped_transitions == 0);
}

TEST(a_transition_that_is_lost_is_counted_and_never_told_later) {
  Spool spool;
  fill_with_transitions(spool, sentry::kSpoolCapacity + 2);
  CHECK(spool.size() == sentry::kSpoolCapacity);
  CHECK(spool.losses().dropped_transitions == 2);
  Reading front;
  bool replayed = false;
  CHECK(spool.front(front, replayed));
  CHECK(front.sequence == 2);  // the two that were lost are gone, not queued behind
}

TEST(an_event_held_across_a_disconnection_arrives_late_rather_than_fresh) {
  Spool spool;
  spool.offer(a_reading("door-1", "sensor.contact", 1, "2026-09-16T20:00:00.000Z"),
              Kept::kTransition, kAtOnce);
  spool.link_lost();
  Reading front;
  bool replayed = false;
  CHECK(spool.front(front, replayed));
  CHECK(replayed);
  CHECK_TEXT(front.occurred_at, "2026-09-16T20:00:00.000Z");  // its own moment, not now

  // What is taken after the link is back is not historic, and is not marked as if it were.
  spool.accepted();
  spool.offer(a_reading("door-1", "sensor.contact", 2), Kept::kTransition, kAtOnce);
  CHECK(spool.front(front, replayed));
  CHECK(!replayed);
}

TEST(a_reading_keeps_its_own_value_and_not_the_next_ones) {
  Spool spool;
  Reading beacon = a_reading("ble-1", "presence.beacon", 1);
  beacon.value = Value::of("away");
  beacon.unit = nullptr;
  CHECK(spool.offer(beacon, Kept::kTransition, kAtOnce));
  Reading measured = a_reading("board-temperature", "board.temperature", 2);
  measured.value = Value::of(42.25, 1);
  measured.unit = "\xc2\xb0"
                  "C";
  CHECK(spool.offer(measured, Kept::kPeriodic, kAtOnce));

  Reading front;
  bool replayed = false;
  CHECK(spool.front(front, replayed));
  CHECK(front.value.type == Value::Type::kText);
  CHECK_TEXT(front.value.text, "away");
  CHECK(front.unit == nullptr);
  spool.accepted();
  CHECK(spool.front(front, replayed));
  CHECK(front.value.type == Value::Type::kNumber);
  CHECK(front.value.number > 42.2 && front.value.number < 42.3);
  CHECK_TEXT(front.unit, "\xc2\xb0" "C");
}

TEST(a_baseline_travels_marked_and_is_not_replaced_by_the_next_reading) {
  // The hub asked where this source stands. What comes back must say that it is a
  // baseline, or a contact found closed becomes a contact that just closed; and a
  // periodic reading taken a moment later must not quietly take its place.
  Spool spool;
  CHECK(spool.offer(a_reading("door-1", "sensor.contact", 1), Kept::kTransition, kAtOnce, true));
  CHECK(spool.offer(a_reading("door-1", "sensor.contact", 2), Kept::kPeriodic, kAtOnce));
  CHECK(spool.size() == 2);

  Reading front;
  bool replayed = false;
  bool initial = false;
  CHECK(spool.front(front, replayed, &initial));
  CHECK(initial);
  CHECK(front.sequence == 1);
  spool.accepted();

  initial = true;
  CHECK(spool.front(front, replayed, &initial));
  CHECK(!initial);
  CHECK(front.sequence == 2);
}

TEST(a_field_too_long_for_the_queue_is_refused_and_not_cut_down) {
  Spool spool;
  std::string long_name(sentry::kMaxSourceText + 4, 'a');
  CHECK(!spool.offer(a_reading(long_name.c_str(), "sensor.motion", 1), Kept::kTransition, kAtOnce));
  CHECK(spool.losses().refused == 1);
  CHECK(spool.empty());
}

TEST(a_clock_that_stepped_takes_back_what_is_queued_says_about_itself) {
  // Two readings taken while this node was sure of the time, then an hour appearing from
  // nowhere. The moments they were taken at are still the moments they were taken at, and
  // the strings say so; what cannot survive is either of them calling itself synced.
  Spool spool;
  Reading first = a_reading("door-1", "sensor.contact", 1, "2026-09-17T22:03:06.258Z");
  first.clock = sentry::Clock::kSynced;
  Reading second = a_reading("board-temperature", "board.temperature", 2,
                             "2026-09-17T22:03:11.257Z");
  second.clock = sentry::Clock::kSynced;
  CHECK(spool.offer(first, Kept::kTransition, kAtOnce));
  CHECK(spool.offer(second, Kept::kPeriodic, kAtOnce));

  spool.clock_stepped();

  Reading front;
  bool replayed = false;
  CHECK(spool.front(front, replayed));
  CHECK(front.clock == sentry::Clock::kUnknown);
  CHECK(std::strcmp(front.occurred_at, "2026-09-17T22:03:06.258Z") == 0);
  spool.accepted();
  CHECK(spool.front(front, replayed));
  CHECK(front.clock == sentry::Clock::kUnknown);
  CHECK(std::strcmp(front.occurred_at, "2026-09-17T22:03:11.257Z") == 0);

  // And a reading taken after it is as good as the new offset is, which is what the node
  // was just told.
  Reading later = a_reading("door-1", "sensor.contact", 3, "2026-09-17T22:03:20.100Z");
  later.clock = sentry::Clock::kSynced;
  spool.accepted();
  CHECK(spool.offer(later, Kept::kTransition, kAtOnce));
  CHECK(spool.front(front, replayed));
  CHECK(front.clock == sentry::Clock::kSynced);
}

int main() { return harness::run_all("spool"); }

TEST(a_reading_says_how_long_it_waited_here) {
  // The one number in an event that is about this node rather than about the world. The
  // queue is the only thing that knows it: by the time the envelope is written, the moment
  // the reading was taken is a timestamp and the moment it was queued is gone unless it
  // was kept.
  Spool spool;
  CHECK(spool.offer(a_reading("pir-1", "sensor.motion", 1), Kept::kTransition, 4000));
  Reading front;
  bool replayed = false;
  uint64_t queued_at = 0;
  CHECK(spool.front(front, replayed, nullptr, &queued_at));
  CHECK(queued_at == 4000);
  // And a caller that does not ask still gets the reading, because most of them do not.
  CHECK(spool.front(front, replayed));
}

TEST(a_reading_that_replaced_another_waited_from_when_it_arrived) {
  // Coalescing is one reading standing in for a series, not one reading inheriting
  // another's history: what is waiting is the newest temperature, and it has been waiting
  // since it was taken. Saying otherwise would report an outage that the reading being
  // sent was never in.
  Spool spool;
  for (size_t index = 0; index < 15; ++index) {
    CHECK(spool.offer(a_reading("door-1", "sensor.contact", static_cast<int64_t>(index)),
                      Kept::kTransition, 1000));
  }
  CHECK(spool.offer(a_reading("board-temperature", "board.temperature", 1), Kept::kPeriodic, 2000));
  CHECK(spool.offer(a_reading("board-temperature", "board.temperature", 2), Kept::kPeriodic, 9000));
  CHECK(spool.losses().coalesced == 1);

  // Drain the transitions; what is left is the temperature that replaced the other one.
  for (size_t index = 0; index < 15; ++index) spool.accepted();
  Reading front;
  bool replayed = false;
  uint64_t queued_at = 0;
  CHECK(spool.front(front, replayed, nullptr, &queued_at));
  CHECK(front.sequence == 2);
  CHECK(queued_at == 9000);
}

TEST(a_link_that_dropped_does_not_change_how_long_anything_has_waited) {
  // A reading held through an outage is the same reading, and the wait it reports is the
  // whole of it — from when it was taken in, not from when the link came back. That is the
  // number that tells a hub how far behind a node was.
  Spool spool;
  CHECK(spool.offer(a_reading("door-1", "sensor.contact", 1), Kept::kTransition, 500));
  spool.link_lost();
  Reading front;
  bool replayed = false;
  uint64_t queued_at = 0;
  CHECK(spool.front(front, replayed, nullptr, &queued_at));
  CHECK(replayed);
  CHECK(queued_at == 500);
}
