#include "sentry/timebase.h"

namespace sentry {

uint64_t Ticks::extend(uint32_t raw) {
  if (raw < last_) high_ += UINT64_C(1) << 32;  // the counter went round
  last_ = raw;
  return high_ | raw;
}

uint64_t Ticks::just_before(uint32_t raw) const {
  if (raw <= last_) return high_ | raw;
  // Before the last time the counter went round. If it never has, there is no earlier
  // period to put this in and the best answer is the raw value itself.
  if (high_ == 0) return raw;
  return (high_ - (UINT64_C(1) << 32)) | raw;
}

bool Timebase::sync(int64_t unix_ms, uint64_t monotonic_us) {
  const int64_t offset = unix_ms * 1000 - static_cast<int64_t>(monotonic_us);
  // How far the world moved, not how far this node's counter did: the offset is what turns
  // one into the other, so a change in it is the whole of what was wrong before.
  moved_by_us_ = synced_ ? offset - offset_us_ : 0;
  const bool stepped = moved_by_us_ > kClockStepUs || moved_by_us_ < -kClockStepUs;
  offset_us_ = offset;
  synced_at_us_ = monotonic_us;
  synced_ = true;
  lost_ = false;
  return stepped;
}

void Timebase::lost() { lost_ = true; }

bool Timebase::unix_ms(uint64_t monotonic_us, int64_t& out) const {
  if (!synced_) return false;
  int64_t microseconds = offset_us_ + static_cast<int64_t>(monotonic_us);
  if (microseconds < 0) return false;  // before 1970, which is not a moment this node had
  out = microseconds / 1000;
  return true;
}

Clock Timebase::status_at(uint64_t monotonic_us) const {
  if (!synced_) return Clock::kUnsynced;
  // The time source went away: this node can still stamp, but it can no longer say how
  // close the stamp is to the truth, and unknown is the honest word for that.
  if (lost_) return Clock::kUnknown;
  return monotonic_us >= synced_at_us_ ? Clock::kSynced : Clock::kUnsynced;
}

}  // namespace sentry
