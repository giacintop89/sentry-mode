// The USB cable, for a board that has no radio.
//
// A Pico without a W cannot reach a broker. What it can reach is the machine powering it,
// and that machine is already a satellite: `scripts/pico_bridge.py` carries this node's
// messages the rest of the way. This file is the port, and `sentry/link.h` is the shape of
// what crosses it.
//
// One USB port carries everything, so everything is framed — the messages and the console
// both. Text sharing a channel with frames unmarked is text that can be read as a frame
// and a frame that lands in somebody's terminal; carried as `Carries::kSaid`, a line of
// diagnostics is a line of diagnostics wherever it ends up, and the bridge never publishes
// it. The other way round, a line of `Carries::kTyped` is what somebody typed, and it
// reaches `getchar` exactly as it would over a serial monitor: provisioning a wired board
// is the same conversation it is on a wireless one.
//
// Nothing here queues a message. A frame written while no host has the port open is
// refused and counted, and whatever it was about is still on this node's own queue.

#ifndef SENTRY_DEVICE_BRIDGE_H
#define SENTRY_DEVICE_BRIDGE_H

#include <cstddef>
#include <cstdint>

#include "sentry/link.h"

namespace bridge {

// Take the port. From here on `printf` goes out inside frames, and `getchar` reads what
// arrived in them: this is called before anything is printed, so that no line is ever
// written to the cable unframed.
void start();

// Whether a host has the port open. Not whether the bridge has said hello.
bool present();

// One frame out, now. False when there is no host, when the payload will not fit, or when
// this side does not send that kind.
bool say(sentry::Carries what, const uint8_t* payload, size_t size);

// One frame in, if a whole one has arrived. Console lines are taken here rather than
// returned: they belong to `getchar`, not to whatever is reading messages. The payload
// points into the reader's own buffer and is good until the next call.
bool heard(sentry::Frame& frame);

// What the cable has cost so far, for the health message and the console.
struct Counts {
  uint32_t sent = 0;       // frames written to the port
  uint32_t unsent = 0;     // frames there was nobody to write to
  uint32_t frames = 0;     // frames read whole
  uint32_t discarded = 0;  // bytes thrown away looking for a magic
  uint32_t missed = 0;     // frames the counter says never arrived
  uint32_t refused = 0;    // frames from the side that does not send them
  uint32_t unsaid = 0;     // console lines there was nobody to hear
  uint32_t unheard = 0;    // console lines that arrived with nowhere to put them
};
Counts counts();

// The host went away, or said hello again. Nothing half-read and nothing half-printed:
// what was in the middle of arriving belongs to a conversation that is over.
void forget();

}  // namespace bridge

#endif  // SENTRY_DEVICE_BRIDGE_H
