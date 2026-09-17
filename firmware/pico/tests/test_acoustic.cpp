// What a microphone is allowed to conclude, driven by a signal nobody recorded.
//
// Every case here is a block of samples made by arithmetic: a tone at a known amplitude,
// silence, a level sitting exactly on the threshold. The point is that none of it needs a
// microphone — the whole judgement is here, and what the board adds is the wire.

#include <cmath>
#include <cstdint>
#include <vector>

#include "harness.h"
#include "sentry/acoustic.h"

using sentry::Activity;
using sentry::dbfs;
using sentry::kHysteresisDb;
using sentry::kSilenceDbfs;
using sentry::Loudness;

namespace {

// A sine at a fraction of full scale. Its RMS is the amplitude over root two, so a tone at
// full scale measures about -3 dBFS and not 0: that is what a sine is, and a test that
// expected 0 would be testing arithmetic nobody does.
std::vector<int16_t> a_tone(double of_full_scale, size_t samples) {
  std::vector<int16_t> block(samples);
  for (size_t index = 0; index < samples; ++index) {
    const double turn = 2.0 * 3.14159265358979323846 * static_cast<double>(index) / 16.0;
    block[index] = static_cast<int16_t>(of_full_scale * 32767.0 * std::sin(turn));
  }
  return block;
}

Activity an_activity(double threshold, double min_seconds, double hold_seconds) {
  Activity activity;
  Loudness how;
  how.threshold_dbfs = threshold;
  how.min_seconds = min_seconds;
  how.hold_seconds = hold_seconds;
  activity.configure(how);
  return activity;
}

bool about(double value, double expected, double slack = 0.6) {
  return std::fabs(value - expected) <= slack;
}

}  // namespace

TEST(silence_is_silence_and_not_minus_infinity) {
  std::vector<int16_t> nothing(256, 0);
  CHECK(dbfs(nothing.data(), nothing.size()) == kSilenceDbfs);
  // A block that is not there at all is quiet rather than an error: a capture that
  // produced nothing has nothing to say, and there is no third answer.
  CHECK(dbfs(nullptr, 0) == kSilenceDbfs);
  CHECK(dbfs(nothing.data(), 0) == kSilenceDbfs);
}

TEST(a_tone_measures_what_a_tone_measures) {
  const std::vector<int16_t> loud = a_tone(1.0, 256);
  // Full scale, as a sine: root two below the peak.
  CHECK(about(dbfs(loud.data(), loud.size()), -3.0));

  const std::vector<int16_t> half = a_tone(0.5, 256);
  CHECK(about(dbfs(half.data(), half.size()), -9.0));

  const std::vector<int16_t> quiet = a_tone(0.01, 256);
  CHECK(about(dbfs(quiet.data(), quiet.size()), -43.0));

  // Louder is a larger number, always. The one property somebody reading a threshold on a
  // page is entitled to assume.
  CHECK(dbfs(loud.data(), loud.size()) > dbfs(half.data(), half.size()));
  CHECK(dbfs(half.data(), half.size()) > dbfs(quiet.data(), quiet.size()));
}

TEST(a_level_is_a_tenth_of_a_decibel_and_no_more) {
  const std::vector<int16_t> tone = a_tone(0.3, 256);
  const double level = dbfs(tone.data(), tone.size());
  CHECK(std::fabs(level * 10.0 - std::round(level * 10.0)) < 1e-9);
}

TEST(one_loud_block_is_not_an_event) {
  Activity activity = an_activity(-35.0, 1.0, 3.0);
  CHECK(activity.step(-20.0, 0.25) == Activity::Change::kNothing);
  CHECK(activity.step(-20.0, 0.25) == Activity::Change::kNothing);
  CHECK(activity.step(-20.0, 0.25) == Activity::Change::kNothing);
  CHECK(!activity.active());
  // A second of it is.
  CHECK(activity.step(-20.0, 0.25) == Activity::Change::kStarted);
  CHECK(activity.active());
  // And it does not start twice.
  CHECK(activity.step(-20.0, 1.0) == Activity::Change::kNothing);
}

TEST(a_noise_that_stops_and_starts_does_not_add_up_to_an_event) {
  Activity activity = an_activity(-35.0, 1.0, 3.0);
  for (int burst = 0; burst < 10; ++burst) {
    CHECK(activity.step(-20.0, 0.5) == Activity::Change::kNothing);
    CHECK(activity.step(-60.0, 0.5) == Activity::Change::kNothing);
  }
  CHECK(!activity.active());
}

TEST(quiet_for_long_enough_ends_it_and_less_does_not) {
  Activity activity = an_activity(-35.0, 1.0, 3.0);
  CHECK(activity.step(-20.0, 1.0) == Activity::Change::kStarted);
  CHECK(activity.step(-60.0, 2.0) == Activity::Change::kNothing);
  // A loud block in the middle of the quiet starts the wait again rather than shortening
  // it: an event that ended while something was still making a noise would be a lie.
  CHECK(activity.step(-20.0, 0.5) == Activity::Change::kNothing);
  CHECK(activity.step(-60.0, 2.0) == Activity::Change::kNothing);
  CHECK(activity.active());
  CHECK(activity.step(-60.0, 1.0) == Activity::Change::kEnded);
  CHECK(!activity.active());
}

TEST(a_level_sitting_on_the_threshold_does_not_flicker) {
  // Just under the threshold and inside the hysteresis band: not loud enough to start
  // anything, not quiet enough to end anything. Ten minutes of it is still nothing.
  Activity activity = an_activity(-35.0, 1.0, 3.0);
  for (int block = 0; block < 1200; ++block) {
    CHECK(activity.step(-35.0 - kHysteresisDb / 2.0, 0.5) == Activity::Change::kNothing);
  }
  CHECK(!activity.active());
  CHECK(activity.quiet_seconds() == 0.0);
  CHECK(activity.loud_seconds() == 0.0);

  // An event that has started does not end in that band either.
  CHECK(activity.step(-20.0, 1.0) == Activity::Change::kStarted);
  for (int block = 0; block < 100; ++block) {
    CHECK(activity.step(-35.0 - kHysteresisDb / 2.0, 0.5) == Activity::Change::kNothing);
  }
  CHECK(activity.active());
  // Properly quiet, and it ends.
  CHECK(activity.step(-90.0, 3.0) == Activity::Change::kEnded);
}

TEST(a_threshold_nobody_could_mean_is_refused) {
  Activity activity;
  Loudness how;
  how.threshold_dbfs = 12.0;  // above full scale: nothing can ever be that loud
  CHECK(!activity.configure(how));
  how.threshold_dbfs = -200.0;  // below what silence is called
  CHECK(!activity.configure(how));
  how.threshold_dbfs = -35.0;
  how.min_seconds = 0.0;  // a start with no duration is every block a start
  CHECK(!activity.configure(how));
  how.min_seconds = 0.3;
  how.hold_seconds = 1000.0;
  CHECK(!activity.configure(how));
  how.hold_seconds = 3.0;
  CHECK(activity.configure(how));
}

TEST(a_microphone_that_has_never_been_read_has_no_level) {
  Activity activity = an_activity(-35.0, 1.0, 3.0);
  CHECK(!activity.has_level());
  activity.step(-42.5, 0.5);
  CHECK(activity.has_level());
  CHECK(activity.level() == -42.5);
}

TEST(a_tone_read_through_the_whole_of_it_becomes_an_event) {
  // The only test here that goes from samples to a state: half a second of blocks of a
  // tone at a hundredth of full scale, which is about -43 dBFS, against a threshold below
  // it — and then silence.
  Activity activity = an_activity(-50.0, 0.3, 0.5);
  const std::vector<int16_t> tone = a_tone(0.01, 1600);  // a tenth of a second at 16 kHz
  const std::vector<int16_t> nothing(1600, 0);
  Activity::Change change = Activity::Change::kNothing;
  for (int block = 0; block < 3 && change == Activity::Change::kNothing; ++block) {
    change = activity.step(dbfs(tone.data(), tone.size()), 0.1);
  }
  CHECK(change == Activity::Change::kStarted);
  change = Activity::Change::kNothing;
  for (int block = 0; block < 5 && change == Activity::Change::kNothing; ++block) {
    change = activity.step(dbfs(nothing.data(), nothing.size()), 0.1);
  }
  CHECK(change == Activity::Change::kEnded);
}

int main() { return harness::run_all("acoustic"); }
