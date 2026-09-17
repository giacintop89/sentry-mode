#include "sentry/acoustic.h"

#include <cmath>

namespace sentry {

double dbfs(const int16_t* samples, size_t count) {
  if (samples == nullptr || count == 0) return kSilenceDbfs;
  // Summed as a double rather than as an integer: a long block of full-scale samples
  // overflows a 32-bit sum, and a level that wraps to a small number is a microphone that
  // reports silence at exactly the moment something happened.
  double square_sum = 0.0;
  size_t taken = 0;
  for (size_t index = 0; index < count; index += kLevelStride) {
    const double sample = static_cast<double>(samples[index]);
    square_sum += sample * sample;
    ++taken;
  }
  if (taken == 0) return kSilenceDbfs;
  const double rms = std::sqrt(square_sum / static_cast<double>(taken));
  // The floor is not cosmetic: log10 of zero is not a number, and one sample of 1 in a
  // block of silence is not evidence of anything.
  const double level = 20.0 * std::log10((rms > 1e-9 ? rms : 1e-9) / 32768.0);
  const double rounded = std::round(level * 10.0) / 10.0;
  return rounded < kSilenceDbfs ? kSilenceDbfs : rounded;
}

bool Activity::configure(const Loudness& how) {
  if (!(how.threshold_dbfs >= kLeastThresholdDbfs) ||
      !(how.threshold_dbfs <= kMostThresholdDbfs)) {
    return false;
  }
  if (!(how.min_seconds >= kLeastMinSeconds) || !(how.min_seconds <= kMostMinSeconds)) {
    return false;
  }
  if (!(how.hold_seconds >= kLeastHoldSeconds) || !(how.hold_seconds <= kMostHoldSeconds)) {
    return false;
  }
  how_ = how;
  return true;
}

Activity::Change Activity::step(double level_dbfs, double seconds) {
  has_level_ = true;
  level_ = level_dbfs;
  if (level_dbfs >= how_.threshold_dbfs) {
    loud_ += seconds;
    quiet_ = 0.0;
  } else if (level_dbfs < how_.threshold_dbfs - kHysteresisDb) {
    quiet_ += seconds;
    loud_ = 0.0;
  } else {
    // In the band between the threshold and the hysteresis: not loud enough to start
    // anything, not quiet enough to end anything. The loud run is broken all the same, so
    // that a level hovering on the line never adds up to an event.
    loud_ = 0.0;
  }
  if (!active_ && loud_ >= how_.min_seconds) {
    active_ = true;
    return Change::kStarted;
  }
  if (active_ && quiet_ >= how_.hold_seconds) {
    active_ = false;
    return Change::kEnded;
  }
  return Change::kNothing;
}

}  // namespace sentry
