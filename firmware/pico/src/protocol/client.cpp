#include "sentry/client.h"

#include <cstring>

#include "sentry/names.h"

namespace sentry {
namespace mqtt {
namespace {

// The clock is a counter that wraps. Unsigned subtraction wraps with it, so an interval is
// right across the wrap and wrong only for one longer than 49 days, which no timeout here
// is.
bool elapsed(uint32_t now, uint32_t since, uint32_t limit) {
  return static_cast<uint32_t>(now - since) >= limit;
}

}  // namespace

Client::Client(const Connect& connect, const char* commands_topic, const Timings& timings)
    : connect_(connect), commands_topic_(commands_topic), timings_(timings) {}

bool Client::opened(uint32_t now_ms, const char* connection_id) {
  if (step_ != Step::kClosed) return false;
  if (!is_uuid(connection_id)) return false;
  std::memcpy(connection_id_, connection_id, std::strlen(connection_id) + 1);
  step_ = Step::kSendConnect;
  trouble_ = Trouble::kNone;
  return_code_ = 0;
  since_ = now_ms;
  last_write_ = now_ms;
  waiting_pong_ = false;
  in_flight_ = 0;
  attempts_ = 0;
  announcing_ = false;
  owed_count_ = 0;
  return true;
}

void Client::closed() {
  if (step_ == Step::kClosed) return;
  // A connection that ended before this node was online is what the backoff counts. One
  // that was online and dropped afterwards starts again at the short wait: the link
  // worked once, so the next attempt is worth making promptly. A link this client gave up
  // on has already been counted and already let go of its session.
  if (step_ != Step::kFinished) {
    if (session_.link() != Link::kOnline) ++failures_;
    session_.dropped();
  }
  step_ = Step::kClosed;
  in_flight_ = 0;
  attempts_ = 0;
  announcing_ = false;
  owed_count_ = 0;
  waiting_pong_ = false;
  leaving_ = false;
}

uint16_t Client::take_packet_id() {
  const uint16_t id = next_packet_id_;
  next_packet_id_ = static_cast<uint16_t>(next_packet_id_ + 1);
  if (next_packet_id_ == 0) next_packet_id_ = 1;  // zero is not a packet id
  return id;
}

void Client::give_up(Trouble trouble) {
  trouble_ = trouble;
  step_ = Step::kFinished;
  // The socket may still be open — only the caller can close one — but the connection is
  // over, and a session that still called itself online would be saying this node may
  // publish over a link that has stopped answering.
  if (session_.link() != Link::kOnline) ++failures_;
  session_.dropped();
}

bool Client::allowed_to_publish(const Pending& pending) const {
  // The retained state is what makes a node online, so it is the one thing that may go out
  // before it is. Everything else waits for the hub to have been told this node is here.
  return pending.announcement ? session_.ready_to_announce() : session_.may_publish_events();
}

size_t Client::wrote(size_t bytes, uint32_t now_ms) {
  last_write_ = now_ms;
  return bytes;
}

uint32_t Client::backoff_ms() const {
  if (failures_ == 0) return 0;
  uint32_t wait = timings_.first_backoff_ms;
  for (uint32_t index = 1; index < failures_; ++index) {
    if (wait >= timings_.max_backoff_ms / 2) return timings_.max_backoff_ms;
    wait *= 2;
  }
  return wait > timings_.max_backoff_ms ? timings_.max_backoff_ms : wait;
}

size_t Client::next(uint32_t now_ms, const Pending* pending, uint8_t* out, size_t capacity,
                    Todo& todo) {
  todo = Todo::kNothing;
  if (out == nullptr || capacity == 0) return 0;

  switch (step_) {
    case Step::kClosed:
      return 0;

    case Step::kFinished:
      todo = Todo::kGiveUp;
      return 0;

    case Step::kSendConnect: {
      const size_t bytes = write_connect(connect_, out, capacity);
      if (bytes == 0) {
        give_up(Trouble::kProtocol);
        todo = Todo::kGiveUp;
        return 0;
      }
      step_ = Step::kAwaitConnAck;
      since_ = now_ms;
      todo = Todo::kConnect;
      return wrote(bytes, now_ms);
    }

    case Step::kAwaitConnAck:
      if (elapsed(now_ms, since_, timings_.connack_ms)) {
        give_up(Trouble::kNoConnAck);
        todo = Todo::kGiveUp;
      }
      return 0;

    case Step::kSendSubscribe: {
      subscribe_id_ = take_packet_id();
      const size_t bytes = write_subscribe(subscribe_id_, commands_topic_, 1, out, capacity);
      if (bytes == 0) {
        give_up(Trouble::kProtocol);
        todo = Todo::kGiveUp;
        return 0;
      }
      step_ = Step::kAwaitSubAck;
      since_ = now_ms;
      todo = Todo::kSubscribe;
      return wrote(bytes, now_ms);
    }

    case Step::kAwaitSubAck:
      if (elapsed(now_ms, since_, timings_.suback_ms)) {
        give_up(Trouble::kNoSubAck);
        todo = Todo::kGiveUp;
      }
      return 0;

    case Step::kReady:
      break;
  }

  // A broker that stopped answering pings has stopped being a broker, whatever the socket
  // still says. This is checked before anything is sent, so nothing is written into a link
  // that is already over.
  if (waiting_pong_ && elapsed(now_ms, pinged_at_, timings_.pong_ms)) {
    give_up(Trouble::kNoPong);
    todo = Todo::kGiveUp;
    return 0;
  }

  // What was received is answered before what we would like to say. A command left
  // unacknowledged is one the hub will send again, and repeating work is worse than
  // waiting a loop to publish.
  if (owed_count_ > 0) {
    const size_t bytes = write_puback(owed_[0], out, capacity);
    if (bytes == 0) return 0;
    for (size_t index = 1; index < owed_count_; ++index) owed_[index - 1] = owed_[index];
    --owed_count_;
    todo = Todo::kAcknowledge;
    return wrote(bytes, now_ms);
  }

  // Everything owed has gone and nothing is in flight: a goodbye that jumped the queue
  // would leave the hub waiting for an answer that is never coming.
  if (leaving_ && in_flight_ == 0) {
    const size_t bytes = write_disconnect(out, capacity);
    if (bytes == 0) {
      give_up(Trouble::kProtocol);
      todo = Todo::kGiveUp;
      return 0;
    }
    // Not a failure and not something to try again: this node asked to go. The session
    // ends here, and `closed()` afterwards has nothing left to count.
    step_ = Step::kFinished;
    session_.dropped();
    leaving_ = false;
    todo = Todo::kLeave;
    return wrote(bytes, now_ms);
  }

  if (in_flight_ != 0) {
    if (!elapsed(now_ms, sent_at_, timings_.puback_ms)) return 0;
    if (attempts_ >= timings_.attempts) {
      // Not the message's fault and not a reason to drop it: the link is what has stopped
      // working, and the caller still holds the reading to send on the next one.
      give_up(Trouble::kNoPubAck);
      todo = Todo::kGiveUp;
      return 0;
    }
    // The same bytes and the same packet id. A caller that offers something else while a
    // publish is in flight has changed its mind about an event the broker may already
    // have, which is not something this client will help with.
    if (pending == nullptr) return 0;
    const size_t bytes =
        write_publish(pending->topic, pending->payload, pending->size, 1, pending->retain,
                      in_flight_, out, capacity, true);
    if (bytes == 0) return 0;
    ++attempts_;
    ++retransmissions_;
    sent_at_ = now_ms;
    todo = Todo::kRepublish;
    return wrote(bytes, now_ms);
  }

  if (pending != nullptr && allowed_to_publish(*pending)) {
    const uint16_t id = take_packet_id();
    const size_t bytes = write_publish(pending->topic, pending->payload, pending->size, 1,
                                       pending->retain, id, out, capacity, false);
    if (bytes == 0) {
      // A topic or a payload that cannot go in a packet. Holding it would stop the queue
      // behind it forever, so the caller is told to let it go and it is counted.
      ++unwritable_;
      todo = Todo::kDrop;
      return 0;
    }
    in_flight_ = id;
    attempts_ = 1;
    sent_at_ = now_ms;
    announcing_ = pending->announcement;
    todo = Todo::kPublish;
    return wrote(bytes, now_ms);
  }

  // Half a keepalive of silence. The broker gives up on a client after one and a half, so
  // two lost pings are survivable and a third is the link being gone.
  const uint32_t idle = static_cast<uint32_t>(timings_.keepalive_seconds) * 1000u / 2u;
  if (!waiting_pong_ && elapsed(now_ms, last_write_, idle)) {
    const size_t bytes = write_pingreq(out, capacity);
    if (bytes == 0) return 0;
    waiting_pong_ = true;
    pinged_at_ = now_ms;
    todo = Todo::kPing;
    return wrote(bytes, now_ms);
  }

  return 0;
}

Refusal Client::take(const uint8_t* data, size_t size, Incoming& out, size_t& used,
                     Effect& effect) {
  effect = Effect::kNothing;
  used = 0;
  if (step_ == Step::kClosed || step_ == Step::kFinished) return Refusal::kMalformed;

  const Refusal answer = read(data, size, out, used);
  if (answer == Refusal::kIncomplete) return answer;
  if (answer != Refusal::kNone) {
    // Bytes that are not a packet, or a packet no client can be sent: either way there is
    // no way to find where the next one starts, so the connection is over.
    give_up(Trouble::kProtocol);
    effect = Effect::kRefused;
    return answer;
  }

  switch (out.type) {
    case Type::kConnAck:
      if (step_ != Step::kAwaitConnAck) break;
      if (out.return_code != 0) {
        return_code_ = out.return_code;
        give_up(Trouble::kRefused);
        effect = Effect::kRefused;
        return answer;
      }
      // We asked for a clean session. A broker that says it kept one is answering a
      // different connection than the one we made.
      if (connect_.clean_session && out.session_present) break;
      if (!session_.connected(connection_id_)) break;
      step_ = Step::kSendSubscribe;
      effect = Effect::kConnected;
      return answer;

    case Type::kSubAck:
      if (step_ != Step::kAwaitSubAck || out.packet_id != subscribe_id_) break;
      // 0x80 is the broker refusing; anything other than the QoS 1 that was asked for is
      // commands arriving at a quality of service nobody chose. Neither is a subscription,
      // and a node that carried on would be listening for grants it may never be given.
      if (out.granted_qos != 1) {
        give_up(Trouble::kRefused);
        effect = Effect::kRefused;
        return answer;
      }
      session_.subscribed();
      step_ = Step::kReady;
      effect = Effect::kListening;
      return answer;

    case Type::kPubAck:
      if (step_ != Step::kReady) break;
      if (out.packet_id != in_flight_ || in_flight_ == 0) return answer;  // nothing of ours
      in_flight_ = 0;
      attempts_ = 0;
      if (announcing_) {
        announcing_ = false;
        session_.announced();
        failures_ = 0;  // a connection that got this far worked
        effect = Effect::kAnnounced;
      } else {
        effect = Effect::kDelivered;
      }
      return answer;

    case Type::kPublish:
      if (step_ != Step::kReady) break;
      if (out.qos == 1) {
        if (owed_count_ < kOwed) {
          owed_[owed_count_++] = out.packet_id;
        } else {
          // More at once than there is room to answer. The broker will send it again,
          // and the lease remembers the commands already answered, so this costs a
          // repeat rather than a command.
          ++unacknowledged_;
        }
      }
      return answer;

    case Type::kPingResp:
      waiting_pong_ = false;
      return answer;

    default:
      break;
  }

  // A packet that cannot belong to this conversation at this point in it.
  give_up(Trouble::kProtocol);
  effect = Effect::kRefused;
  return answer;
}

}  // namespace mqtt
}  // namespace sentry
