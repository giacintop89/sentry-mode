// Writing an event the hub will accept, into a buffer decided in advance.
//
// This is the C++ half of `contracts/satellite/v1/event.schema.json`. A reading becomes an
// envelope here and nowhere else, so there is one place where the shape of what this node
// says is decided, and one place for the tests to point at.
//
// What the event says happened is separate from how this copy of it reached the hub. A
// reading that waited in the queue and a reading that went straight out describe the same
// moment; only `delivery` differs, which is what lets the hub tell them apart without the
// node rewriting what it measured.

#ifndef SENTRY_EVENT_H
#define SENTRY_EVENT_H

#include <cstddef>
#include <cstdint>

namespace sentry {

// The largest an event may be, from the budget in the plan. A reading that will not fit is
// refused with a counter rather than sent in halves.
inline constexpr size_t kMaxEventBytes = 2048;

// The hub's vocabulary, from sources/models.py: a reading is trusted, trusted less, not
// there at all, or of a trustworthiness this node cannot tell.
enum class Quality { kValid, kDegraded, kUnavailable, kUnknown };
enum class Clock { kSynced, kUnsynced, kUnknown };

// What a reading is worth. `kNone` is a reading with no value, which is not the same as a
// reading of zero and is written as null.
struct Value {
  enum class Type { kNone, kBoolean, kInteger, kNumber, kText } type = Type::kNone;
  bool boolean = false;
  int64_t integer = 0;
  double number = 0.0;
  const char* text = nullptr;
  int decimals = 1;  // how many places a number is written with

  static Value of(bool value);
  static Value of(int64_t value);
  static Value of(double value, int decimals = 1);
  static Value of(const char* value);
};

struct Reading {
  const char* event_id = nullptr;   // a UUID, made once and kept across retransmissions
  const char* node_id = nullptr;
  const char* source_id = nullptr;  // the name on this node, never node.source
  const char* boot_id = nullptr;
  int64_t sequence = 0;
  const char* kind = nullptr;       // a family and a name, as in sensor.motion
  const char* occurred_at = nullptr;  // RFC 3339 with an offset, never a local guess
  Clock clock = Clock::kUnknown;
  Value value;
  const char* unit = nullptr;
  Quality quality = Quality::kValid;
};

struct Delivery {
  const char* connection_id = nullptr;
  int64_t hub_epoch = 0;
  const char* grant_id = nullptr;  // null when the node is not holding one
  int64_t queued_ms = 0;
  bool replayed = false;
  bool initial_state = false;
};

// Write the envelope into `buffer`. Returns how many bytes it took, or 0 if it did not fit
// or a field was not one the contract allows.
size_t write_event(const Reading& reading, const Delivery& delivery, char* buffer,
                   size_t capacity);

// The time, as the contract spells it: 2026-09-17T09:30:00Z, from a UTC estimate in
// milliseconds since the epoch. False if the estimate is not a time this node may claim.
bool write_timestamp(int64_t unix_ms, char* out, size_t capacity);

const char* name_of(Quality quality);
const char* name_of(Clock clock);

}  // namespace sentry

#endif  // SENTRY_EVENT_H
