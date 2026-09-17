// A wire that is either high or low, and what may be said about it.
//
// Three rules live here, and none of them is about electricity.
//
// The first reading after a boot, a grant or a reconfiguration is a baseline: it says what
// the door is, not that it just moved. A contact that was open when the node started did
// not open when the node started.
//
// A PIR needs a while after power to mean anything. During that window the level is not
// evidence, so no transition comes out of it and what is reported says the quality is
// unknown — which is not the same as reporting no motion.
//
// And a level has to hold still before it is believed. Bounce on a contact would otherwise
// be a burst of intrusions, and the hub would be right to act on every one of them.

#ifndef SENTRY_INPUT_H
#define SENTRY_INPUT_H

#include <cstdint>

#include "sentry/event.h"

namespace sentry {

// The limits a configuration is held to. They are here rather than in the hub because the
// node is what has to survive the number.
inline constexpr uint32_t kMaxDebounceMs = 5000;
inline constexpr uint32_t kMaxSettleMs = 300000;

struct InputConfig {
  // Whether a high level means the thing is happening. A contact wired to ground with a
  // pull-up reads low when it is closed, and nothing in software can guess which it is.
  bool active_high = true;
  uint32_t debounce_ms = 50;
  // How long after power this input means anything. A PIR wants tens of seconds; a
  // contact wants none.
  uint32_t settle_ms = 0;
};

// What, if anything, to publish for a sample.
enum class Report {
  kNothing,
  kBaseline,    // what this input is, sent with initial_state
  kTransition,  // what just happened
};

class DigitalInput {
 public:
  // False, and nothing changed, if the configuration is outside the limits above.
  bool configure(const InputConfig& config);

  // Start again: the next report is a baseline. Called at boot, when a grant arrives, and
  // when the configuration changes, because in all three the hub knows nothing yet.
  void rearm(uint64_t now_ms);

  // Take one reading of the pin. `now_ms` is this node's monotonic clock.
  Report sample(bool level, uint64_t now_ms);

  // The logical state, with the polarity already applied.
  bool state() const { return state_; }

  // Unknown while the input is still settling: the wire has a level, but the sensor behind
  // it does not yet have an opinion, and saying `valid` would be inventing one.
  Quality quality(uint64_t now_ms) const;

  bool settled(uint64_t now_ms) const;

 private:
  InputConfig config_;
  bool started_ = false;
  uint64_t started_at_ = 0;
  bool state_ = false;
  bool candidate_ = false;
  uint64_t candidate_since_ = 0;
  bool baseline_sent_ = false;
};

}  // namespace sentry

#endif  // SENTRY_INPUT_H
