// Which pins a configuration may use, and who already has them.
//
// A configuration that assigned one pin to two sources would start both drivers and
// publish two readings of the same wire under two names. The hub has no way to notice, so
// the node refuses the whole configuration before it starts anything — the same rule as
// everywhere else here: no partial application.
//
// The reserved list depends on the board. On a Pico W, GPIO 23, 24, 25 and 29 are wired to
// the wireless chip; taking one of them does not fail at configuration time, it fails as a
// node that stops being able to reach the broker. The list for a Pico without radio is not
// the same list, which is exactly why it is written per board rather than copied.

#ifndef SENTRY_PINS_H
#define SENTRY_PINS_H

#include <cstddef>

namespace sentry {

enum class Board { kPico, kPicoW, kPico2, kPico2W };

// The RP2040 and RP2350 both expose GPIO 0 to 29 on these boards.
inline constexpr int kMaxGpio = 29;
inline constexpr size_t kMaxClaims = 32;

bool is_reserved(Board board, int gpio);

// Why a pin could not be taken. The node says which, because "configuration refused" with
// no reason is a message somebody has to guess at.
enum class PinRefusal { kNone, kOutOfRange, kReserved, kTaken };

class PinMap {
 public:
  explicit PinMap(Board board) : board_(board) {}

  // Take one pin for one source. False leaves the map as it was.
  bool claim(int gpio, const char* source_id, PinRefusal& why);

  // Who has it, or null.
  const char* holder(int gpio) const;

  void clear();
  size_t size() const { return count_; }

 private:
  struct Claim {
    int gpio = -1;
    const char* source_id = nullptr;
  };

  Board board_;
  Claim claims_[kMaxClaims];
  size_t count_ = 0;
};

const char* name_of(PinRefusal why);

}  // namespace sentry

#endif  // SENTRY_PINS_H
