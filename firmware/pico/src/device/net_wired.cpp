// The same network module, for a board that has no radio.
//
// A Pico without a W in its name is still a satellite in this plan — reached over USB by
// the bridge in `PICO-03` rather than over MQTT — and it still runs the sources, the plan,
// the vault and the serial console. What it cannot do is join a network or open a
// connection, and this file is that answer, given once rather than guessed at by every
// caller.
//
// Nothing here pretends. `join` says there is no radio, `dial` says there is no network
// under it, and `socket` stays idle: a board that answered otherwise would have the rest
// of the firmware waiting for something that is never going to arrive.
//
// It exists so that the wired targets compile only what their profile needs. The lwIP and
// mbedTLS halves of `net.cpp` are not built for them at all — there is no TLS stack in the
// image, and no dead code pretending there might be.

#include "net.h"

namespace net {

bool start(const char*) { return false; }

Joined join(const char*, const char*, uint32_t) { return Joined::kNoRadio; }

bool linked() { return false; }

bool address(char*, size_t) { return false; }

int32_t signal_strength() { return 0; }

void ask_the_time(const char*) {}

TimeAnswers time_answers() { return TimeAnswers{}; }

void poll() {}

Dialled dial(const char*, uint16_t, const sentry::Credentials&) { return Dialled::kNotLinked; }

Socket socket() { return Socket::kIdle; }

const char* why_closed() { return "this board has no radio"; }

size_t send(const uint8_t*, size_t) { return 0; }

size_t receive(uint8_t*, size_t) { return 0; }

void hang_up() {}

const char* name_of(Joined joined) {
  switch (joined) {
    case Joined::kYes:
      return "joined";
    case Joined::kNoRadio:
      return "no radio";
    case Joined::kRefused:
      return "refused: the network did not take that passphrase";
    case Joined::kTimedOut:
      return "no answer: out of range, or that network is not here";
  }
  return "unknown";
}

const char* name_of(Dialled dialled) {
  switch (dialled) {
    case Dialled::kOpening:
      return "opening";
    case Dialled::kNotLinked:
      return "no network to reach the broker over";
    case Dialled::kNoCredentials:
      return "no certificate to connect with, so there is no connection to make";
    case Dialled::kNoAddress:
      return "the broker's name did not resolve";
    case Dialled::kNoMemory:
      return "not enough memory for a TLS connection";
    case Dialled::kBusy:
      return "a connection is already open";
  }
  return "unknown";
}

}  // namespace net
