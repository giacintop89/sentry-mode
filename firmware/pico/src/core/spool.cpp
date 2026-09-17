#include "sentry/spool.h"

#include <cstring>

namespace sentry {
namespace {

// Copy into a fixed field, refusing rather than truncating: a source id cut in half is a
// different source, and the hub would happily create it.
bool copy(char* out, size_t capacity, const char* text) {
  if (text == nullptr) return false;
  size_t length = std::strlen(text);
  if (length + 1 > capacity) return false;
  std::memcpy(out, text, length + 1);
  return true;
}

}  // namespace

void Spool::remove(size_t index) {
  for (size_t at = index; at + 1 < count_; ++at) {
    entries_[(first_ + at) % kSpoolCapacity] = entries_[(first_ + at + 1) % kSpoolCapacity];
  }
  --count_;
}

bool Spool::make_room(const Reading& reading, Kept kept) {
  if (count_ < kSpoolCapacity) return true;

  // A newer reading from the same source replaces the older one: that is what coalescing
  // is, and it is why a temperature every minute cannot push a door open out of the queue.
  for (size_t index = 0; index < count_; ++index) {
    const Entry& held = at(index);
    if (held.kept == Kept::kPeriodic && std::strcmp(held.source_id, reading.source_id) == 0 &&
        std::strcmp(held.kind, reading.kind) == 0) {
      remove(index);
      ++losses_.coalesced;
      return true;
    }
  }
  // Still no room: any periodic reading goes before any transition does.
  for (size_t index = 0; index < count_; ++index) {
    if (at(index).kept == Kept::kPeriodic) {
      remove(index);
      ++losses_.coalesced;
      return true;
    }
  }
  // Nothing but transitions. A new periodic is not worth one of them.
  if (kept == Kept::kPeriodic) {
    ++losses_.refused;
    return false;
  }
  remove(0);
  ++losses_.dropped_transitions;
  return true;
}

bool Spool::offer(const Reading& reading, Kept kept) {
  if (reading.source_id == nullptr || reading.kind == nullptr) {
    ++losses_.refused;
    return false;
  }
  if (!make_room(reading, kept)) return false;

  Entry entry;
  bool copied = copy(entry.event_id, sizeof(entry.event_id), reading.event_id) &&
                copy(entry.node_id, sizeof(entry.node_id), reading.node_id) &&
                copy(entry.source_id, sizeof(entry.source_id), reading.source_id) &&
                copy(entry.boot_id, sizeof(entry.boot_id), reading.boot_id) &&
                copy(entry.kind, sizeof(entry.kind), reading.kind) &&
                copy(entry.occurred_at, sizeof(entry.occurred_at), reading.occurred_at);
  if (!copied) {
    ++losses_.refused;
    return false;
  }
  if (reading.unit != nullptr) {
    if (!copy(entry.unit, sizeof(entry.unit), reading.unit)) {
      ++losses_.refused;
      return false;
    }
    entry.has_unit = true;
  }
  entry.value = reading.value;
  entry.value.text = nullptr;  // the text lives in the entry, and the entry is about to move
  if (reading.value.type == Value::Type::kText &&
      !copy(entry.text, sizeof(entry.text), reading.value.text)) {
    ++losses_.refused;
    return false;
  }
  entry.sequence = reading.sequence;
  entry.clock = reading.clock;
  entry.quality = reading.quality;
  entry.kept = kept;
  entry.replayed = false;

  size_t slot = (first_ + count_) % kSpoolCapacity;
  entries_[slot] = entry;
  ++count_;
  return true;
}

bool Spool::front(Reading& out, bool& replayed) const {
  if (count_ == 0) return false;
  const Entry& held = at(0);
  out = Reading{};
  out.event_id = held.event_id;
  out.node_id = held.node_id;
  out.source_id = held.source_id;
  out.boot_id = held.boot_id;
  out.sequence = held.sequence;
  out.kind = held.kind;
  out.occurred_at = held.occurred_at;
  out.clock = held.clock;
  out.value = held.value;
  if (held.value.type == Value::Type::kText) out.value.text = held.text;
  out.unit = held.has_unit ? held.unit : nullptr;
  out.quality = held.quality;
  replayed = held.replayed;
  return true;
}

void Spool::accepted() {
  if (count_ == 0) return;
  first_ = (first_ + 1) % kSpoolCapacity;
  --count_;
}

void Spool::link_lost() {
  for (size_t index = 0; index < count_; ++index) at(index).replayed = true;
}

}  // namespace sentry
