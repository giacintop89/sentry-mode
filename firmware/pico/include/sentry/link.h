// Frames for a board with no radio, and the cable they go down.
//
// A Pico or Pico 2 without a W cannot reach a broker. What it can reach is the machine
// that is powering it, over USB, and that machine is already a satellite: it carries this
// node's messages the rest of the way. This file is the shape of what crosses the cable,
// and `contracts/satellite/v1/bridge.json` is the same shape written out for the other
// half, in `src/sentry_mode/satellites/link.py`.
//
// A frame is a magic, a version, what it carries, a counter, a length, the payload and a
// CRC-32 over all of it. The counter is per direction, so a reader can say how many frames
// went missing rather than only that something is wrong. The checksum catches a cable that
// dropped a byte and nothing else: it says nothing about who is at the other end, and
// neither does a USB serial number. A device on this link is trusted because somebody
// plugged it in and wrote it into the bridge's configuration, and for no other reason.
//
// What a frame carries is one of the five channels this node already has, plus four the
// cable needs and the air does not: the time, because a board with no radio has no SNTP to
// ask; a hello, which is the bridge saying it is here; and the two halves of the console,
// because this board has one USB port and text sharing it unmarked is text that can be
// read as a frame. The kind is also the direction, and a frame from the wrong side is
// refused rather than acted on.
//
// Nothing here allocates and nothing here keeps a frame: the caller owns every buffer, as
// everywhere else under `src/protocol`.

#ifndef SENTRY_LINK_H
#define SENTRY_LINK_H

#include <cstddef>
#include <cstdint>

namespace sentry {

inline constexpr uint8_t kLinkMagic[4] = {'S', 'M', 'B', '1'};
inline constexpr uint8_t kLinkVersion = 1;
inline constexpr size_t kLinkHeaderBytes = 13;
inline constexpr size_t kLinkTrailerBytes = 4;
// The largest payload either side sends: a configuration is under two kilobytes on these
// boards, and nothing else comes close.
inline constexpr size_t kMaxLinkPayload = 2048;
inline constexpr size_t kMaxLinkFrame = kLinkHeaderBytes + kMaxLinkPayload + kLinkTrailerBytes;

// What a frame holds. The number is on the wire, so it never changes meaning.
enum class Carries : uint8_t {
  kNothing = 0,
  kEvents = 1,
  kState = 2,
  kHealth = 3,
  kAcks = 4,
  kCommands = 5,
  kTime = 6,
  kHello = 7,
  kSaid = 8,   // a line this board printed
  kTyped = 9,  // a line somebody sent this board's console
};

// What a board sends, and what a bridge sends. Anything else is neither.
bool from_the_node(Carries what);
bool to_the_node(Carries what);

const char* name_of(Carries what);

// CRC-32, the one `zlib.crc32` computes, because the other half of this link is Python.
uint32_t link_checksum(const uint8_t* bytes, size_t size);

// Write one frame. False, and nothing written, if it would not fit, if the payload is
// longer than a frame may carry, or if `what` is not something this side sends.
bool write_frame(Carries what, uint32_t counter, const uint8_t* payload, size_t size,
                 uint8_t* out, size_t capacity, size_t& written);

// A frame that arrived: what it carries, its counter, and where its payload is. The
// payload points into the reader's own buffer and is valid until the next call.
struct Frame {
  Carries what = Carries::kNothing;
  uint32_t counter = 0;
  const uint8_t* payload = nullptr;
  size_t size = 0;
};

// Bytes off the port in, frames out, and a count of what the cable cost.
//
// Fed whatever arrives, in whatever sizes it arrives in. A frame comes out only when its
// checksum is right; anything else is thrown away until the magic lines up again, which is
// what makes a cable that dropped a byte a hiccup rather than a session to restart.
class LinkReader {
 public:
  // `from_a_node` says which side is being read: a frame the other side has no business
  // sending is thrown away and counted rather than believed.
  explicit LinkReader(bool from_a_node) : from_a_node_(from_a_node) {}

  // Take one byte. True when it completed a frame, which is then in `frame`.
  bool eat(uint8_t byte, Frame& frame);

  // Start again with nothing half-read: the cable was unplugged, or the other side reset.
  void forget();

  uint32_t frames() const { return frames_; }
  uint32_t discarded() const { return discarded_; }   // bytes thrown away
  uint32_t missed() const { return missed_; }         // frames the counter says never came
  uint32_t refused() const { return refused_; }       // frames from the wrong side

 private:
  void drop_one();
  bool lined_up() const;
  bool complete(Frame& frame);

  bool from_a_node_ = true;
  uint8_t held_[kMaxLinkFrame] = {};
  size_t size_ = 0;
  size_t deliver_ = 0;  // what the last frame took, dropped when the next byte arrives
  bool counting_ = false;
  uint32_t counter_ = 0;
  uint32_t frames_ = 0;
  uint32_t discarded_ = 0;
  uint32_t missed_ = 0;
  uint32_t refused_ = 0;
};

}  // namespace sentry

#endif  // SENTRY_LINK_H
