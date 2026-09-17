// The second connection: sound, for as long as the hub has asked for it and not one block
// longer.
//
// This is the only part of the firmware that sends anything other than an event, and it is
// deliberately the part with the least memory of its own. Nothing is recorded, nothing is
// kept across a stream and nothing starts by itself: a microphone that was streaming when
// the board reset comes back silent, and stays silent until a hub with a live grant asks
// again. There is no flash in this file and never will be.
//
// What it holds while a stream runs is a few finished blocks and the one being filled.
// When it cannot keep up it throws away whole blocks that were never sent and says so in
// the next one's header — the hub fills that hole with silence, which is honest. Half a
// block is never sent, and a connection that goes wrong is closed rather than continued.

#ifndef SENTRY_DEVICE_MEDIA_H
#define SENTRY_DEVICE_MEDIA_H

#include <cstddef>
#include <cstdint>

#include "sentry/credentials.h"

namespace media {

// Take a stream the hub has just asked for. The host is this node's own provisioned hub
// and never the command's: a command may choose a port, which is a door on a machine this
// node already talks to, and may not choose the machine.
//
// False with a reason when there is nothing to stream — no such source, no microphone
// running on it, or a stream already in hand.
bool start(const char* host, uint16_t port, const char* stream_id, const char* source_id,
           const char* token, uint64_t until_ms, const sentry::Credentials& credentials,
           const char*& why_not);

// Push the end of the stream out. False when that is not the stream this node is running.
bool renew(const char* stream_id, uint64_t until_ms, const char*& why_not);

// End it. `stream_id` may be null, which means whatever is running: a revoked grant, a
// dropped broker connection and a node that is stopping all mean the same thing here.
void stop(const char* stream_id, const char* why);

// Whether sound from this source is wanted right now. False for every source when no
// stream is running, which is the usual answer.
bool wants(const char* source_id);

// Samples, as they were captured. What there is no room for is dropped whole and counted;
// this never blocks and never keeps anything the next call would overwrite.
void offer(const int16_t* samples, size_t count);

// One turn of the loop: the handshake, the answer, and as much of the queue as the
// connection will take. Everything this file does that takes time happens here.
void serve(uint64_t now_ms, const sentry::Credentials& credentials);

// What to say about it, on the cable and in the health message.
struct Numbers {
  const char* state = "idle";   // idle, dialling, greeting, live
  const char* stream_id = nullptr;
  const char* source_id = nullptr;
  uint32_t blocks = 0;          // sent on this connection
  uint32_t dropped = 0;         // finished blocks thrown away rather than sent
  uint32_t connections = 0;     // since this node started
  uint64_t until_ms = 0;
  const char* error = nullptr;  // why the last one ended, or null
};
Numbers how_it_is_going();

}  // namespace media

#endif  // SENTRY_DEVICE_MEDIA_H
