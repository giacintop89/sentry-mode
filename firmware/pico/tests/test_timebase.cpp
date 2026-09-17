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

int main() { return harness::run_all("timebase"); }
