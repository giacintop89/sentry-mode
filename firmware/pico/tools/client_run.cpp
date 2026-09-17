// Drive the client through one whole connection and print what it put on the wire.
//
// `mqtt_emit.cpp` shows that each packet is written correctly. This shows the order they
// come in and what the client does when an answer does not arrive: the CONNECT, the
// SUBSCRIBE that has to be confirmed before anything is announced, the retained state, an
// event, that same event again with DUP when no PUBACK came back, the acknowledgement a
// command is owed, and the ping that keeps a quiet link open.
//
// The broker in here is bytes written from the specification rather than anything from
// this firmware. `tools/mqtt_check.py` then decodes the whole conversation with a third
// implementation and asks the hub whether it would accept what was said.

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "sentry/client.h"
#include "sentry/control.h"
#include "sentry/event.h"
#include "sentry/topics.h"

namespace {

const char* kNode = "pico-ingresso";
const char* kBoot = "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611";
const char* kConnection = "9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb";
constexpr uint16_t kCommandId = 42;

void fail(const char* what) {
  std::fprintf(stderr, "the client %s\n", what);
  std::exit(1);
}

void emit(const char* what, const uint8_t* packet, size_t size) {
  if (size == 0) fail("wrote nothing where a packet was expected");
  std::printf("%s\t", what);
  for (size_t index = 0; index < size; ++index) std::printf("%02x", packet[index]);
  std::putchar('\n');
}

// What a broker sends, spelled out here so that nothing in this conversation comes from
// the code being checked.
void connack(sentry::mqtt::Client& client) {
  const uint8_t bytes[] = {0x20, 0x02, 0x00, 0x00};
  sentry::mqtt::Incoming in;
  sentry::mqtt::Effect effect = sentry::mqtt::Effect::kNothing;
  size_t used = 0;
  if (client.take(bytes, sizeof(bytes), in, used, effect) != sentry::mqtt::Refusal::kNone ||
      effect != sentry::mqtt::Effect::kConnected) {
    fail("would not take a CONNACK that accepted it");
  }
}

void suback(sentry::mqtt::Client& client, uint16_t packet_id) {
  const uint8_t bytes[] = {0x90, 0x03, static_cast<uint8_t>(packet_id >> 8),
                           static_cast<uint8_t>(packet_id & 0xff), 0x01};
  sentry::mqtt::Incoming in;
  sentry::mqtt::Effect effect = sentry::mqtt::Effect::kNothing;
  size_t used = 0;
  if (client.take(bytes, sizeof(bytes), in, used, effect) != sentry::mqtt::Refusal::kNone ||
      effect != sentry::mqtt::Effect::kListening) {
    fail("would not take the SUBACK for its own subscription");
  }
}

void puback(sentry::mqtt::Client& client, uint16_t packet_id, sentry::mqtt::Effect expected) {
  const uint8_t bytes[] = {0x40, 0x02, static_cast<uint8_t>(packet_id >> 8),
                           static_cast<uint8_t>(packet_id & 0xff)};
  sentry::mqtt::Incoming in;
  sentry::mqtt::Effect effect = sentry::mqtt::Effect::kNothing;
  size_t used = 0;
  if (client.take(bytes, sizeof(bytes), in, used, effect) != sentry::mqtt::Refusal::kNone ||
      effect != expected) {
    fail("did not treat a PUBACK as the answer to what it sent");
  }
}

// A command from the hub, at QoS 1, which is what the acknowledgement below is for.
void deliver_command(sentry::mqtt::Client& client, const char* topic) {
  uint8_t bytes[256];
  const char* body = "{\"schema_version\":1,\"command\":{}}";
  const size_t topic_length = std::strlen(topic);
  const size_t body_length = std::strlen(body);
  size_t at = 0;
  bytes[at++] = 0x32;
  bytes[at++] = static_cast<uint8_t>(2 + topic_length + 2 + body_length);
  bytes[at++] = static_cast<uint8_t>(topic_length >> 8);
  bytes[at++] = static_cast<uint8_t>(topic_length & 0xff);
  std::memcpy(bytes + at, topic, topic_length);
  at += topic_length;
  bytes[at++] = static_cast<uint8_t>(kCommandId >> 8);
  bytes[at++] = static_cast<uint8_t>(kCommandId & 0xff);
  std::memcpy(bytes + at, body, body_length);
  at += body_length;
  sentry::mqtt::Incoming in;
  sentry::mqtt::Effect effect = sentry::mqtt::Effect::kNothing;
  size_t used = 0;
  if (client.take(bytes, at, in, used, effect) != sentry::mqtt::Refusal::kNone ||
      in.type != sentry::mqtt::Type::kPublish) {
    fail("would not read a command the hub published to it");
  }
}

uint16_t packet_id_of_subscribe(const uint8_t* packet) {
  return static_cast<uint16_t>(static_cast<uint16_t>(packet[2]) << 8 | packet[3]);
}

uint16_t packet_id_of_publish(const uint8_t* packet, size_t size) {
  size_t at = 1;
  while (at < size && (packet[at] & 0x80) != 0) ++at;
  ++at;
  const size_t topic_length = static_cast<size_t>(packet[at]) << 8 | packet[at + 1];
  at += 2 + topic_length;
  return static_cast<uint16_t>(static_cast<uint16_t>(packet[at]) << 8 | packet[at + 1]);
}

}  // namespace

int main() {
  char state_topic[sentry::kMaxTopicText] = {};
  char events_topic[sentry::kMaxTopicText] = {};
  char commands_topic[sentry::kMaxTopicText] = {};
  if (!sentry::topic(sentry::kTopicPrefix, kNode, sentry::Channel::kState, state_topic,
                     sizeof(state_topic)) ||
      !sentry::topic(sentry::kTopicPrefix, kNode, sentry::Channel::kEvents, events_topic,
                     sizeof(events_topic)) ||
      !sentry::topic(sentry::kTopicPrefix, kNode, sentry::Channel::kCommands, commands_topic,
                     sizeof(commands_topic))) {
    fail("could not build its own topics");
  }

  char goodbye[256];
  const size_t goodbye_size = sentry::write_goodbye(kNode, kBoot, kConnection,
                                                    std::strlen(state_topic), goodbye,
                                                    sizeof(goodbye));
  if (goodbye_size == 0) fail("could not write the goodbye its will carries");

  sentry::mqtt::Will will;
  will.topic = state_topic;
  will.payload = reinterpret_cast<const uint8_t*>(goodbye);
  will.size = goodbye_size;
  sentry::mqtt::Connect connect;
  connect.client_id = kNode;
  connect.keepalive_seconds = 30;
  connect.will = &will;

  sentry::mqtt::Timings timings;
  timings.puback_ms = 5000;
  sentry::mqtt::Client client(connect, commands_topic, timings);

  uint8_t out[sentry::mqtt::kMaxPacketBytes];
  sentry::mqtt::Todo todo = sentry::mqtt::Todo::kNothing;

  if (!client.opened(0, kConnection)) fail("refused a socket of its own");
  size_t size = client.next(0, nullptr, out, sizeof(out), todo);
  if (todo != sentry::mqtt::Todo::kConnect) fail("did not begin with a CONNECT");
  emit("1.connect", out, size);
  connack(client);

  size = client.next(10, nullptr, out, sizeof(out), todo);
  if (todo != sentry::mqtt::Todo::kSubscribe) fail("did not subscribe before anything else");
  emit("2.subscribe", out, size);
  const uint16_t subscribed = packet_id_of_subscribe(out);

  // An event offered before the node is online is held: the hub has not been told this
  // node is here, and a reading from a node the hub thinks is gone is a reading nobody
  // asked for.
  char body[1024];
  sentry::Reading reading;
  reading.event_id = "0f6c4a1e-9a5b-4c2d-8e11-5b7c9d0a1f23";
  reading.node_id = kNode;
  reading.source_id = "pir-1";
  reading.boot_id = kBoot;
  reading.sequence = 41;
  reading.kind = "sensor.motion";
  reading.occurred_at = "2026-09-16T21:04:07.512Z";
  reading.clock = sentry::Clock::kSynced;
  reading.value = sentry::Value::of(true);
  sentry::Delivery delivery;
  delivery.connection_id = kConnection;
  delivery.hub_epoch = 7;
  const size_t event_size = sentry::write_event(reading, delivery, body, sizeof(body));
  if (event_size == 0) fail("could not write the event it was going to publish");
  sentry::mqtt::Pending event;
  event.topic = events_topic;
  event.payload = reinterpret_cast<const uint8_t*>(body);
  event.size = event_size;

  if (client.next(20, &event, out, sizeof(out), todo) != 0 ||
      todo != sentry::mqtt::Todo::kNothing) {
    fail("published an event before its subscription was confirmed");
  }
  suback(client, subscribed);
  if (client.next(30, &event, out, sizeof(out), todo) != 0) {
    fail("published an event before the hub had been told it was here");
  }

  char hello[512];
  sentry::State here;
  here.node_id = kNode;
  here.boot_id = kBoot;
  here.connection_id = kConnection;
  here.online = true;
  here.firmware_version = "pico-0.1.0";
  here.profile = "sensor-presence";
  const size_t hello_size = sentry::write_state(here, hello, sizeof(hello));
  if (hello_size == 0) fail("could not write the state that says it is here");
  sentry::mqtt::Pending state;
  state.topic = state_topic;
  state.payload = reinterpret_cast<const uint8_t*>(hello);
  state.size = hello_size;
  state.retain = true;
  state.announcement = true;
  size = client.next(40, &state, out, sizeof(out), todo);
  if (todo != sentry::mqtt::Todo::kPublish) fail("did not announce itself when it could");
  emit("3.publish.state", out, size);
  puback(client, packet_id_of_publish(out, size), sentry::mqtt::Effect::kAnnounced);

  size = client.next(50, &event, out, sizeof(out), todo);
  if (todo != sentry::mqtt::Todo::kPublish) fail("did not publish the event it was holding");
  emit("4.publish.event", out, size);
  const uint16_t in_flight = packet_id_of_publish(out, size);

  // No PUBACK. The same event goes again, with DUP and the same packet id: one reading
  // arriving twice, which the hub deduplicates, rather than two readings.
  size = client.next(5050, &event, out, sizeof(out), todo);
  if (todo != sentry::mqtt::Todo::kRepublish) fail("gave up on an unacknowledged event");
  emit("5.republish.event", out, size);
  if (packet_id_of_publish(out, size) != in_flight) fail("sent the same event under a new id");
  puback(client, in_flight, sentry::mqtt::Effect::kDelivered);

  deliver_command(client, commands_topic);
  size = client.next(5100, nullptr, out, sizeof(out), todo);
  if (todo != sentry::mqtt::Todo::kAcknowledge) fail("left a command unacknowledged");
  emit("6.puback", out, size);

  // Half a keepalive of silence, measured from the last thing it said.
  size = client.next(5100 + 15000, nullptr, out, sizeof(out), todo);
  if (todo != sentry::mqtt::Todo::kPing) fail("let a quiet link go unpinged");
  emit("7.ping", out, size);

  if (client.link() != sentry::Link::kOnline) fail("did not end the conversation online");
  return 0;
}
