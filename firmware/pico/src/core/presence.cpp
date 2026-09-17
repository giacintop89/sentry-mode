#include "sentry/presence.h"

#include <cstdio>
#include <cstring>

namespace sentry {
namespace {

// How much of a new reading goes into the smoothed one. The agent's number, because the
// two have to agree about what they show for the same beacon.
constexpr double kSmoothing = 0.3;

constexpr uint8_t kApple[] = {0x4c, 0x00};
constexpr uint8_t kIbeacon[] = {0x02, 0x15};

bool hex_value(char letter, uint8_t& out) {
  if (letter >= '0' && letter <= '9') {
    out = static_cast<uint8_t>(letter - '0');
    return true;
  }
  if (letter >= 'a' && letter <= 'f') {
    out = static_cast<uint8_t>(letter - 'a' + 10);
    return true;
  }
  return false;
}

// Two hex digits, in the case the configuration is supposed to have written them in. An
// address is capitals and a uuid is lowercase, the same way round as the agent's patterns,
// so that one file cannot be accepted by one node and refused by the other.
bool hex_pair(const char* text, bool capitals, uint8_t& out) {
  char first = text[0];
  char second = text[1];
  if (capitals) {
    if ((first >= 'a' && first <= 'f') || (second >= 'a' && second <= 'f')) return false;
    if (first >= 'A' && first <= 'F') first = static_cast<char>(first - 'A' + 'a');
    if (second >= 'A' && second <= 'F') second = static_cast<char>(second - 'A' + 'a');
  } else if ((first >= 'A' && first <= 'F') || (second >= 'A' && second <= 'F')) {
    return false;
  }
  uint8_t high = 0;
  uint8_t low = 0;
  if (!hex_value(first, high) || !hex_value(second, low)) return false;
  out = static_cast<uint8_t>((high << 4) | low);
  return true;
}

}  // namespace

const char* name_of(Presence state) {
  switch (state) {
    case Presence::kPresent:
      return "present";
    case Presence::kAbsent:
      return "absent";
    case Presence::kUnknown:
    default:
      return "unknown";
  }
}

bool write_address(const uint8_t address[6], char* out, size_t capacity) {
  if (out == nullptr || capacity < 18) return false;
  const int written = std::snprintf(out, capacity, "%02X:%02X:%02X:%02X:%02X:%02X", address[0],
                                    address[1], address[2], address[3], address[4], address[5]);
  return written == 17;
}

bool write_uuid(const uint8_t uuid[16], char* out, size_t capacity) {
  if (capacity < 37) return false;
  static const char* kDigits = "0123456789abcdef";
  size_t at = 0;
  for (size_t index = 0; index < 16; ++index) {
    if (index == 4 || index == 6 || index == 8 || index == 10) out[at++] = '-';
    out[at++] = kDigits[uuid[index] >> 4];
    out[at++] = kDigits[uuid[index] & 0x0f];
  }
  out[at] = '\0';
  return true;
}

bool read_address(const char* text, uint8_t out[6]) {
  if (text == nullptr || std::strlen(text) != 17) return false;
  for (size_t index = 0; index < 6; ++index) {
    const size_t at = index * 3;
    if (index > 0 && text[at - 1] != ':') return false;
    if (!hex_pair(text + at, true, out[index])) return false;
  }
  return true;
}

bool read_uuid(const char* text, uint8_t out[16]) {
  if (text == nullptr || std::strlen(text) != 36) return false;
  size_t at = 0;
  for (size_t index = 0; index < 16; ++index) {
    if (at == 8 || at == 13 || at == 18 || at == 23) {
      if (text[at] != '-') return false;
      ++at;
    }
    if (!hex_pair(text + at, false, out[index])) return false;
    at += 2;
  }
  return at == 36;
}

bool read_beacon(const uint8_t* data, size_t size, Beacon& out) {
  if (data == nullptr) return false;
  // The advertising payload is a list of [length][type][value]. Anything that does not fit
  // inside what was received ends the walk: a truncated advertisement is not evidence.
  size_t at = 0;
  while (at < size) {
    const size_t length = data[at];
    if (length == 0 || at + 1 + length > size) return false;
    const uint8_t type = data[at + 1];
    const uint8_t* value = data + at + 2;
    const size_t value_size = length - 1;
    // 0xFF is manufacturer specific: Apple's company id, Apple's iBeacon prefix, then the
    // uuid, the major, the minor and a transmit power this node has no use for.
    if (type == 0xff && value_size >= 25 && std::memcmp(value, kApple, 2) == 0 &&
        std::memcmp(value + 2, kIbeacon, 2) == 0) {
      std::memcpy(out.uuid, value + 4, 16);
      out.major = static_cast<uint16_t>((value[20] << 8) | value[21]);
      out.minor = static_cast<uint16_t>((value[22] << 8) | value[23]);
      return true;
    }
    at += 1 + length;
  }
  return false;
}

bool is_the_one(const Watched& watched, const uint8_t address[6], const uint8_t* data,
                size_t size) {
  if (!watched.named()) return false;
  if (watched.by_address) {
    return address != nullptr && std::memcmp(watched.address, address, 6) == 0;
  }
  Beacon found;
  if (!read_beacon(data, size, found)) return false;
  if (std::memcmp(found.uuid, watched.uuid, 16) != 0) return false;
  if (watched.major >= 0 && found.major != watched.major) return false;
  if (watched.minor >= 0 && found.minor != watched.minor) return false;
  return true;
}

bool Watch::configure(const Watchfulness& how) {
  if (how.enter_sightings < 1 || how.enter_sightings > kMaxEnterSightings) return false;
  if (how.enter_window_ms < kMinEnterWindowMs || how.enter_window_ms > kMaxEnterWindowMs) {
    return false;
  }
  if (how.absent_after_ms < kMinAbsentAfterMs || how.absent_after_ms > kMaxAbsentAfterMs) {
    return false;
  }
  if (how.rssi_min < -127 || how.rssi_min > 0) return false;
  // Two sightings a second apart at best: a window too short to hold the sightings it asks
  // for is a watch that would wait for an arrival that can never be counted. Refused here
  // rather than discovered by somebody standing in the hall.
  if (static_cast<uint64_t>(how.enter_sightings - 1) * kLeastGapMs >= how.enter_window_ms) {
    return false;
  }
  how_ = how;
  return true;
}

bool Watch::covered(bool scanning, uint64_t now_ms) {
  if (scanning) {
    if (!covered_) {
      covered_ = true;
      covered_since_ms_ = now_ms;
    }
    return false;
  }
  covered_ = false;
  covered_since_ms_ = 0;
  hit_count_ = 0;
  return become(Presence::kUnknown);
}

bool Watch::seen(uint64_t now_ms, int32_t rssi, bool has_rssi) {
  // Heard while the radio was not listening: impossible from the scanner, and refused here
  // rather than trusted, because what it would mean is that the coverage is wrong.
  if (!covered_) return false;
  if (has_rssi && rssi < how_.rssi_min) {
    ++too_weak_;
    return false;
  }
  ++sightings_;
  last_seen_ms_ = now_ms;
  if (has_rssi) {
    const double reading = static_cast<double>(rssi);
    rssi_ = has_rssi_ ? rssi_ + kSmoothing * (reading - rssi_) : reading;
    has_rssi_ = true;
  }
  if (hit_count_ > 0 && now_ms - hits_[hit_count_ - 1] < kLeastGapMs) return false;
  if (hit_count_ == kMaxEnterSightings) {
    for (size_t index = 1; index < hit_count_; ++index) hits_[index - 1] = hits_[index];
    --hit_count_;
  }
  hits_[hit_count_++] = now_ms;
  // Only the sightings inside the window count towards arriving.
  size_t first = 0;
  while (first < hit_count_ && now_ms - hits_[first] > how_.enter_window_ms) ++first;
  if (first > 0) {
    for (size_t index = first; index < hit_count_; ++index) hits_[index - first] = hits_[index];
    hit_count_ -= first;
  }
  if (state_ != Presence::kPresent && hit_count_ >= how_.enter_sightings) {
    return become(Presence::kPresent);
  }
  return false;
}

bool Watch::quiet(uint64_t now_ms) {
  if (!covered_ || state_ == Presence::kAbsent) return false;
  const uint64_t since =
      last_seen_ms_ > covered_since_ms_ ? last_seen_ms_ : covered_since_ms_;
  if (now_ms < since || now_ms - since < how_.absent_after_ms) return false;
  hit_count_ = 0;
  return become(Presence::kAbsent);
}

bool Watch::become(Presence state) {
  if (state == state_) return false;
  from_unknown_ = state_ == Presence::kUnknown;
  state_ = state;
  return true;
}

}  // namespace sentry
