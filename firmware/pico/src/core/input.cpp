#include "sentry/input.h"

namespace sentry {

bool DigitalInput::configure(const InputConfig& config) {
  if (config.debounce_ms > kMaxDebounceMs) return false;
  if (config.settle_ms > kMaxSettleMs) return false;
  config_ = config;
  return true;
}

void DigitalInput::rearm(uint64_t now_ms) {
  started_ = true;
  started_at_ = now_ms;
  baseline_sent_ = false;
  candidate_since_ = now_ms;
}

bool DigitalInput::settled(uint64_t now_ms) const {
  if (!started_) return false;
  return now_ms - started_at_ >= config_.settle_ms;
}

Quality DigitalInput::quality(uint64_t now_ms) const {
  return settled(now_ms) ? Quality::kValid : Quality::kUnknown;
}

Report DigitalInput::sample(bool level, uint64_t now_ms) {
  bool logical = config_.active_high ? level : !level;
  if (!started_) rearm(now_ms);

  if (!baseline_sent_) {
    // The baseline is what the input is, taken as it is: debouncing it would mean waiting
    // to tell the hub anything at all, and there is nothing yet to compare it against.
    state_ = logical;
    candidate_ = logical;
    candidate_since_ = now_ms;
    baseline_sent_ = true;
    return Report::kBaseline;
  }

  if (logical != candidate_) {
    candidate_ = logical;
    candidate_since_ = now_ms;
  }
  if (logical == state_) return Report::kNothing;
  if (now_ms - candidate_since_ < config_.debounce_ms) return Report::kNothing;

  state_ = logical;
  // Settling is about the sensor, not the wire: until it is over, a change is not evidence
  // of anything and is kept rather than published.
  if (!settled(now_ms)) return Report::kNothing;
  return Report::kTransition;
}

}  // namespace sentry
