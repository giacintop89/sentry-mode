#include "sentry/link.h"

#include <cstring>

namespace sentry {
namespace {

void put32(uint8_t* out, uint32_t value) {
  out[0] = static_cast<uint8_t>(value & 0xFFu);
  out[1] = static_cast<uint8_t>((value >> 8) & 0xFFu);
  out[2] = static_cast<uint8_t>((value >> 16) & 0xFFu);
  out[3] = static_cast<uint8_t>((value >> 24) & 0xFFu);
}

void put16(uint8_t* out, uint16_t value) {
  out[0] = static_cast<uint8_t>(value & 0xFFu);
  out[1] = static_cast<uint8_t>((value >> 8) & 0xFFu);
}

uint32_t take32(const uint8_t* from) {
  return static_cast<uint32_t>(from[0]) | (static_cast<uint32_t>(from[1]) << 8) |
         (static_cast<uint32_t>(from[2]) << 16) | (static_cast<uint32_t>(from[3]) << 24);
}

uint16_t take16(const uint8_t* from) {
  return static_cast<uint16_t>(static_cast<uint16_t>(from[0]) |
                               static_cast<uint16_t>(static_cast<uint16_t>(from[1]) << 8));
}

}  // namespace

bool from_the_node(Carries what) {
  return what == Carries::kEvents || what == Carries::kState || what == Carries::kHealth ||
         what == Carries::kAcks || what == Carries::kSaid;
}

bool to_the_node(Carries what) {
  return what == Carries::kCommands || what == Carries::kTime || what == Carries::kHello ||
         what == Carries::kTyped;
}

const char* name_of(Carries what) {
  switch (what) {
    case Carries::kNothing:
      return "nothing";
    case Carries::kEvents:
      return "events";
    case Carries::kState:
      return "state";
    case Carries::kHealth:
      return "health";
    case Carries::kAcks:
      return "acks";
    case Carries::kCommands:
      return "commands";
    case Carries::kTime:
      return "time";
    case Carries::kHello:
      return "hello";
    case Carries::kSaid:
      return "said";
    case Carries::kTyped:
      return "typed";
  }
  return "unknown";
}

// The reflected CRC-32 of the standard, computed a bit at a time. There is no table here
// on purpose: a kilobyte of constants to save microseconds on a frame that comes once a
// second is memory a queue would rather have, and this is the same polynomial `zlib`
// uses, which is what the other end of the cable computes with.
uint32_t link_checksum(const uint8_t* bytes, size_t size) {
  if (bytes == nullptr) return 0;
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t index = 0; index < size; ++index) {
    crc ^= bytes[index];
    for (int bit = 0; bit < 8; ++bit) {
      const bool odd = (crc & 1u) != 0u;
      crc >>= 1;
      if (odd) crc ^= 0xEDB88320u;
    }
  }
  return ~crc;
}

bool write_frame(Carries what, uint32_t counter, const uint8_t* payload, size_t size,
                 uint8_t* out, size_t capacity, size_t& written) {
  written = 0;
  if (out == nullptr) return false;
  if (!from_the_node(what) && !to_the_node(what)) return false;
  if (size > kMaxLinkPayload) return false;
  if (size > 0 && payload == nullptr) return false;
  const size_t whole = kLinkHeaderBytes + size + kLinkTrailerBytes;
  if (capacity < whole) return false;

  std::memcpy(out, kLinkMagic, sizeof(kLinkMagic));
  out[4] = kLinkVersion;
  out[5] = static_cast<uint8_t>(what);
  out[6] = 0;  // flags: none are defined, and a frame that sets one is refused
  put32(out + 7, counter);
  put16(out + 11, static_cast<uint16_t>(size));
  if (size > 0) std::memcpy(out + kLinkHeaderBytes, payload, size);
  put32(out + kLinkHeaderBytes + size, link_checksum(out, kLinkHeaderBytes + size));
  written = whole;
  return true;
}

void LinkReader::forget() {
  size_ = 0;
  deliver_ = 0;
  counting_ = false;
  counter_ = 0;
}

void LinkReader::drop_one() {
  if (size_ == 0) return;
  std::memmove(held_, held_ + 1, size_ - 1);
  --size_;
  ++discarded_;
}

// Whether what is held could still be the beginning of a frame. Anything else is a byte
// the cable put there, and it goes.
bool LinkReader::lined_up() const {
  const size_t look = size_ < sizeof(kLinkMagic) ? size_ : sizeof(kLinkMagic);
  for (size_t index = 0; index < look; ++index) {
    if (held_[index] != kLinkMagic[index]) return false;
  }
  return true;
}

bool LinkReader::eat(uint8_t byte, Frame& frame) {
  // The frame handed out last time pointed into this buffer, so it is only now that the
  // bytes it was made of can go.
  if (deliver_ > 0) {
    std::memmove(held_, held_ + deliver_, size_ - deliver_);
    size_ -= deliver_;
    deliver_ = 0;
  }
  if (size_ == kMaxLinkFrame) drop_one();  // cannot happen: the length bounds every frame
  held_[size_++] = byte;

  for (;;) {
    while (size_ > 0 && !lined_up()) drop_one();
    if (size_ < kLinkHeaderBytes) return false;
    const uint8_t version = held_[4];
    const uint8_t flags = held_[6];
    const size_t length = take16(held_ + 11);
    if (version != kLinkVersion || flags != 0 || length > kMaxLinkPayload) {
      drop_one();
      continue;
    }
    const size_t whole = kLinkHeaderBytes + length + kLinkTrailerBytes;
    if (size_ < whole) return false;
    const uint32_t said = take32(held_ + kLinkHeaderBytes + length);
    if (said != link_checksum(held_, kLinkHeaderBytes + length)) {
      drop_one();
      continue;
    }
    if (complete(frame)) return true;
    // It was a frame, and not one this side may be sent. It is gone either way.
  }
}

bool LinkReader::complete(Frame& frame) {
  const size_t length = take16(held_ + 11);
  const size_t whole = kLinkHeaderBytes + length + kLinkTrailerBytes;
  const Carries what = static_cast<Carries>(held_[5]);
  const uint32_t counter = take32(held_ + 7);
  const bool mine = from_a_node_ ? from_the_node(what) : to_the_node(what);
  if (!mine) {
    ++refused_;
    std::memmove(held_, held_ + whole, size_ - whole);
    size_ -= whole;
    return false;
  }
  // The counter is what says a frame went missing rather than arrived late: nothing is
  // reordered on a serial cable, so a jump is a loss. It wraps as an unsigned number does,
  // which is the same arithmetic the other end uses.
  if (counting_) missed_ += counter - counter_ - 1u;
  counting_ = true;
  counter_ = counter;
  ++frames_;

  frame.what = what;
  frame.counter = counter;
  frame.payload = held_ + kLinkHeaderBytes;
  frame.size = length;
  deliver_ = whole;
  return true;
}

}  // namespace sentry
