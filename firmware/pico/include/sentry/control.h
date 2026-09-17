// The three control messages this node writes: what it is, how it is, and what it did.
//
// The fourth, the command, it only ever reads (see command.h). These are the C++ half of
// `contracts/satellite/v1/control/*.schema.json`, and the same rule as events applies: a
// message is checked before a byte of it is written, so a refusal costs nothing and no
// buffer is left holding most of a message.
//
// The goodbye deserves its own function. The lwIP MQTT client writes the will's topic and
// payload with 8-bit lengths, so the whole of a goodbye — topic included — has to fit in
// 255 bytes. That is why everything after `online` is optional in the contract, and why
// the compact form is a separate call rather than a flag: a will that did not fit would
// not fail here, it would fail at CONNECT, on a board, months later.

#ifndef SENTRY_CONTROL_H
#define SENTRY_CONTROL_H

#include <cstddef>
#include <cstdint>

#include "sentry/event.h"

namespace sentry {

// What the lwIP MQTT client can express in a will, topic and payload together.
inline constexpr size_t kMaxWillBytes = 255;
// The hub's MAX_SOURCES, and the agent's own limit.
inline constexpr size_t kMaxDeclaredSources = 32;
inline constexpr size_t kMaxOptionsPerSource = 8;
// The hub's MAX_DETAIL.
inline constexpr size_t kMaxDetailText = 256;

struct DeclaredOption {
  const char* name = nullptr;  // a driver option: pin, interval_seconds, zone
  Value value;
};

struct DeclaredSource {
  const char* source_id = nullptr;
  const char* kind = nullptr;  // the driver: gpio, board, onewire
  bool enabled = true;
  const DeclaredOption* options = nullptr;
  size_t option_count = 0;
};

struct State {
  const char* node_id = nullptr;
  const char* boot_id = nullptr;
  const char* connection_id = nullptr;
  bool online = true;
  const char* firmware_version = nullptr;  // written as agent_version: one field, two names
  const char* profile = nullptr;
  int64_t config_revision = -1;  // below zero means this node is not saying
  const DeclaredSource* sources = nullptr;
  size_t source_count = 0;
};

// The retained snapshot on `state`.
size_t write_state(const State& state, char* buffer, size_t capacity);

// The will: who is leaving, which boot and which connection, and that it is gone. Refuses
// to write one that would not fit beside `topic_bytes` in the will the client can send.
size_t write_goodbye(const char* node_id, const char* boot_id, const char* connection_id,
                     size_t topic_bytes, char* buffer, size_t capacity);

struct QueueHealth {
  int64_t events = 0;
  int64_t bytes = 0;
  int64_t published = 0;
  int64_t refused = 0;
  int64_t drops_count = 0;
  int64_t drops_bytes = 0;
  int64_t drops_age = 0;
  int64_t drops_total = 0;
  bool granted = false;
};

// Every measurement is optional, and a board that cannot measure something says nothing
// about it. Absent is unknown; zero is a measurement, and writing one for the other is how
// a dashboard ends up showing a temperature nobody took.
struct BoardHealth {
  bool has_uptime = false;
  double uptime_seconds = 0.0;
  bool has_temperature = false;
  double temperature_c = 0.0;
  bool has_free_heap = false;
  int64_t memory_available_kb = 0;
};

struct SourceHealth {
  const char* source_id = nullptr;
  int64_t readings = 0;
  const char* driver = nullptr;
  const char* error = nullptr;  // null when there is nothing wrong
  // What it last measured, and how long ago. Both are left out rather than sent as zero
  // when there has been nothing: a source that has never read is not a source that read
  // nothing, and a page cannot tell those apart from a number.
  bool has_last = false;
  Value last;
  const char* unit = nullptr;  // null for a reading that has no unit, like a wire's state
  Quality quality = Quality::kValid;
  bool has_age = false;
  double last_reading_age_seconds = 0.0;
};

struct Health {
  const char* node_id = nullptr;
  const char* boot_id = nullptr;
  double uptime_seconds = 0.0;
  Clock clock = Clock::kUnknown;
  QueueHealth queue;
  BoardHealth board;
  const SourceHealth* sources = nullptr;
  size_t source_count = 0;
};

size_t write_health(const Health& health, char* buffer, size_t capacity);

enum class Outcome { kReceived, kApplied, kFailed };

// One answer about one command. `detail` may be null, and `applied` is not a claim that
// the hub agreed with the result — only that the node did what it was asked.
size_t write_ack(const char* command_id, const char* node_id, Outcome outcome,
                 const char* detail, char* buffer, size_t capacity);

const char* name_of(Outcome outcome);

}  // namespace sentry

#endif  // SENTRY_CONTROL_H
