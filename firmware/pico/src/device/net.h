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

// -- the connections this node makes --------------------------------------------------
//
// TLS, proving who this node is with its own certificate and checking the other end's
// against the authority that issued it. There is no plain path to the same place: a
// handshake that fails leaves a node that does not connect, and that is the point.
//
// There are two lines at most, and they are not interchangeable. The broker's is the one
// this node lives on: it carries the lease, the commands and every event, and it is never
// closed to make room for anything. The media line is borrowed for as long as the hub has
// asked for sound and dropped the moment it stops asking — and if the memory for it is not
// there, that is a stream refused and not a node that fell off the network.

enum class Line { kBroker, kMedia };

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
Dialled dial(const char* host, uint16_t port, const sentry::Credentials& credentials,
             Line line = Line::kBroker);

Socket socket(Line line = Line::kBroker);
// A reason a person can read, for the last connection that ended. Never a secret and never
// a number on its own.
const char* why_closed(Line line = Line::kBroker);

// Hand bytes to the connection. Returns how many it took, which is zero when the
// connection is not open or the send window is full; the caller keeps what was not taken.
// A packet goes whole or not at all: half an MQTT packet cannot be resynchronised.
size_t send(const uint8_t* bytes, size_t size, Line line = Line::kBroker);

// The same, for something that is a stream rather than a packet: it takes what there is
// room for and says how much, and the caller sends the rest next turn. Sound is the only
// thing on this node shaped that way, and the hub reads it back out of a byte stream.
size_t send_some(const uint8_t* bytes, size_t size, Line line);

// Take bytes that arrived, oldest first. Zero when there are none.
size_t receive(uint8_t* out, size_t capacity, Line line = Line::kBroker);

// Close it, and let go of what it held. The certificate and the copy of the private key
// mbedTLS made belong to both lines, and are freed when the last one lets go.
void hang_up(Line line = Line::kBroker);

const char* name_of(Joined joined);
const char* name_of(Dialled dialled);

}  // namespace net

#endif  // SENTRY_DEVICE_NET_H
