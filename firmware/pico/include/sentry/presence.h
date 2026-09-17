// Whether one device is here: present, absent, or unknown, and nothing in between.
//
// The rules are the Linux agent's, because the hub must not be able to tell which kind of
// node an arrival came from. Present takes a few sightings inside a window, so that one
// packet from something passing the house is not an arrival. Sightings closer together
// than a second count once, because a beacon that advertises ten times a second is not ten
// times as present. Absent takes a long quiet while the radio was listening the whole
// time: a scanner that stops, for any reason, makes the state unknown at once, and the
// wait for absence starts again when it comes back. Nothing is ever concluded from a gap
// in what the node could hear.
//
// A state reached from unknown is where things stand rather than a change, and is reported
// as a baseline: a node that restarts next to a phone that has been there all evening has
// not just seen somebody arrive.
//
// What is watched is one device, named by its address or by its iBeacon. Not "some device
// appeared": an address nobody wrote down is a stranger's phone, and a presence built out
// of strangers is a presence that says somebody is home whenever a bus goes past.
//
// The signal strength is kept, smoothed, for a person to look at. It is never turned into
// a distance.

#ifndef SENTRY_PRESENCE_H
#define SENTRY_PRESENCE_H

#include <cstddef>
#include <cstdint>

namespace sentry {

// What a configuration may ask for. The limits are the agent's, so that a source file that
// works on a Pi is refused here only for what this board really cannot do.
inline constexpr uint32_t kMaxEnterSightings = 20;
inline constexpr uint32_t kMinEnterWindowMs = 1000;
inline constexpr uint32_t kMaxEnterWindowMs = 120000;
inline constexpr uint32_t kMinAbsentAfterMs = 10000;
inline constexpr uint32_t kMaxAbsentAfterMs = 3600000;

// Two sightings of the same beacon closer together than this are one sighting.
inline constexpr uint32_t kLeastGapMs = 1000;

enum class Presence { kUnknown, kPresent, kAbsent };

const char* name_of(Presence state);

// One device, named the one way or the other. Never both: an address and a beacon are two
// different claims about what is being watched, and a source that made both would be a
// source nobody can read.
struct Watched {
  bool by_address = false;
  uint8_t address[6] = {};  // as it is printed: AA:BB:CC:DD:EE:FF, first byte first
  bool by_beacon = false;
  uint8_t uuid[16] = {};
  int32_t major = -1;  // below zero is any
  int32_t minor = -1;

  bool named() const { return by_address != by_beacon; }
};

// The address as it is printed, into `out` (18 bytes are enough).
bool write_address(const uint8_t address[6], char* out, size_t capacity);

// AA:BB:CC:DD:EE:FF, in capitals, and nothing else.
bool read_address(const char* text, uint8_t out[6]);

// The uuid as a configuration writes it, into `out` (37 bytes are enough). The node says
// back what it is running, and a beacon named by sixteen raw bytes is not a beacon
// anybody can recognise as the one they wrote down.
bool write_uuid(const uint8_t uuid[16], char* out, size_t capacity);

// A uuid in the text a configuration writes it in, lowercase, with its hyphens.
bool read_uuid(const char* text, uint8_t out[16]);

// Whether this advertisement is the device being watched. `address` is who sent it and
// `data` is the raw advertising payload — the AD structures, as the radio handed them
// over. An advertisement that is not an iBeacon is not made into one.
bool is_the_one(const Watched& watched, const uint8_t address[6], const uint8_t* data,
                size_t size);

// The iBeacon in an advertisement, if there is one at all.
struct Beacon {
  uint8_t uuid[16] = {};
  uint16_t major = 0;
  uint16_t minor = 0;
};
bool read_beacon(const uint8_t* data, size_t size, Beacon& out);

struct Watchfulness {
  uint32_t enter_sightings = 3;
  uint32_t enter_window_ms = 10000;
  uint32_t absent_after_ms = 120000;
  int32_t rssi_min = -100;
};

// One device's state, and how it got there. Everything is measured on this node's
// monotonic clock, in milliseconds: none of it survives a reset, and none of it should.
class Watch {
 public:
  // False, and nothing changed, if the configuration is outside the limits above.
  bool configure(const Watchfulness& how);

  // Whether the radio is listening at all. True when the state changed because of it,
  // which is always a change to unknown.
  bool covered(bool scanning, uint64_t now_ms);

  // One sighting of this device. True when it became present because of it.
  bool seen(uint64_t now_ms, int32_t rssi, bool has_rssi);

  // Time passing, and nothing heard. True when it became absent because of it.
  bool quiet(uint64_t now_ms);

  Presence state() const { return state_; }
  // Whether what is being reported is where things stand rather than something that just
  // happened: true for the first state after unknown.
  bool is_baseline() const { return from_unknown_; }
  bool has_rssi() const { return has_rssi_; }
  double rssi() const { return rssi_; }
  uint32_t sightings() const { return sightings_; }
  uint32_t too_weak() const { return too_weak_; }
  bool has_last_seen() const { return last_seen_ms_ != 0; }
  uint64_t last_seen_ms() const { return last_seen_ms_; }

 private:
  bool become(Presence state);

  Watchfulness how_;
  Presence state_ = Presence::kUnknown;
  bool from_unknown_ = true;
  bool covered_ = false;
  uint64_t covered_since_ms_ = 0;
  uint64_t last_seen_ms_ = 0;
  bool has_rssi_ = false;
  double rssi_ = 0.0;
  uint32_t sightings_ = 0;
  uint32_t too_weak_ = 0;
  uint64_t hits_[kMaxEnterSightings] = {};
  size_t hit_count_ = 0;
};

}  // namespace sentry

#endif  // SENTRY_PRESENCE_H
