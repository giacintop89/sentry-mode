// What is in flight, what is owed, and when a link has stopped being one.
//
// The broker in here is hand-written bytes from the MQTT 3.1.1 specification rather than
// anything from this firmware: there is no server-side writer to borrow, which is what
// makes these packets an independent statement of what the node has to cope with.

#include <cstring>
#include <vector>

#include "harness.h"
#include "sentry/client.h"

using sentry::Link;
using sentry::mqtt::Client;
using sentry::mqtt::Connect;
using sentry::mqtt::Effect;
using sentry::mqtt::Incoming;
using sentry::mqtt::Pending;
using sentry::mqtt::Refusal;
using sentry::mqtt::Timings;
using sentry::mqtt::Todo;
using sentry::mqtt::Trouble;
using sentry::mqtt::Type;

namespace {

const char* kTopic = "sentry/v1/nodes/pico-1/commands";
const char* kEvents = "sentry/v1/nodes/pico-1/events";
const char* kState = "sentry/v1/nodes/pico-1/state";
const char* kFirst = "9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb";
const char* kSecond = "3f1b7c0e-8d4a-4e2b-9f61-0a5c8d7e4b12";

using Bytes = std::vector<uint8_t>;

Bytes connack(uint8_t code, bool session_present = false) {
  return {0x20, 0x02, static_cast<uint8_t>(session_present ? 1 : 0), code};
}

Bytes suback(uint16_t packet_id, uint8_t granted) {
  return {0x90, 0x03, static_cast<uint8_t>(packet_id >> 8), static_cast<uint8_t>(packet_id & 0xff),
          granted};
}

Bytes puback(uint16_t packet_id) {
  return {0x40, 0x02, static_cast<uint8_t>(packet_id >> 8), static_cast<uint8_t>(packet_id & 0xff)};
}

Bytes pingresp() { return {0xd0, 0x00}; }

// A command from the hub, at QoS 1 so that it is owed an acknowledgement.
Bytes command(uint16_t packet_id, const char* payload) {
  const size_t topic_length = std::strlen(kTopic);
  const size_t payload_length = std::strlen(payload);
  Bytes packet;
  packet.push_back(0x32);  // PUBLISH, QoS 1
  packet.push_back(static_cast<uint8_t>(2 + topic_length + 2 + payload_length));
  packet.push_back(static_cast<uint8_t>(topic_length >> 8));
  packet.push_back(static_cast<uint8_t>(topic_length & 0xff));
  for (size_t index = 0; index < topic_length; ++index) {
    packet.push_back(static_cast<uint8_t>(kTopic[index]));
  }
  packet.push_back(static_cast<uint8_t>(packet_id >> 8));
  packet.push_back(static_cast<uint8_t>(packet_id & 0xff));
  for (size_t index = 0; index < payload_length; ++index) {
    packet.push_back(static_cast<uint8_t>(payload[index]));
  }
  return packet;
}

// The packet id a PUBLISH carries, read back out of what the client wrote.
uint16_t published_id(const uint8_t* packet, size_t size) {
  size_t at = 1;
  while (at < size && (packet[at] & 0x80) != 0) ++at;
  ++at;
  const size_t topic_length = static_cast<size_t>(packet[at]) << 8 | packet[at + 1];
  at += 2 + topic_length;
  return static_cast<uint16_t>(static_cast<uint16_t>(packet[at]) << 8 | packet[at + 1]);
}

bool is_duplicate(const uint8_t* packet) { return (packet[0] & 0x08) != 0; }

struct Fixture {
  Connect connect;
  uint8_t out[512] = {};
  Todo todo = Todo::kNothing;
  Incoming in;
  Effect effect = Effect::kNothing;
  size_t used = 0;

  Fixture() { connect.client_id = "pico-1"; }

  Refusal give(Client& client, const Bytes& bytes) {
    return client.take(bytes.data(), bytes.size(), in, used, effect);
  }

  size_t ask(Client& client, uint32_t now, const Pending* pending = nullptr) {
    return client.next(now, pending, out, sizeof(out), todo);
  }
};

// Connect, be accepted, subscribe, be granted. The time stays at `now`.
bool bring_up(Fixture& fixture, Client& client, uint32_t now, const char* connection_id) {
  if (!client.opened(now, connection_id)) return false;
  if (fixture.ask(client, now) == 0 || fixture.todo != Todo::kConnect) return false;
  if (fixture.give(client, connack(0)) != Refusal::kNone) return false;
  if (fixture.effect != Effect::kConnected) return false;
  if (fixture.ask(client, now) == 0 || fixture.todo != Todo::kSubscribe) return false;
  const uint16_t subscribed = static_cast<uint16_t>(fixture.out[2] << 8 | fixture.out[3]);
  if (fixture.give(client, suback(subscribed, 1)) != Refusal::kNone) return false;
  return fixture.effect == Effect::kListening;
}

Pending an_event(const uint8_t* payload, size_t size) {
  Pending pending;
  pending.topic = kEvents;
  pending.payload = payload;
  pending.size = size;
  return pending;
}

}  // namespace

TEST(nothing_is_written_before_a_socket_is_open) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(fixture.ask(client, 0) == 0);
  CHECK(fixture.todo == Todo::kNothing);
  CHECK(client.link() == Link::kOffline);
  // And bytes that arrive on a connection nobody opened are not a conversation.
  CHECK(fixture.give(client, connack(0)) == Refusal::kMalformed);
}

TEST(a_connection_comes_up_in_one_order_and_only_that_one) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(client.opened(10, kFirst));
  CHECK(client.link() == Link::kOffline);  // the broker has not answered yet

  CHECK(fixture.ask(client, 10) > 0);
  CHECK(fixture.todo == Todo::kConnect);
  CHECK(fixture.out[0] == 0x10);
  CHECK(fixture.ask(client, 20) == 0);  // nothing else until the CONNACK

  CHECK(fixture.give(client, connack(0)) == Refusal::kNone);
  CHECK(fixture.effect == Effect::kConnected);
  CHECK(client.link() == Link::kSubscribing);

  CHECK(fixture.ask(client, 30) > 0);
  CHECK(fixture.todo == Todo::kSubscribe);
  CHECK(fixture.out[0] == 0x82);
  const uint16_t subscribed = static_cast<uint16_t>(fixture.out[2] << 8 | fixture.out[3]);
  CHECK(subscribed != 0);

  CHECK(fixture.give(client, suback(subscribed, 1)) == Refusal::kNone);
  CHECK(fixture.effect == Effect::kListening);
  CHECK(client.link() == Link::kListening);
  CHECK(!client.session().may_publish_events());
}

TEST(an_event_waits_for_the_node_to_be_online_and_the_state_does_not) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(bring_up(fixture, client, 100, kFirst));

  const uint8_t payload[] = "{\"reading\":1}";
  Pending event = an_event(payload, sizeof(payload) - 1);
  CHECK(fixture.ask(client, 110, &event) == 0);  // not online: it waits
  CHECK(fixture.todo == Todo::kNothing);
  CHECK(!client.waiting_for_ack());

  Pending state;
  state.topic = kState;
  state.payload = payload;
  state.size = sizeof(payload) - 1;
  state.retain = true;
  state.announcement = true;
  CHECK(fixture.ask(client, 120, &state) > 0);
  CHECK(fixture.todo == Todo::kPublish);
  CHECK((fixture.out[0] & 0x01) != 0);  // retained
  const uint16_t announced = published_id(fixture.out, sizeof(fixture.out));
  CHECK(client.in_flight() == announced);

  CHECK(fixture.give(client, puback(announced)) == Refusal::kNone);
  CHECK(fixture.effect == Effect::kAnnounced);
  CHECK(client.link() == Link::kOnline);

  CHECK(fixture.ask(client, 130, &event) > 0);
  CHECK(fixture.todo == Todo::kPublish);
}

TEST(a_publish_nobody_acknowledged_goes_again_with_the_same_id_and_dup) {
  Fixture fixture;
  Timings timings;
  timings.puback_ms = 1000;
  timings.attempts = 3;
  Client client(fixture.connect, kTopic, timings);
  CHECK(bring_up(fixture, client, 0, kFirst));
  const uint8_t payload[] = "{\"reading\":1}";
  Pending state;
  state.topic = kState;
  state.payload = payload;
  state.size = sizeof(payload) - 1;
  state.announcement = true;
  CHECK(fixture.ask(client, 0, &state) > 0);
  const uint16_t announced = published_id(fixture.out, sizeof(fixture.out));
  CHECK(fixture.give(client, puback(announced)) == Refusal::kNone);

  Pending event = an_event(payload, sizeof(payload) - 1);
  const size_t first = fixture.ask(client, 1000, &event);
  CHECK(first > 0);
  CHECK(fixture.todo == Todo::kPublish);
  CHECK(!is_duplicate(fixture.out));
  const uint16_t id = published_id(fixture.out, first);

  CHECK(fixture.ask(client, 1500, &event) == 0);  // the broker still has time
  const size_t again = fixture.ask(client, 2000, &event);
  CHECK(again == first);
  CHECK(fixture.todo == Todo::kRepublish);
  CHECK(is_duplicate(fixture.out));
  CHECK(published_id(fixture.out, again) == id);  // the same event, not a second one
  CHECK(client.retransmissions() == 1);

  CHECK(fixture.ask(client, 3000, &event) > 0);
  CHECK(fixture.todo == Todo::kRepublish);
  CHECK(client.attempts_made() == 3);

  // Three attempts and no answer: the link is the problem, and the reading is still the
  // caller's to send on the next one.
  CHECK(fixture.ask(client, 4000, &event) == 0);
  CHECK(fixture.todo == Todo::kGiveUp);
  CHECK(client.trouble() == Trouble::kNoPubAck);
  CHECK(client.waiting_for_ack());
  client.closed();
  CHECK(client.link() == Link::kOffline);
  CHECK(!client.waiting_for_ack());
}

TEST(an_acknowledged_publish_is_let_go_of_and_the_next_one_is_its_own) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(bring_up(fixture, client, 0, kFirst));
  const uint8_t payload[] = "{\"reading\":1}";
  Pending state;
  state.topic = kState;
  state.payload = payload;
  state.size = sizeof(payload) - 1;
  state.announcement = true;
  CHECK(fixture.ask(client, 0, &state) > 0);
  CHECK(fixture.give(client, puback(published_id(fixture.out, sizeof(fixture.out)))) ==
        Refusal::kNone);

  Pending event = an_event(payload, sizeof(payload) - 1);
  CHECK(fixture.ask(client, 10, &event) > 0);
  const uint16_t first = published_id(fixture.out, sizeof(fixture.out));
  CHECK(fixture.give(client, puback(first)) == Refusal::kNone);
  CHECK(fixture.effect == Effect::kDelivered);
  CHECK(!client.waiting_for_ack());

  CHECK(fixture.ask(client, 20, &event) > 0);
  const uint16_t second = published_id(fixture.out, sizeof(fixture.out));
  CHECK(second != first);
  // A PUBACK for nothing we sent is not an answer, and not a reason to drop the link.
  CHECK(fixture.give(client, puback(31000)) == Refusal::kNone);
  CHECK(fixture.effect == Effect::kNothing);
  CHECK(client.in_flight() == second);
}

TEST(a_command_is_acknowledged_before_the_node_says_anything_else) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(bring_up(fixture, client, 0, kFirst));
  const uint8_t payload[] = "{\"reading\":1}";
  Pending state;
  state.topic = kState;
  state.payload = payload;
  state.size = sizeof(payload) - 1;
  state.announcement = true;
  CHECK(fixture.ask(client, 0, &state) > 0);
  CHECK(fixture.give(client, puback(published_id(fixture.out, sizeof(fixture.out)))) ==
        Refusal::kNone);

  CHECK(fixture.give(client, command(77, "{\"action\":\"grant\"}")) == Refusal::kNone);
  CHECK(fixture.in.type == Type::kPublish);
  CHECK(fixture.effect == Effect::kNothing);  // the command itself is the caller's business

  Pending event = an_event(payload, sizeof(payload) - 1);
  const size_t bytes = fixture.ask(client, 10, &event);
  CHECK(bytes == 4);
  CHECK(fixture.todo == Todo::kAcknowledge);
  CHECK(fixture.out[0] == 0x40);
  CHECK(fixture.out[2] == 0 && fixture.out[3] == 77);
  CHECK(!client.waiting_for_ack());  // the event has not gone yet

  CHECK(fixture.ask(client, 20, &event) > 0);
  CHECK(fixture.todo == Todo::kPublish);
}

TEST(more_commands_at_once_than_there_is_room_to_answer_are_counted) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(bring_up(fixture, client, 0, kFirst));
  for (uint16_t id = 1; id <= 6; ++id) {
    CHECK(fixture.give(client, command(id, "{\"action\":\"stop\"}")) == Refusal::kNone);
  }
  CHECK(client.unacknowledged() == 2);
  // The four it took are answered in the order they arrived; the broker sends the rest
  // again, and the lease has already seen those command ids.
  for (uint16_t id = 1; id <= 4; ++id) {
    CHECK(fixture.ask(client, id) == 4);
    CHECK(fixture.todo == Todo::kAcknowledge);
    CHECK(fixture.out[3] == id);
  }
  CHECK(fixture.ask(client, 10) == 0);
}

TEST(a_broker_that_says_no_is_not_retried_at_it) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(client.opened(0, kFirst));
  CHECK(fixture.ask(client, 0) > 0);
  CHECK(fixture.give(client, connack(5)) == Refusal::kNone);  // not authorized
  CHECK(fixture.effect == Effect::kRefused);
  CHECK(client.trouble() == Trouble::kRefused);
  CHECK(client.return_code() == 5);
  CHECK(fixture.ask(client, 1) == 0);
  CHECK(fixture.todo == Todo::kGiveUp);
  CHECK(client.link() == Link::kOffline);
}

TEST(a_session_the_broker_kept_is_not_the_one_we_asked_for) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(client.opened(0, kFirst));
  CHECK(fixture.ask(client, 0) > 0);
  // We connect clean so that the subscription is confirmed every time. A broker answering
  // that it kept our session is answering a different connection.
  CHECK(fixture.give(client, connack(0, true)) == Refusal::kNone);
  CHECK(client.trouble() == Trouble::kProtocol);
  CHECK(client.link() == Link::kOffline);
}

TEST(a_subscription_granted_at_a_quality_nobody_asked_for_is_not_one) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(client.opened(0, kFirst));
  CHECK(fixture.ask(client, 0) > 0);
  CHECK(fixture.give(client, connack(0)) == Refusal::kNone);
  CHECK(fixture.ask(client, 0) > 0);
  const uint16_t subscribed = static_cast<uint16_t>(fixture.out[2] << 8 | fixture.out[3]);
  CHECK(fixture.give(client, suback(subscribed, 0x80)) == Refusal::kNone);
  CHECK(fixture.effect == Effect::kRefused);
  CHECK(client.trouble() == Trouble::kRefused);
  CHECK(client.link() == Link::kOffline);

  // And a grant of QoS 0 is commands that may be lost, which is the same answer.
  Fixture second;
  Client downgraded(second.connect, kTopic);
  CHECK(downgraded.opened(0, kFirst));
  CHECK(second.ask(downgraded, 0) > 0);
  CHECK(second.give(downgraded, connack(0)) == Refusal::kNone);
  CHECK(second.ask(downgraded, 0) > 0);
  const uint16_t asked = static_cast<uint16_t>(second.out[2] << 8 | second.out[3]);
  CHECK(second.give(downgraded, suback(asked, 0)) == Refusal::kNone);
  CHECK(downgraded.trouble() == Trouble::kRefused);
}

TEST(a_suback_for_a_subscription_nobody_asked_for_ends_the_link) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(client.opened(0, kFirst));
  CHECK(fixture.ask(client, 0) > 0);
  CHECK(fixture.give(client, connack(0)) == Refusal::kNone);
  CHECK(fixture.ask(client, 0) > 0);
  CHECK(fixture.give(client, suback(9999, 1)) == Refusal::kNone);
  CHECK(client.trouble() == Trouble::kProtocol);
}

TEST(an_answer_that_never_comes_is_not_waited_on_forever) {
  Fixture fixture;
  Timings timings;
  timings.connack_ms = 5000;
  timings.suback_ms = 4000;
  Client client(fixture.connect, kTopic, timings);
  CHECK(client.opened(1000, kFirst));
  CHECK(fixture.ask(client, 1000) > 0);
  CHECK(fixture.ask(client, 5999) == 0);
  CHECK(fixture.todo == Todo::kNothing);
  CHECK(fixture.ask(client, 6000) == 0);
  CHECK(fixture.todo == Todo::kGiveUp);
  CHECK(client.trouble() == Trouble::kNoConnAck);

  client.closed();
  Fixture second;
  Client subscriber(second.connect, kTopic, timings);
  CHECK(subscriber.opened(0, kFirst));
  CHECK(second.ask(subscriber, 0) > 0);
  CHECK(second.give(subscriber, connack(0)) == Refusal::kNone);
  CHECK(second.ask(subscriber, 100) > 0);  // the SUBSCRIBE, at 100
  CHECK(second.ask(subscriber, 4099) == 0);
  CHECK(second.todo == Todo::kNothing);
  CHECK(second.ask(subscriber, 4100) == 0);
  CHECK(second.todo == Todo::kGiveUp);
  CHECK(subscriber.trouble() == Trouble::kNoSubAck);
}

TEST(silence_is_broken_by_a_ping_and_ended_by_one_that_is_not_answered) {
  Fixture fixture;
  Timings timings;
  timings.keepalive_seconds = 30;
  timings.pong_ms = 10000;
  Client client(fixture.connect, kTopic, timings);
  CHECK(bring_up(fixture, client, 0, kFirst));

  CHECK(fixture.ask(client, 14999) == 0);
  CHECK(fixture.ask(client, 15000) == 2);
  CHECK(fixture.todo == Todo::kPing);
  CHECK(fixture.out[0] == 0xc0 && fixture.out[1] == 0x00);
  CHECK(fixture.ask(client, 20000) == 0);  // one ping at a time

  CHECK(fixture.give(client, pingresp()) == Refusal::kNone);
  CHECK(fixture.effect == Effect::kNothing);
  CHECK(fixture.ask(client, 29999) == 0);
  CHECK(fixture.ask(client, 30000) == 2);  // half a keepalive after the last one
  CHECK(fixture.todo == Todo::kPing);

  CHECK(fixture.ask(client, 39999) == 0);
  CHECK(fixture.ask(client, 40000) == 0);
  CHECK(fixture.todo == Todo::kGiveUp);
  CHECK(client.trouble() == Trouble::kNoPong);
}

TEST(anything_sent_is_a_sign_of_life_and_postpones_the_ping) {
  Fixture fixture;
  Timings timings;
  timings.keepalive_seconds = 30;
  Client client(fixture.connect, kTopic, timings);
  CHECK(bring_up(fixture, client, 0, kFirst));
  const uint8_t payload[] = "{\"reading\":1}";
  Pending state;
  state.topic = kState;
  state.payload = payload;
  state.size = sizeof(payload) - 1;
  state.announcement = true;
  CHECK(fixture.ask(client, 10000, &state) > 0);
  CHECK(fixture.give(client, puback(published_id(fixture.out, sizeof(fixture.out)))) ==
        Refusal::kNone);

  CHECK(fixture.ask(client, 15000) == 0);  // the publish was the last thing said
  CHECK(fixture.ask(client, 25000) == 2);
  CHECK(fixture.todo == Todo::kPing);
}

TEST(a_message_that_cannot_be_written_is_dropped_rather_than_holding_the_queue) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(bring_up(fixture, client, 0, kFirst));
  const uint8_t payload[] = "{\"reading\":1}";
  Pending state;
  state.topic = kState;
  state.payload = payload;
  state.size = sizeof(payload) - 1;
  state.announcement = true;
  CHECK(fixture.ask(client, 0, &state) > 0);
  CHECK(fixture.give(client, puback(published_id(fixture.out, sizeof(fixture.out)))) ==
        Refusal::kNone);

  Pending wrong = an_event(payload, sizeof(payload) - 1);
  wrong.topic = "sentry/v1/nodes/pico-1/events/#";  // a wildcard is not somewhere to publish
  CHECK(fixture.ask(client, 10, &wrong) == 0);
  CHECK(fixture.todo == Todo::kDrop);
  CHECK(client.unwritable() == 1);
  CHECK(!client.waiting_for_ack());
  CHECK(client.link() == Link::kOnline);  // the link is fine; the message was not
}

TEST(a_connection_that_never_worked_is_waited_on_longer_each_time) {
  Fixture fixture;
  Timings timings;
  timings.first_backoff_ms = 1000;
  timings.max_backoff_ms = 8000;
  Client client(fixture.connect, kTopic, timings);
  CHECK(client.backoff_ms() == 0);

  const char* ids[] = {kFirst, kSecond, "7a2f4c81-3b6d-4e95-a0c7-2f8e1b4d6093",
                       "1c5e9a37-6d84-4b20-9f3a-8e7c2d510b46"};
  uint32_t expected[] = {1000, 2000, 4000};
  for (size_t attempt = 0; attempt < 3; ++attempt) {
    CHECK(client.opened(0, ids[attempt]));
    CHECK(fixture.ask(client, 0) > 0);
    client.closed();
    CHECK(client.failures() == attempt + 1);
    CHECK(client.backoff_ms() == expected[attempt]);
  }
  CHECK(client.opened(0, "2b8d4f16-9c37-4a5e-8071-3d6f9e2c5a84"));
  CHECK(fixture.ask(client, 0) > 0);
  client.closed();
  CHECK(client.backoff_ms() == 8000);  // and no further
}

TEST(a_connection_that_worked_starts_the_waiting_over) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(client.opened(0, kFirst));
  CHECK(fixture.ask(client, 0) > 0);
  client.closed();
  CHECK(client.backoff_ms() > 0);

  CHECK(bring_up(fixture, client, 100, kSecond));
  const uint8_t payload[] = "{\"online\":true}";
  Pending state;
  state.topic = kState;
  state.payload = payload;
  state.size = sizeof(payload) - 1;
  state.announcement = true;
  CHECK(fixture.ask(client, 100, &state) > 0);
  CHECK(fixture.give(client, puback(published_id(fixture.out, sizeof(fixture.out)))) ==
        Refusal::kNone);
  CHECK(fixture.effect == Effect::kAnnounced);
  CHECK(client.failures() == 0);
  CHECK(client.backoff_ms() == 0);

  client.closed();  // a link that worked and then dropped is tried again at once
  CHECK(client.failures() == 0);
}

TEST(a_connection_identifier_is_never_used_twice) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(client.opened(0, kFirst));
  CHECK(fixture.ask(client, 0) > 0);
  CHECK(fixture.give(client, connack(0)) == Refusal::kNone);
  client.closed();

  CHECK(client.opened(10, kFirst));  // the socket is a socket; the identifier is not
  CHECK(fixture.ask(client, 10) > 0);
  CHECK(fixture.give(client, connack(0)) == Refusal::kNone);
  CHECK(client.trouble() == Trouble::kProtocol);
  CHECK(client.link() == Link::kOffline);
}

TEST(a_packet_no_client_can_be_sent_ends_the_conversation) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(bring_up(fixture, client, 0, kFirst));
  const Bytes subscribe = {0x82, 0x05, 0x00, 0x01, 0x00, 0x01, 0x00};
  CHECK(fixture.give(client, subscribe) != Refusal::kNone);
  CHECK(client.trouble() == Trouble::kProtocol);
  CHECK(fixture.effect == Effect::kRefused);
}

TEST(half_a_packet_is_waited_for_rather_than_refused) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(client.opened(0, kFirst));
  CHECK(fixture.ask(client, 0) > 0);
  const Bytes half = {0x20, 0x02, 0x00};
  CHECK(fixture.give(client, half) == Refusal::kIncomplete);
  CHECK(fixture.used == 0);
  CHECK(client.trouble() == Trouble::kNone);
  CHECK(fixture.give(client, connack(0)) == Refusal::kNone);
  CHECK(fixture.effect == Effect::kConnected);
}

TEST(packet_ids_count_up_past_the_end_and_never_reach_zero) {
  Fixture fixture;
  Client client(fixture.connect, kTopic);
  CHECK(bring_up(fixture, client, 0, kFirst));
  const uint8_t payload[] = "{\"reading\":1}";
  Pending state;
  state.topic = kState;
  state.payload = payload;
  state.size = sizeof(payload) - 1;
  state.announcement = true;
  CHECK(fixture.ask(client, 0, &state) > 0);
  CHECK(fixture.give(client, puback(published_id(fixture.out, sizeof(fixture.out)))) ==
        Refusal::kNone);

  Pending event = an_event(payload, sizeof(payload) - 1);
  uint16_t seen = 0;
  for (uint32_t count = 0; count < 70000; ++count) {
    CHECK(fixture.ask(client, count + 1, &event) > 0);
    const uint16_t id = published_id(fixture.out, sizeof(fixture.out));
    CHECK(id != 0);
    seen = id;
    CHECK(fixture.give(client, puback(id)) == Refusal::kNone);
  }
  CHECK(seen != 0);
}

int main() { return harness::run_all("client"); }
