// The permission to speak, and how long it lasts.
//
// A node may say it is alive before the hub has granted it anything. It may not publish
// events until a grant arrives, and it stops the moment that grant runs out — measured on
// its own monotonic clock, not on the hub still being reachable. That is the point of the
// lease: a node that has lost the network stops reporting on its own, rather than filling
// a queue with readings nobody authorised and nobody will act on.
//
// Everything here is timed in milliseconds from a 64-bit monotonic counter. A 32-bit
// millisecond timer wraps after 49 days, and a lease that is renewed across the wrap would
// be either immortal or instantly dead, depending on which way the subtraction went.

#ifndef SENTRY_LEASE_H
#define SENTRY_LEASE_H

#include <cstdint>

#include "sentry/command.h"

namespace sentry {

enum class Answer { kApplied, kFailed, kRepeated };

// The outcome of one command, and whether this is the node's first grant in this session.
struct Decision {
  Answer answer = Answer::kFailed;
  const char* detail = nullptr;
  bool newly_granted = false;  // the node owes the hub a baseline of every source
  bool stop_requested = false;
};

class Lease {
 public:
  // Take one command into account at `now_ms` on the monotonic clock.
  Decision apply(const Command& command, uint64_t now_ms);

  // Whether this node may publish events right now.
  bool live(uint64_t now_ms) const;

  const char* grant_id() const { return held_ ? grant_id_ : nullptr; }
  int64_t hub_epoch() const { return hub_epoch_; }
  uint64_t expires_at() const { return expires_at_; }

  // Forget the permission without being told: what a dropped link means.
  void surrender();

 private:
  bool held_ = false;
  char grant_id_[kMaxIdText] = {};
  Capability capability_ = Capability::kNone;
  int64_t hub_epoch_ = 0;
  int64_t sequence_ = 0;
  uint64_t expires_at_ = 0;
};

// The commands already answered, so a retransmission is answered again but not acted on
// twice. A hub that retries because an ack was lost must not be able to extend a lease by
// doing it, which is what an unremembered command id would allow.
class Answered {
 public:
  static constexpr size_t kCapacity = 64;

  // The answer given before, or nullptr if this command has not been seen.
  const Answer* recall(const char* command_id) const;
  void remember(const char* command_id, Answer answer);
  size_t size() const { return count_ < kCapacity ? count_ : kCapacity; }

 private:
  char ids_[kCapacity][kMaxIdText] = {};
  Answer answers_[kCapacity] = {};
  size_t count_ = 0;  // how many have ever been remembered; the ring is count_ % kCapacity
};

}  // namespace sentry

#endif  // SENTRY_LEASE_H
