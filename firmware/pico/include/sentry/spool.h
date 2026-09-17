// What to keep when the link is down, and what to give up.
//
// The first profile queues in RAM and not in flash: a node that wrote every reading to
// flash would wear it out to protect events nobody would miss. So the queue is small and
// has to choose, and the choice is not "the newest wins".
//
// A periodic reading is one of a series — the board temperature every minute. Losing one
// costs nothing, because another is coming, so several of them from the same source
// collapse into the most recent. A transition is a thing that happened once: a door
// opened. Nothing is coming to replace it, and it is never dropped to make room for a
// temperature. When even the transitions will not fit, the oldest is lost and counted, and
// a lost transition is never presented later as a new one.
//
// An event kept across a disconnection keeps its own timestamp and its own id: it is the
// same reading arriving late, marked `replayed`, and not a fresh one.

#ifndef SENTRY_SPOOL_H
#define SENTRY_SPOOL_H

#include <cstddef>
#include <cstdint>

#include "sentry/event.h"

namespace sentry {

// Sixteen events of roughly 200 bytes: about 3 kB of a 264 kB part, which buys a short
// outage without pretending to buy a long one.
inline constexpr size_t kSpoolCapacity = 16;
inline constexpr size_t kMaxValueText = 33;
inline constexpr size_t kMaxUnitText = 17;
inline constexpr size_t kMaxKindText = 41;
inline constexpr size_t kMaxSourceText = 41;
inline constexpr size_t kMaxTimestampText = 33;
inline constexpr size_t kMaxUuidText = 37;

// Whether another reading like this one is coming.
enum class Kept { kPeriodic, kTransition };

// What the node gives up, so that health can say it rather than hide it. Anything not
// measured is unknown; these are measured.
struct Losses {
  uint32_t coalesced = 0;             // periodic readings replaced by a newer one
  uint32_t dropped_transitions = 0;   // things that happened and will never be told
  uint32_t refused = 0;               // readings the queue had no room for at all
};

class Spool {
 public:
  // Take a reading into the queue. False when it was not kept, which is counted.
  //
  // A baseline is where a source stands, taken because a hub has just granted this node
  // and needs to know before it can tell news from the way things already were. It is kept
  // like a transition, and it travels marked, so that a door found open is not published
  // as a door that just opened.
  bool offer(const Reading& reading, Kept kept, bool initial = false);

  // The oldest reading still waiting, pointing into the spool's own storage. It stays
  // there until `accepted()`: a publish that was never acknowledged is retried with the
  // same event id rather than becoming a second event.
  bool front(Reading& out, bool& replayed, bool* initial = nullptr) const;

  // The broker acknowledged the front. Only now is it gone.
  void accepted();

  // The link dropped. Everything held from here is an event arriving late, and says so.
  void link_lost();

  size_t size() const { return count_; }
  bool empty() const { return count_ == 0; }
  const Losses& losses() const { return losses_; }

 private:
  struct Entry {
    char event_id[kMaxUuidText] = {};
    char node_id[kMaxSourceText] = {};
    char source_id[kMaxSourceText] = {};
    char boot_id[kMaxUuidText] = {};
    char kind[kMaxKindText] = {};
    char occurred_at[kMaxTimestampText] = {};
    char unit[kMaxUnitText] = {};
    char text[kMaxValueText] = {};
    bool has_unit = false;
    int64_t sequence = 0;
    Value value;
    Clock clock = Clock::kUnknown;
    Quality quality = Quality::kValid;
    Kept kept = Kept::kPeriodic;
    bool replayed = false;
    bool initial = false;
  };

  Entry& at(size_t index) { return entries_[(first_ + index) % kSpoolCapacity]; }
  const Entry& at(size_t index) const { return entries_[(first_ + index) % kSpoolCapacity]; }
  void remove(size_t index);
  bool make_room(const Reading& reading, Kept kept);

  Entry entries_[kSpoolCapacity];
  size_t first_ = 0;
  size_t count_ = 0;
  Losses losses_;
};

}  // namespace sentry

#endif  // SENTRY_SPOOL_H
