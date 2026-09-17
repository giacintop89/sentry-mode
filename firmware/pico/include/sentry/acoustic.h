// Whether something was loud, and nothing else about it.
//
// A microphone on a satellite is the source people are right to be nervous about, so this
// file is deliberately the whole of what the sound becomes: a level in dB full scale, and
// a boolean that says the level stayed above a threshold for long enough. No audio is kept,
// none is published, and nothing here can tell a voice from a door.
//
// The rules are the Linux agent's, in `audio/source.py`, because a hub must not be able to
// tell which kind of node an `audio.activity` came from: loud for `min_seconds` starts it,
// quiet for `hold_seconds` ends it, and the quiet has to be `kHysteresisDb` below the
// threshold rather than a hair under it — a level sitting exactly on the line would
// otherwise start and end an event several times a second.
//
// Nothing here has seen a microphone. It takes blocks of signed 16-bit samples and time in
// seconds, which is what makes the whole of it testable on a host with a synthetic signal.

#ifndef SENTRY_ACOUSTIC_H
#define SENTRY_ACOUSTIC_H

#include <cstddef>
#include <cstdint>

namespace sentry {

// What silence is called. A block of zeroes has no logarithm, and a number like -300 dB
// would be arithmetic rather than a measurement.
inline constexpr double kSilenceDbfs = -96.0;

// How far below the threshold a level has to fall before the quiet counts.
inline constexpr double kHysteresisDb = 6.0;

// One sample in four is enough to tell loud from quiet, and it is what the agent measures,
// so the two report the same number for the same sound.
inline constexpr size_t kLevelStride = 4;

// What a configuration may ask for, which is what the agent's own options allow.
inline constexpr double kLeastThresholdDbfs = -90.0;
inline constexpr double kMostThresholdDbfs = -1.0;
inline constexpr double kLeastMinSeconds = 0.1;
inline constexpr double kMostMinSeconds = 10.0;
inline constexpr double kLeastHoldSeconds = 0.5;
inline constexpr double kMostHoldSeconds = 120.0;

// The level of a block of samples, in dB full scale, rounded to a tenth — the agent's
// number for the same block. An empty block is silence rather than an error: a capture
// that produced nothing is quiet, and there is no third answer to give.
double dbfs(const int16_t* samples, size_t count);

struct Loudness {
  double threshold_dbfs = -35.0;
  double min_seconds = 0.3;
  double hold_seconds = 3.0;
};

// Loud for long enough starts it; quiet for long enough ends it. Nothing in between.
class Activity {
 public:
  enum class Change { kNothing, kStarted, kEnded };

  // False, and nothing changed, if the configuration is outside the limits above.
  bool configure(const Loudness& how);

  // One block's level, and how long that block was. The state when it changes.
  Change step(double level_dbfs, double seconds);

  bool active() const { return active_; }
  // The last level measured, for health. A source that has never been read has none.
  bool has_level() const { return has_level_; }
  double level() const { return level_; }
  // How long the current side of the threshold has lasted, for somebody wondering why
  // nothing has been reported yet.
  double loud_seconds() const { return loud_; }
  double quiet_seconds() const { return quiet_; }

 private:
  Loudness how_;
  bool active_ = false;
  bool has_level_ = false;
  double level_ = kSilenceDbfs;
  double loud_ = 0.0;
  double quiet_ = 0.0;
};

}  // namespace sentry

#endif  // SENTRY_ACOUSTIC_H
