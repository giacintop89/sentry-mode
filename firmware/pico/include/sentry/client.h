// What is in flight, what has not been answered, and when to stop waiting.
//
// `mqtt.h` writes packets and reads them back; `session.h` says what order a connection
// has to come up in. Neither remembers anything and neither knows the time. This does: it
// holds one publish in flight, sends it again with DUP when no PUBACK arrives, pings
// before the keepalive runs out, and gives up on a link that has stopped answering rather
// than waiting on it forever.
//
// It still has no socket. Bytes to send are written into a buffer the caller owns and
// bytes that arrived are handed in, so the whole state machine — every timeout, every
// retransmission, every refusal — runs on a host against a clock made of integers.
//
// One publish in flight and not a window of them. A 264 kB part has nowhere to keep
// several unacknowledged payloads, and the queue behind this (`spool.h`) has already
// decided what is worth keeping; the caller offers the front of it and is told when it may
// let go. A reading sent again is the same packet id with DUP set — the same event
// arriving twice, which the hub deduplicates, and never a second event.
//
// Everything this node publishes is at QoS 1. An event nobody acknowledged is an event
// nobody can say arrived, and a queue that dropped what it could not confirm would be
// losing readings quietly.

#ifndef SENTRY_CLIENT_H
#define SENTRY_CLIENT_H

#include <cstddef>
#include <cstdint>

#include "sentry/identity.h"
#include "sentry/mqtt.h"
#include "sentry/session.h"

namespace sentry {
namespace mqtt {

// How long this node waits for each answer, on its own monotonic clock in milliseconds.
// The defaults are for a LAN with a broker on it: generous enough that a busy broker is
// not mistaken for a dead one, short enough that a node does not sit on a link that will
// never answer while readings pile up behind it.
struct Timings {
  uint32_t connack_ms = 10000;
  uint32_t suback_ms = 10000;
  uint32_t puback_ms = 5000;
  // How many times the same publish is sent before the link is treated as the problem.
  // Sending a sixth copy of something to a broker that answered none of five is not
  // persistence, it is a node that will never notice it is talking to nobody.
  uint8_t attempts = 3;
  // The broker drops a client it has not heard from in one and a half keepalives, so the
  // ping goes out at half of one: two may be lost before that becomes true.
  uint16_t keepalive_seconds = 30;
  uint32_t pong_ms = 10000;
  // What to wait before opening the socket again, doubling per failed attempt. The caller
  // adds its own jitter if it has entropy: this is arithmetic and has none.
  uint32_t first_backoff_ms = 1000;
  uint32_t max_backoff_ms = 60000;
};

// What the caller should do with the bytes `next()` just wrote.
enum class Todo {
  kNothing,     // nothing to send at this instant; come back later
  kConnect,     // a CONNECT, carrying the will
  kSubscribe,   // the commands topic
  kPublish,     // the pending message, for the first time
  kRepublish,   // the same packet id again, with DUP set
  kAcknowledge, // a PUBACK the broker is owed
  kPing,
  kDrop,        // the pending message cannot be written as it stands; let go of it
  kLeave,       // a DISCONNECT went out; send it, then close the socket
  kGiveUp,      // close the socket; `trouble()` says what went unanswered
};

// Why a link was given up on. Everything here is something that did not happen in time,
// except the two that are the broker saying no.
enum class Trouble {
  kNone,
  kNoConnAck,
  kNoSubAck,
  kNoPubAck,
  kNoPong,
  kRefused,   // CONNACK with a return code, or a SUBACK that granted nothing
  kProtocol,  // a packet that cannot be part of this conversation
};

// What an incoming packet meant to the connection, on top of what the packet was.
enum class Effect {
  kNothing,    // a command for the caller to read, or a PINGRESP
  kConnected,  // the broker accepted us; the subscription goes out next
  kListening,  // the subscription is confirmed
  kDelivered,  // the publish in flight was acknowledged and may be let go of
  kAnnounced,  // that publish was the retained state, so this node is now online
  kRefused,    // the link is over: the broker said no, or said something impossible
};

// One message the caller would like published, owned by the caller. It must stay where it
// is until the client says it was delivered: a retransmission sends these same bytes.
struct Pending {
  const char* topic = nullptr;
  const uint8_t* payload = nullptr;
  size_t size = 0;
  bool retain = false;
  // The retained `state` that tells the hub this node is here. It is the one publish
  // allowed before the node is online, because it is what makes it online.
  bool announcement = false;
};

class Client {
 public:
  // `connect` and `commands_topic` are read every time a connection is opened, so they
  // must outlive this client — on a board they are statics, which is what they are for.
  Client(const Connect& connect, const char* commands_topic, const Timings& timings = Timings());

  // The socket is open and, if there is one, the TLS handshake is done. `connection_id`
  // is this connection's own identifier, which `Session` refuses to see twice.
  bool opened(uint32_t now_ms, const char* connection_id);

  // The socket is gone, however it went: closed, reset, given up on. Nothing is in flight
  // afterwards, and what was in flight was never acknowledged and stays with the caller.
  void closed();

  // Go, and say so. A broker told that a client meant to leave keeps the will to itself,
  // so a node that was asked to stop does not also look like one that fell off the
  // network. What is already owed and what is already in flight go first.
  void leave() { leaving_ = true; }
  bool leaving() const { return leaving_; }

  // What to send now. Returns the number of bytes written into `out`, and sets `todo` to
  // what they are — `kNothing` with zero bytes when there is nothing to do yet.
  size_t next(uint32_t now_ms, const Pending* pending, uint8_t* out, size_t capacity,
              Todo& todo);

  // Bytes that arrived. Reads one packet, applies it to this connection, and says both
  // what it was and what it meant. `used` is how many bytes it consumed, zero unless the
  // refusal is `kNone`; the caller keeps the rest for next time.
  // Nothing here is timed: a packet that arrived is an answer, and when it arrived only
  // matters to the timeouts in `next()`, which are measured from when the question was
  // asked.
  Refusal take(const uint8_t* data, size_t size, Incoming& out, size_t& used, Effect& effect);

  const Session& session() const { return session_; }
  Link link() const { return session_.link(); }
  Trouble trouble() const { return trouble_; }
  uint8_t return_code() const { return return_code_; }
  bool waiting_for_ack() const { return in_flight_ != 0; }
  uint16_t in_flight() const { return in_flight_; }
  uint8_t attempts_made() const { return attempts_; }
  uint32_t retransmissions() const { return retransmissions_; }
  // How long to wait before opening the socket again, after this many failed attempts.
  uint32_t backoff_ms() const;
  // Connections that ended without this node ever getting online, since the last one that
  // did. It is what the backoff is measured from, and it is reset by success and by
  // nothing else.
  uint32_t failures() const { return failures_; }
  // Messages the client refused to write at all — a topic or a payload it cannot put in a
  // packet. The caller is told to drop each one; this is how many there have been.
  uint32_t unwritable() const { return unwritable_; }
  // Commands acknowledged late because too many arrived at once. It is not a loss — the
  // broker sends them again — but it is a number rather than a silence.
  uint32_t unacknowledged() const { return unacknowledged_; }

 private:
  enum class Step {
    kClosed,
    kSendConnect,
    kAwaitConnAck,
    kSendSubscribe,
    kAwaitSubAck,
    kReady,
    kFinished,  // something went unanswered; the caller has not closed the socket yet
  };

  // Four commands owed an acknowledgement at once. The hub sends one at a time and waits
  // for the answer; four is room for a burst without a buffer nobody can account for.
  static constexpr size_t kOwed = 4;

  uint16_t take_packet_id();
  void give_up(Trouble trouble);
  bool allowed_to_publish(const Pending& pending) const;
  size_t wrote(size_t bytes, uint32_t now_ms);

  const Connect& connect_;
  const char* commands_topic_;
  Timings timings_;
  Session session_;

  Step step_ = Step::kClosed;
  Trouble trouble_ = Trouble::kNone;
  uint8_t return_code_ = 0;

  uint32_t since_ = 0;        // when the answer this step is waiting for was asked for
  uint32_t last_write_ = 0;   // the keepalive is measured from anything we send
  uint32_t pinged_at_ = 0;
  bool waiting_pong_ = false;

  uint16_t next_packet_id_ = 1;
  uint16_t subscribe_id_ = 0;
  uint16_t in_flight_ = 0;
  uint32_t sent_at_ = 0;
  uint8_t attempts_ = 0;
  bool announcing_ = false;   // what is in flight is the retained state

  uint16_t owed_[kOwed] = {};
  size_t owed_count_ = 0;

  uint32_t failures_ = 0;
  bool leaving_ = false;
  uint32_t retransmissions_ = 0;
  uint32_t unacknowledged_ = 0;
  uint32_t unwritable_ = 0;
  char connection_id_[kUuidText] = {};
};

}  // namespace mqtt
}  // namespace sentry

#endif  // SENTRY_CLIENT_H
