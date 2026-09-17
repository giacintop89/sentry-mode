// How long this node has been running, and what it may claim about the time.

#include "harness.h"
#include "sentry/timebase.h"

using sentry::Clock;
using sentry::Ticks;
using sentry::Timebase;

TEST(a_counter_that_wraps_is_seen_as_one_that_does_not) {
  Ticks ticks;
  CHECK(ticks.extend(10) == 10);
  CHECK(ticks.extend(0xFFFFFFF0u) == 0xFFFFFFF0u);
  uint64_t after = ticks.extend(5);  // round it went
  CHECK(after == (UINT64_C(1) << 32) + 5);
  CHECK(ticks.extend(6) == (UINT64_C(1) << 32) + 6);
}

TEST(a_node_that_has_not_been_told_the_time_does_not_invent_one) {
  Timebase time;
  int64_t stamped = -1;
  CHECK(!time.synced());
  CHECK(!time.unix_ms(1000, stamped));
  CHECK(time.status_at(1000) == Clock::kUnsynced);
}

TEST(once_the_time_is_known_a_moment_becomes_a_timestamp) {
  Timebase time;
  time.sync(1789592647512, 5'000'000);
  int64_t stamped = 0;
  CHECK(time.unix_ms(5'000'000, stamped) && stamped == 1789592647512);
  CHECK(time.unix_ms(6'500'000, stamped) && stamped == 1789592649012);
  CHECK(time.status_at(6'500'000) == Clock::kSynced);
}

TEST(a_reading_taken_before_the_sync_gets_its_real_moment_but_not_the_word_synced) {
  // Working the timestamp out backwards is correct; calling it fresh is not, and the hub
  // decides what is live from the word rather than from the number.
  Timebase time;
  time.sync(1789592647512, 5'000'000);
  int64_t stamped = 0;
  CHECK(time.unix_ms(2'000'000, stamped) && stamped == 1789592644512);
  CHECK(time.status_at(2'000'000) == Clock::kUnsynced);
}

TEST(when_the_time_source_goes_away_the_node_stops_claiming_to_know) {
  Timebase time;
  time.sync(1789592647512, 5'000'000);
  time.lost();
  int64_t stamped = 0;
  CHECK(time.unix_ms(9'000'000, stamped));  // still the best estimate there is
  CHECK(time.status_at(9'000'000) == Clock::kUnknown);
  time.sync(1789592700000, 10'000'000);
  CHECK(time.status_at(10'000'000) == Clock::kSynced);
}

TEST(a_sync_that_would_put_this_node_before_1970_is_not_a_time) {
  Timebase time;
  time.sync(1000, 900'000'000);
  int64_t stamped = 0;
  CHECK(!time.unix_ms(0, stamped));
}

TEST(a_reading_from_a_callback_does_not_send_this_node_into_the_future) {
  // The SNTP answer is stamped inside lwIP's callback and read on the next turn of the
  // loop, by which time the loop has already extended a later reading. Handing that older
  // value to `extend` would look like the counter going round: seventy-one minutes, once,
  // in whichever direction nobody was looking.
  sentry::Ticks ticks;
  CHECK(ticks.extend(1000) == 1000);
  CHECK(ticks.extend(5000) == 5000);
  CHECK(ticks.just_before(4000) == 4000);
  // And the extender has not moved: the next real reading is still where it should be.
  CHECK(ticks.extend(6000) == 6000);
}

TEST(a_reading_from_before_the_counter_went_round_is_put_before_it) {
  sentry::Ticks ticks;
  CHECK(ticks.extend(0xFFFFFF00u) == 0xFFFFFF00u);
  const uint64_t after = ticks.extend(0x00000100u);
  CHECK(after == (UINT64_C(1) << 32) + 0x100u);
  // A callback that ran just before the wrap, read just after it.
  CHECK(ticks.just_before(0xFFFFFFF0u) == 0xFFFFFFF0u);
  CHECK(ticks.just_before(0x50u) == (UINT64_C(1) << 32) + 0x50u);
}

TEST(a_reading_from_before_the_counter_ever_went_round_is_itself) {
  sentry::Ticks ticks;
  CHECK(ticks.extend(900) == 900);
  CHECK(ticks.just_before(1000) == 1000);
  CHECK(ticks.just_before(800) == 800);
}

TEST(the_first_time_this_node_is_told_the_time_is_not_a_step) {
  sentry::Timebase clock;
  // Nothing to move from, and everything stamped before it was unsynced anyway.
  CHECK(!clock.sync(1'700'000'000'000, 1'000'000));
  CHECK(clock.moved_by_us() == 0);
}

TEST(a_correction_is_not_a_step_and_a_step_is) {
  sentry::Timebase clock;
  CHECK(!clock.sync(1'700'000'000'000, 1'000'000));
  // A second later by the counter, and 1,200 ms later by the world: the crystal is slow,
  // which is what a correction corrects.
  CHECK(!clock.sync(1'700'000'001'200, 2'000'000));
  CHECK(clock.moved_by_us() == 200'000);
  // An hour, which no crystal loses between two answers. A second of the counter has gone
  // by as well, and the second answer was 1,200 ms after the first: the hour is what is
  // left once both of those are accounted for, which is what an offset does.
  CHECK(clock.sync(1'700'000'002'200 + 3'600'000, 3'000'000));
  CHECK(clock.moved_by_us() == 3'600'000'000);
  // And back, which is the same thing happening the other way.
  CHECK(clock.sync(1'700'000'003'200, 4'000'000));
  CHECK(clock.moved_by_us() == -3'600'000'000);
  // What it says the time is, is whatever it was told last: the step is a warning about
  // what came before it, not a reason to disbelieve what came with it.
  int64_t now = 0;
  CHECK(clock.unix_ms(4'000'000, now));
  CHECK(now == 1'700'000'003'200);
  CHECK(clock.status_at(4'000'000) == sentry::Clock::kSynced);
}

int main() { return harness::run_all("timebase"); }
