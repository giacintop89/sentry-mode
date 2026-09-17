// The radio, the address it was given, and what time the network says it is.
//
// This is the only file in the firmware that talks to lwIP or to the wireless chip. It is
// deliberately thin: joining a network and asking for the time are two things that either
// happen or do not, and everything above wants the answer rather than the machinery.

#ifndef SENTRY_DEVICE_NET_H
#define SENTRY_DEVICE_NET_H

#include <cstddef>
#include <cstdint>

#include "sentry/credentials.h"

namespace net {

enum class Joined { kYes, kNoRadio, kRefused, kTimedOut };

// Bring the radio up as a station. Once, at boot: it also allocates the driver's buffers.
bool start(const char* hostname);

// Join, waiting up to `patience_ms`. An empty passphrase means an open network, which is
// not the same as a network with a passphrase nobody typed.
Joined join(const char* ssid, const char* passphrase, uint32_t patience_ms);

// Whether the link is up right now, which is not the same as having joined once.
bool linked();

// The address DHCP gave this board, written as text. False when there is none yet.
bool address(char* out, size_t capacity);

// How strong the signal is, in dBm. Zero when there is no link to measure.
int32_t signal_strength();

// Start asking the network what time it is. The answer arrives on another turn of the
// loop, at `sentry_sntp_set_time_us`, which is why this returns nothing useful.
void ask_the_time(const char* server);

// What the last answer was worth: how many have arrived, and the monotonic moment of the
// most recent one. Nothing here is a clock; the timebase is.
struct TimeAnswers {
  uint32_t count = 0;
  int64_t last_unix_ms = 0;
  uint64_t taken_at_us = 0;
};
TimeAnswers time_answers();

// Let the driver and lwIP run. With the background context this is nearly nothing, and it
// is still called every turn so that a change of context strategy stays a one-line change.
void poll();

// -- the one connection this node makes ----------------------------------------------
//
// TLS, to the broker, proving who this node is with its own certificate and checking the
// broker's against the authority that issued it. There is no plain path to the same place:
// a handshake that fails leaves a node that does not connect, and that is the point.

enum class Dialled {
  kOpening,          // the connection is on its way; watch `socket()`
  kNotLinked,        // no network under it
  kNoCredentials,    // the three things a connection is made of are not all here
  kNoAddress,        // the broker's name did not resolve
  kNoMemory,         // the TLS configuration or the connection would not allocate
  kBusy,             // one is already open, and this node makes one at a time
};

enum class Socket {
  kIdle,
  kResolving,
  kConnecting,  // TCP and the handshake, which from here are one wait
  kOpen,
  kClosed,      // it ended; `why_closed()` says how
};

// Start connecting. Returns as soon as it is under way: the handshake takes a second or so
// on this chip and nothing here blocks the loop while it happens.
Dialled dial(const char* host, uint16_t port, const sentry::Credentials& credentials);

Socket socket();
// A reason a person can read, for the last connection that ended. Never a secret and never
// a number on its own.
const char* why_closed();

// Hand bytes to the connection. Returns how many it took, which is zero when the
// connection is not open or the send window is full; the caller keeps what was not taken.
size_t send(const uint8_t* bytes, size_t size);

// Take bytes that arrived, oldest first. Zero when there are none.
size_t receive(uint8_t* out, size_t capacity);

// Close it, and let go of everything the connection held — including the copy of the
// private key mbedTLS made.
void hang_up();

const char* name_of(Joined joined);
const char* name_of(Dialled dialled);

}  // namespace net

#endif  // SENTRY_DEVICE_NET_H
