// The closed grammar of what the hub may ask this node to do.
//
// This is the C++ half of `contracts/satellite/v1/control/command.schema.json`, and of the
// rule the schema cannot state: which fields a command may carry depends on what it is
// asking for. A `stop` that arrives with a ticket is not a stop with something extra, it
// is a message nobody meant to send, and it is refused.
//
// A command can lend a capability for a while, take it back, replace the list of sources
// with one made of drivers this node already has, or ask a camera or a microphone for a
// stream to a port on the hub. It cannot name a program, a file, a rule, or another host.
// That is the difference between a peripheral and a second Sentry, and it is decided here
// rather than trusted to the sender.
//
// Nothing is allocated. A command that does not fit in these fields is refused, which is
// the point: the sizes come from the contract, so a sender cannot choose them.

#ifndef SENTRY_COMMAND_H
#define SENTRY_COMMAND_H

#include <cstddef>
#include <cstdint>

namespace sentry {

inline constexpr size_t kMaxSources = 32;
inline constexpr size_t kMaxIdText = 65;      // 64 characters and the NUL after them
inline constexpr size_t kMaxNameText = 41;    // a node or source name, from the contract
inline constexpr size_t kMaxOptionText = 129; // an option value, from the contract
inline constexpr size_t kMaxOptions = 8;
inline constexpr double kMaxStreamSeconds = 600.0;

enum class Action {
  kUnknown,
  kGrant,
  kRenew,
  kRevoke,
  kStop,
  kConfigure,
  kVideoStart,
  kVideoRenew,
  kVideoStop,
  kAudioStart,
  kAudioRenew,
  kAudioStop,
};

enum class Capability { kNone, kEvents, kVideo, kAudio };

// Why a command was not acted on. Every refusal has one, and it is counted, never silent.
enum class Refusal {
  kNone,
  kNotAnObject,
  kUnknownField,
  kMalformed,
  kMissingField,
  kUnknownAction,
  kNotForThisNode,
  kWrongShape,
  kTooMany,
  kOutOfRange,
};

// One option of one configured source: a name and a plain value, never a structure.
struct Option {
  char name[kMaxNameText] = {};
  enum class Type { kString, kInteger, kNumber, kBoolean, kNull } type = Type::kNull;
  char text[kMaxOptionText] = {};
  int64_t integer = 0;
  double number = 0.0;
  bool boolean = false;
};

struct Source {
  char id[kMaxNameText] = {};
  char kind[kMaxNameText] = {};
  Option options[kMaxOptions];
  size_t option_count = 0;
};

struct Command {
  char command_id[kMaxIdText] = {};
  Action action = Action::kUnknown;
  char node_id[kMaxNameText] = {};
  int64_t hub_epoch = 0;
  Capability capability = Capability::kNone;
  char grant_id[kMaxIdText] = {};
  double duration_seconds = 0.0;
  int64_t sequence = 0;
  int64_t revision = 0;
  Source sources[kMaxSources];
  size_t source_count = 0;
  char stream_id[kMaxIdText] = {};
  char source_id[kMaxNameText] = {};
  int64_t port = 0;
  char token[kMaxOptionText] = {};
};

// Read one command addressed to `node_id`. False, with `why` set, if it is not one.
//
// `node_id` is this node's own identity, not the one written in the message: a command is
// obeyed because it was addressed here, never because it says it was.
bool parse_command(const char* payload, size_t size, const char* node_id, Command& out,
                   Refusal& why);

const char* name_of(Action action);
const char* name_of(Refusal refusal);

}  // namespace sentry

#endif  // SENTRY_COMMAND_H
