// What time it is here, and how sure this node is about that.
//
// Two different questions live in this file. The first is how long the board has been
// running, which it always knows and which never goes backwards. The second is what that
// corresponds to in the world, which it does not know until something tells it.
//
// A reading taken before the node learned the time still has a real moment: once the time
// is known, the moment can be worked out from the monotonic difference, and the timestamp
// written for it is correct. What must never happen is that reading being called `synced`.
// That is the difference between a timestamp and a claim about a timestamp, and the hub
// decides what counts as live from the second one.

#ifndef SENTRY_TIMEBASE_H
#define SENTRY_TIMEBASE_H

#include <cstdint>

#include "sentry/event.h"

namespace sentry {

// A counter that wraps, seen as one that does not. Callers read the hardware and hand the
// raw value here; two reads more than 2^32 units apart cannot be told apart by anyone, so
// the contract is that this is called more often than the counter wraps.
class Ticks {
 public:
  uint64_t extend(uint32_t raw);

  // A raw reading taken a moment ago, from an interrupt or a callback, which must not be
  // allowed to move the extender: handing an older value to `extend` would look exactly
  // like the counter going round and would put this node seventy-one minutes into the
  // future. Anything newer than the last reading is taken as being just before it, because
  // a reading from the past is what this is for.
  uint64_t just_before(uint32_t raw) const;

 private:
  uint32_t last_ = 0;
  uint64_t high_ = 0;
};

class Timebase {
 public:
  // The wall time at a monotonic moment, from whatever told us: SNTP, or the bridge.
  void sync(int64_t unix_ms, uint64_t monotonic_us);

  // The source of time stopped answering. The offset is kept, because it is still the best
  // estimate this node has, but nothing it stamps from here on is called synced.
  void lost();

  // False before the first sync: there is no answer to give, and zero is not one.
  bool unix_ms(uint64_t monotonic_us, int64_t& out) const;

  // What a reading taken at that moment may claim about its own timestamp.
  Clock status_at(uint64_t monotonic_us) const;

  bool synced() const { return synced_; }

 private:
  bool synced_ = false;
  bool lost_ = false;
  int64_t offset_us_ = 0;
  uint64_t synced_at_us_ = 0;
};

}  // namespace sentry

#endif  // SENTRY_TIMEBASE_H
