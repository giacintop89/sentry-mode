#include "sentry/lease.h"

#include <cstring>

namespace sentry {

bool Lease::live(uint64_t now_ms) const {
  return held_ && capability_ == Capability::kEvents && now_ms < expires_at_;
}

void Lease::surrender() {
  held_ = false;
  capability_ = Capability::kNone;
  expires_at_ = 0;
  grant_id_[0] = '\0';
}

Decision Lease::apply(const Command& command, uint64_t now_ms) {
  Decision decision;
  switch (command.action) {
    case Action::kStop:
      decision.answer = Answer::kApplied;
      decision.detail = "stopping";
      decision.stop_requested = true;
      return decision;
    case Action::kRevoke:
      surrender();
      decision.answer = Answer::kApplied;
      return decision;
    case Action::kGrant:
    case Action::kRenew:
      break;
    default:
      // Media commands are the streaming code's business; they ride on this lease but they
      // are not it, and a lease that answered for them would be answering twice.
      decision.detail = "not a lease command";
      return decision;
  }

  if (command.grant_id[0] == '\0' || command.capability == Capability::kNone) {
    decision.detail = "a grant names a capability and has an id";
    return decision;
  }
  if (!(command.duration_seconds > 0)) {
    decision.detail = "a grant that has already expired is not a grant";
    return decision;
  }
  bool same = held_ && std::strcmp(grant_id_, command.grant_id) == 0;
  if (command.action == Action::kRenew) {
    if (!same) {
      decision.detail = "there is no such grant to renew";
      return decision;
    }
    if (command.sequence <= sequence_) {
      // An old renewal arriving late must not push the expiry backwards or forwards: the
      // hub has already sent a newer one, and this is the copy that took the long way.
      decision.detail = "a renewal that is not newer than the grant it renews";
      return decision;
    }
  }

  decision.newly_granted = !same;
  held_ = true;
  capability_ = command.capability;
  hub_epoch_ = command.hub_epoch;
  sequence_ = command.sequence;
  std::strncpy(grant_id_, command.grant_id, sizeof(grant_id_) - 1);
  grant_id_[sizeof(grant_id_) - 1] = '\0';
  uint64_t seconds = static_cast<uint64_t>(command.duration_seconds * 1000.0);
  expires_at_ = now_ms + seconds;
  decision.answer = Answer::kApplied;
  return decision;
}

const Answer* Answered::recall(const char* command_id) const {
  size_t held = size();
  for (size_t index = 0; index < held; ++index) {
    if (std::strcmp(ids_[index], command_id) == 0) return &answers_[index];
  }
  return nullptr;
}

void Answered::remember(const char* command_id, Answer answer) {
  const Answer* already = recall(command_id);
  if (already != nullptr) {
    // Answered before: the answer stands. Overwriting it would let a retry change what the
    // hub is told happened the first time.
    return;
  }
  size_t slot = count_ % kCapacity;
  std::strncpy(ids_[slot], command_id, kMaxIdText - 1);
  ids_[slot][kMaxIdText - 1] = '\0';
  answers_[slot] = answer;
  ++count_;
}

}  // namespace sentry
