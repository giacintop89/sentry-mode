// What this node will actually run, decided before anything starts.
//
// A configuration arrives as a list of sources with options on them, written by somebody
// who may be looking at a different board than this one. Between that list and a running
// driver there is a decision to make about every line of it: whether this firmware has
// that driver at all, whether the pin exists, whether it belongs to the radio, whether
// another source in the same list already took it, and whether each option is a value this
// board can survive.
//
// All of that happens here, on a copy, before a single pin is touched. A configuration is
// taken whole or refused whole with a reason naming the source it failed on, because a
// node that started half of one would be a node whose sources are whatever survived —
// something neither the hub nor the person who wrote the file could predict.
//
// Nothing here reads a pin or starts a driver. That is the device's half, and it is given
// a plan that has already been agreed to.

#ifndef SENTRY_PLAN_H
#define SENTRY_PLAN_H

#include <cstddef>
#include <cstdint>

#include "sentry/command.h"
#include "sentry/input.h"
#include "sentry/pins.h"
#include "sentry/spool.h"  // kMaxKindText: what an event kind may be, in one place

namespace sentry {

// Every driver this firmware has. A configuration naming anything else — a camera, a
// microphone, a bus this build has no code for — is refused rather than ignored.
enum class Driver { kNone, kBoard, kGpio };

// What holds a pin when nothing else is driving it. A contact wired to ground needs a pull
// up or it reads whatever the air says; a PIR drives its own line and needs neither.
enum class Bias { kNone, kPullUp, kPullDown };

// Eight sources on one of these boards. The limit is the loop's, not the contract's: every
// one of them is sampled on the same thread that keeps the connection alive.
inline constexpr size_t kMaxPlanned = 8;

// The seconds a board measurement may be taken apart, and what it defaults to. Thirty is
// what the Linux agent uses for the same reading, and a die temperature that changes by
// degrees over minutes has nothing to say every second.
inline constexpr uint32_t kMinBoardSeconds = 1;
inline constexpr uint32_t kMaxBoardSeconds = 3600;
inline constexpr uint32_t kDefaultBoardSeconds = 30;

struct BoardSource {
  // The only measure this chip can take of itself. It is an option rather than an
  // assumption so that a configuration written for a Linux node, which also measures its
  // load and its memory, is refused for naming one rather than quietly measuring another.
  char measure[kMaxNameText] = {};
  uint32_t interval_ms = kDefaultBoardSeconds * 1000;
};

struct GpioSource {
  int pin = -1;
  Bias bias = Bias::kNone;
  InputConfig input;  // polarity, debounce and the settling a PIR needs
  char event_kind[kMaxKindText] = {};
};

struct Planned {
  char source_id[kMaxNameText] = {};
  Driver driver = Driver::kNone;
  bool enabled = true;
  BoardSource board;
  GpioSource gpio;
};

// Why a configuration was not taken up. Every one of these is said out loud with the name
// of the source it happened on: "refused" with no reason is a message somebody has to
// guess at, and they will guess wrong.
enum class Unplanned {
  kNone,
  kTooMany,
  kNotAName,
  kRepeatedId,
  kNoSuchDriver,
  kNoSuchOption,
  kMissingOption,
  kWrongType,
  kOutOfRange,
  kPinRefused,
};

inline constexpr size_t kMaxPlanDetail = 128;

class Plan {
 public:
  explicit Plan(Board board) : board_(board) {}

  // Take a whole configuration, or none of it. `detail`, when there is room for it, is
  // filled with something a person can act on; it is the text the ack carries back.
  bool take(const Source* sources, size_t count, Unplanned& why, char* detail, size_t capacity);

  size_t size() const { return count_; }
  const Planned& at(size_t index) const { return planned_[index]; }

  // Who has a pin in the plan as it stands, or null. The device asks before it touches one.
  const char* holder(int gpio) const { return pins_.holder(gpio); }

  void clear();

 private:
  Board board_;
  PinMap pins_{board_};
  Planned planned_[kMaxPlanned];
  size_t count_ = 0;
};

const char* name_of(Driver driver);
const char* name_of(Unplanned why);

// The kinds this firmware answers to in a configuration: `board`, `gpio`. Anything else is
// a driver somebody else has.
Driver driver_of(const char* kind);

}  // namespace sentry

#endif  // SENTRY_PLAN_H
