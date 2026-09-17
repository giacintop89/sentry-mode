#include "bridge.h"

#include <cstring>

#include "pico.h"
#include "pico/stdio.h"
#include "pico/stdio/driver.h"
#include "pico/stdio_usb.h"

namespace bridge {
namespace {

// The bytes of the port, before they are frames. Small on purpose: whatever is not read
// this turn is still in TinyUSB's own buffer and is read on the next one.
uint8_t arrived[128];
size_t arrived_size = 0;
size_t arrived_at = 0;

// This side reads what a bridge sends; a frame of events arriving from a bridge is a
// bridge doing something it never does.
sentry::LinkReader reader(false);

// What has been printed and not yet framed. A line at a time, because a line is what a
// person reading the other end is waiting for, and because a frame per character would be
// twenty bytes of header for one.
char pending[512];
size_t pending_size = 0;

// What somebody typed, waiting for `getchar` to ask for it. Long enough for the longest
// thing anybody sends this board — a provisioning record with three PEM blocks in it —
// plus a frame's worth of room behind it.
char typed[6144];
size_t typed_head = 0;  // where the next byte is read from
size_t typed_size = 0;  // how many are waiting

// What this board said before anybody was listening. On a bridged board the first lines
// are the interesting ones — what it took back out of flash, what it refused and why — and
// they are printed in the second or so between the power coming up and the machine at the
// other end opening the port. Counting them as lost was honest and useless: they are held
// here instead, and go out as soon as there is somebody to go to. What does not fit is
// still counted as unsaid, because a boot that says more than this has other problems.
char first_words[1024];
size_t first_words_size = 0;

uint32_t counter = 0;  // frames this side has written, ever
Counts counted;

// True while a frame is being written. `say` prints nothing, but the stdio driver is
// reachable from anywhere, and a line printed from inside a write would be a frame inside
// a frame. It is dropped and counted instead.
bool writing = false;

void put(const uint8_t* bytes, size_t size) {
  stdio_usb.out_chars(reinterpret_cast<const char*>(bytes), static_cast<int>(size));
}

bool write_frame_out(sentry::Carries what, const uint8_t* payload, size_t size) {
  if (!stdio_usb_connected()) return false;
  static uint8_t frame[sentry::kMaxLinkFrame];
  size_t written = 0;
  if (!sentry::write_frame(what, counter, payload, size, frame, sizeof(frame), written)) {
    return false;
  }
  ++counter;
  ++counted.sent;
  writing = true;
  put(frame, written);
  writing = false;
  return true;
}

// Whatever has been printed, as one frame. Called when a line ends and once a turn, so
// that a prompt with no newline after it still reaches the person waiting for it.
void flush_pending() {
  // Whatever was said before the cable was open goes first, so that the lines arrive in
  // the order they were printed rather than after the ones that followed them.
  if (first_words_size > 0 && stdio_usb_connected()) {
    const size_t held = first_words_size;
    first_words_size = 0;
    if (!write_frame_out(sentry::Carries::kSaid, reinterpret_cast<const uint8_t*>(first_words),
                         held)) {
      ++counted.unsaid;
    }
  }
  if (pending_size == 0) return;
  const size_t size = pending_size;
  pending_size = 0;  // before the write, so that a print from inside it cannot loop
  if (write_frame_out(sentry::Carries::kSaid, reinterpret_cast<const uint8_t*>(pending), size)) {
    return;
  }
  if (first_words_size + size <= sizeof(first_words)) {
    std::memcpy(first_words + first_words_size, pending, size);
    first_words_size += size;
    return;
  }
  ++counted.unsaid;
}

// The stdio driver. Everything this program prints arrives here, and nothing else does.
void said(const char* text, int length) {
  if (writing || length <= 0) {
    if (length > 0) ++counted.unsaid;
    return;
  }
  for (int index = 0; index < length; ++index) {
    const char letter = text[index];
    if (pending_size < sizeof(pending)) pending[pending_size++] = letter;
    if (letter == '\n' || pending_size == sizeof(pending)) flush_pending();
  }
}

void said_flush() { flush_pending(); }

int typed_in(char* out, int length) {
  if (length <= 0 || typed_size == 0) return PICO_ERROR_NO_DATA;
  size_t want = static_cast<size_t>(length);
  if (want > typed_size) want = typed_size;
  for (size_t index = 0; index < want; ++index) {
    out[index] = typed[typed_head];
    typed_head = (typed_head + 1) % sizeof(typed);
  }
  typed_size -= want;
  return static_cast<int>(want);
}

// A console line that arrived. What will not fit is dropped rather than overwriting what
// is already waiting: half of an old line and half of a new one is worse than one line.
void take_typed(const uint8_t* payload, size_t size) {
  if (size > sizeof(typed) - typed_size) {
    ++counted.unheard;
    return;
  }
  size_t at = (typed_head + typed_size) % sizeof(typed);
  for (size_t index = 0; index < size; ++index) {
    typed[at] = static_cast<char>(payload[index]);
    at = (at + 1) % sizeof(typed);
  }
  typed_size += size;
}

stdio_driver_t as_frames;

}  // namespace

void start() {
  as_frames.out_chars = said;
  as_frames.out_flush = said_flush;
  as_frames.in_chars = typed_in;
  // The port keeps working — TinyUSB is still serviced by the SDK's own timer — but the
  // driver that writes text straight to it is taken out of the chain. Nothing printed
  // after this reaches the cable except inside a frame.
  stdio_set_driver_enabled(&stdio_usb, false);
  stdio_set_driver_enabled(&as_frames, true);
}

bool present() { return stdio_usb_connected(); }

bool say(sentry::Carries what, const uint8_t* payload, size_t size) {
  flush_pending();
  if (!write_frame_out(what, payload, size)) {
    ++counted.unsent;
    return false;
  }
  return true;
}

bool heard(sentry::Frame& frame) {
  flush_pending();
  while (true) {
    if (arrived_at == arrived_size) {
      arrived_size = 0;
      arrived_at = 0;
      const int got = stdio_usb.in_chars(reinterpret_cast<char*>(arrived), sizeof(arrived));
      if (got <= 0) return false;
      arrived_size = static_cast<size_t>(got);
    }
    while (arrived_at < arrived_size) {
      if (!reader.eat(arrived[arrived_at++], frame)) continue;
      counted.frames = reader.frames();
      counted.discarded = reader.discarded();
      counted.missed = reader.missed();
      counted.refused = reader.refused();
      if (frame.what != sentry::Carries::kTyped) return true;
      take_typed(frame.payload, frame.size);
    }
  }
}

Counts counts() { return counted; }

void forget() {
  reader.forget();
  arrived_size = 0;
  arrived_at = 0;
  pending_size = 0;
  typed_head = 0;
  typed_size = 0;
}

}  // namespace bridge
