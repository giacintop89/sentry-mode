// The packets this node writes, byte for byte, and what it refuses to read.
//
// The expectations here are written from the MQTT 3.1.1 specification rather than from
// mqtt.cpp: a test that agrees with the code because it was copied from it proves nothing.
// `tools/mqtt_check.py` then decodes the same packets with a third implementation.

#include <cstring>
#include <string>

#include "harness.h"
#include "sentry/mqtt.h"

using sentry::mqtt::Connect;
using sentry::mqtt::Incoming;
using sentry::mqtt::Refusal;
using sentry::mqtt::Type;
using sentry::mqtt::Will;

namespace {

// Where the variable header starts: one byte of type, then however many bytes the
// remaining length took. Working it out rather than assuming one is the difference between
// a test that reads the packet and a test that reads the code that wrote it.
size_t body_at(const uint8_t* packet) {
  size_t at = 1;
  while ((packet[at] & 0x80) != 0) ++at;
  return at + 1;
}

std::string hex(const uint8_t* data, size_t size) {
  static const char kDigits[] = "0123456789abcdef";
  std::string out;
  for (size_t index = 0; index < size; ++index) {
    out.push_back(kDigits[data[index] >> 4]);
    out.push_back(kDigits[data[index] & 0x0f]);
  }
  return out;
}

}  // namespace

TEST(a_connect_says_which_protocol_it_is_and_who_is_speaking) {
  Connect connect;
  connect.client_id = "pico-1";
  connect.keepalive_seconds = 30;
  uint8_t out[256];
  const size_t size = sentry::mqtt::write_connect(connect, out, sizeof(out));
  CHECK(size == 20);
  // 10 = CONNECT, 12 = 18 bytes after the header: "MQTT", level 4, clean session, 30s,
  // then a six character client id.
  CHECK_TEXT(hex(out, size).c_str(), "101200044d5154540402001e00067069636f2d31");
}

TEST(a_connection_registers_the_goodbye_before_it_can_need_it) {
  const char* goodbye = R"({"schema_version":1,"node_id":"pico-1","online":false})";
  Will will;
  will.topic = "sentry/v1/nodes/pico-1/state";
  will.payload = reinterpret_cast<const uint8_t*>(goodbye);
  will.size = std::strlen(goodbye);
  Connect connect;
  connect.client_id = "pico-1";
  connect.will = &will;

  uint8_t out[512];
  const size_t size = sentry::mqtt::write_connect(connect, out, sizeof(out));
  CHECK(size > 0);
  const uint8_t* variable = out + body_at(out);
  // Will flag, will QoS 1 and will retain: 0x02 | 0x04 | 0x08 | 0x20 = 0x2e.
  // "MQTT" with its length is six bytes, then the protocol level, then the flags.
  CHECK(variable[7] == 0x2e);
  // The will topic and payload follow the client id, each with its own two-byte length.
  const uint8_t* at = variable + 10 + 2 + 6;
  CHECK(at[0] == 0 && at[1] == 28);
  CHECK(std::memcmp(at + 2, will.topic, 28) == 0);
  const uint8_t* payload = at + 2 + 28;
  CHECK(((static_cast<size_t>(payload[0]) << 8) | payload[1]) == will.size);
  CHECK(std::memcmp(payload + 2, goodbye, will.size) == 0);
}

TEST(a_will_this_node_could_not_deliver_stops_the_connection_rather_than_going_without_one) {
  const char* payload = "{}";
  Will will;
  will.payload = reinterpret_cast<const uint8_t*>(payload);
  will.size = 2;
  Connect connect;
  connect.client_id = "pico-1";
  connect.will = &will;
  uint8_t out[256];

  will.topic = nullptr;
  CHECK(sentry::mqtt::write_connect(connect, out, sizeof(out)) == 0);
  will.topic = "sentry/v1/nodes/+/state";  // a wildcard is not something to publish to
  CHECK(sentry::mqtt::write_connect(connect, out, sizeof(out)) == 0);
  will.topic = "sentry/v1/nodes/pico-1/state";
  will.qos = 2;  // never implemented here, and not quietly written as 1
  CHECK(sentry::mqtt::write_connect(connect, out, sizeof(out)) == 0);
  will.qos = 1;
  CHECK(sentry::mqtt::write_connect(connect, out, sizeof(out)) > 0);
}

TEST(a_connect_without_a_name_is_not_written) {
  Connect connect;
  uint8_t out[256];
  CHECK(sentry::mqtt::write_connect(connect, out, sizeof(out)) == 0);
  connect.client_id = "";
  CHECK(sentry::mqtt::write_connect(connect, out, sizeof(out)) == 0);
  connect.client_id = "pico-1";
  CHECK(sentry::mqtt::write_connect(connect, out, 19) == 0);  // one byte short
  CHECK(sentry::mqtt::write_connect(connect, out, 20) == 20);
}

TEST(a_subscribe_names_the_topic_the_quality_and_the_packet_it_will_be_answered_by) {
  uint8_t out[128];
  const size_t size =
      sentry::mqtt::write_subscribe(1, "sentry/v1/nodes/pico-1/commands", 1, out, sizeof(out));
  CHECK(size == 38);
  CHECK(out[0] == 0x82);  // the reserved bits of SUBSCRIBE are 0010 and brokers check them
  CHECK(out[1] == 36);
  CHECK(out[2] == 0 && out[3] == 1);
  CHECK(out[4] == 0 && out[5] == 31);
  CHECK(out[size - 1] == 1);
  CHECK(sentry::mqtt::write_subscribe(0, "a", 1, out, sizeof(out)) == 0);
  CHECK(sentry::mqtt::write_subscribe(1, "a", 2, out, sizeof(out)) == 0);
}

TEST(an_event_is_published_at_least_once_and_says_which_packet_it_is) {
  const char* body = R"({"schema_version":1})";
  uint8_t out[256];
  const size_t size =
      sentry::mqtt::write_publish("sentry/v1/nodes/pico-1/events",
                                  reinterpret_cast<const uint8_t*>(body), std::strlen(body), 1,
                                  false, 7, out, sizeof(out));
  CHECK(size == 2 + 2 + 29 + 2 + 20);
  CHECK(out[0] == 0x32);  // PUBLISH, QoS 1, not retained, not a duplicate
  CHECK(out[1] == 53);
  CHECK(out[2] == 0 && out[3] == 29);
  CHECK(out[2 + 2 + 29] == 0 && out[2 + 2 + 29 + 1] == 7);
}

TEST(the_retained_state_is_the_one_message_a_node_leaves_behind) {
  const char* body = "{}";
  uint8_t out[128];
  const size_t size = sentry::mqtt::write_publish("sentry/v1/nodes/pico-1/state",
                                                  reinterpret_cast<const uint8_t*>(body), 2, 1,
                                                  true, 9, out, sizeof(out));
  CHECK(size > 0);
  CHECK(out[0] == 0x33);  // the retain bit is the low one
}

TEST(health_goes_out_at_most_once_and_carries_no_packet_id) {
  const char* body = "{}";
  uint8_t out[128];
  const size_t size = sentry::mqtt::write_publish("sentry/v1/nodes/pico-1/health",
                                                  reinterpret_cast<const uint8_t*>(body), 2, 0,
                                                  false, 0, out, sizeof(out));
  // Two bytes of header, the topic with its length, the payload, and no packet id.
  CHECK(size == 2 + 2 + 29 + 2);
  CHECK(out[0] == 0x30);
}

TEST(a_publish_that_could_not_be_answered_is_not_written) {
  const char* body = "{}";
  uint8_t out[128];
  // QoS 1 with no packet id: there would be nothing for the PUBACK to name.
  CHECK(sentry::mqtt::write_publish("a/b", reinterpret_cast<const uint8_t*>(body), 2, 1, false, 0,
                                    out, sizeof(out)) == 0);
  CHECK(sentry::mqtt::write_publish("a/b", reinterpret_cast<const uint8_t*>(body), 2, 2, false, 1,
                                    out, sizeof(out)) == 0);
  CHECK(sentry::mqtt::write_publish("a/#", reinterpret_cast<const uint8_t*>(body), 2, 1, false, 1,
                                    out, sizeof(out)) == 0);
  CHECK(sentry::mqtt::write_publish("", reinterpret_cast<const uint8_t*>(body), 2, 1, false, 1, out,
                                    sizeof(out)) == 0);
}

TEST(the_short_packets_are_the_ones_the_specification_says) {
  uint8_t out[8];
  CHECK(sentry::mqtt::write_puback(5, out, sizeof(out)) == 4);
  CHECK_TEXT(hex(out, 4).c_str(), "40020005");
  CHECK(sentry::mqtt::write_pingreq(out, sizeof(out)) == 2);
  CHECK_TEXT(hex(out, 2).c_str(), "c000");
  CHECK(sentry::mqtt::write_disconnect(out, sizeof(out)) == 2);
  CHECK_TEXT(hex(out, 2).c_str(), "e000");
  CHECK(sentry::mqtt::write_puback(0, out, sizeof(out)) == 0);
  CHECK(sentry::mqtt::write_pingreq(out, 1) == 0);
}

TEST(a_connack_says_whether_the_broker_took_this_node) {
  const uint8_t accepted[] = {0x20, 0x02, 0x00, 0x00};
  Incoming found;
  size_t used = 0;
  CHECK(sentry::mqtt::read(accepted, sizeof(accepted), found, used) == Refusal::kNone);
  CHECK(used == 4);
  CHECK(found.type == Type::kConnAck);
  CHECK(found.return_code == 0 && !found.session_present);

  const uint8_t refused[] = {0x20, 0x02, 0x01, 0x05};
  CHECK(sentry::mqtt::read(refused, sizeof(refused), found, used) == Refusal::kNone);
  CHECK(found.session_present);
  CHECK(found.return_code == 5);
  CHECK(std::strstr(sentry::mqtt::name_of_return_code(5), "not authorised") != nullptr);
}

TEST(a_command_arrives_as_a_topic_and_a_payload_that_are_not_copied) {
  // PUBLISH, QoS 1, topic "a/b", packet id 3, payload "hi"
  const uint8_t packet[] = {0x32, 0x09, 0x00, 0x03, 'a', '/', 'b', 0x00, 0x03, 'h', 'i'};
  Incoming found;
  size_t used = 0;
  CHECK(sentry::mqtt::read(packet, sizeof(packet), found, used) == Refusal::kNone);
  CHECK(used == sizeof(packet));
  CHECK(found.type == Type::kPublish);
  CHECK(found.qos == 1 && found.packet_id == 3);
  CHECK(found.topic_size == 3 && std::memcmp(found.topic, "a/b", 3) == 0);
  CHECK(found.payload_size == 2 && std::memcmp(found.payload, "hi", 2) == 0);
  // Nothing was copied: what was read points into the caller's own buffer.
  CHECK(found.topic == reinterpret_cast<const char*>(packet + 4));
}

TEST(half_a_packet_is_kept_rather_than_guessed_at) {
  const uint8_t packet[] = {0x32, 0x09, 0x00, 0x03, 'a', '/', 'b', 0x00, 0x03, 'h', 'i'};
  Incoming found;
  size_t used = 0;
  for (size_t size = 0; size < sizeof(packet); ++size) {
    CHECK(sentry::mqtt::read(packet, size, found, used) == Refusal::kIncomplete);
    CHECK(used == 0);
  }
  CHECK(sentry::mqtt::read(packet, sizeof(packet), found, used) == Refusal::kNone);
}

TEST(two_packets_in_one_read_are_two_packets) {
  const uint8_t stream[] = {0xd0, 0x00, 0x40, 0x02, 0x00, 0x04};
  Incoming found;
  size_t used = 0;
  CHECK(sentry::mqtt::read(stream, sizeof(stream), found, used) == Refusal::kNone);
  CHECK(found.type == Type::kPingResp && used == 2);
  CHECK(sentry::mqtt::read(stream + used, sizeof(stream) - used, found, used) == Refusal::kNone);
  CHECK(found.type == Type::kPubAck && found.packet_id == 4 && used == 4);
}

TEST(a_quality_this_node_never_implemented_is_refused_rather_than_answered_as_another) {
  const uint8_t qos2[] = {0x34, 0x09, 0x00, 0x03, 'a', '/', 'b', 0x00, 0x03, 'h', 'i'};
  Incoming found;
  size_t used = 0;
  CHECK(sentry::mqtt::read(qos2, sizeof(qos2), found, used) == Refusal::kUnsupported);
}

TEST(a_packet_only_a_client_sends_is_not_something_a_broker_may_send_back) {
  const uint8_t connect[] = {0x10, 0x02, 0x00, 0x00};
  const uint8_t subscribe[] = {0x82, 0x02, 0x00, 0x01};
  Incoming found;
  size_t used = 0;
  CHECK(sentry::mqtt::read(connect, sizeof(connect), found, used) == Refusal::kUnsupported);
  CHECK(sentry::mqtt::read(subscribe, sizeof(subscribe), found, used) == Refusal::kUnsupported);
}

TEST(a_length_nobody_could_have_meant_is_refused) {
  // Five continuation bytes: the protocol allows four.
  const uint8_t endless[] = {0x30, 0xff, 0xff, 0xff, 0xff, 0xff};
  Incoming found;
  size_t used = 0;
  CHECK(sentry::mqtt::read(endless, sizeof(endless), found, used) == Refusal::kMalformed);

  // A length this node has nowhere to put: 300000 bytes, announced in four bytes.
  const uint8_t enormous[] = {0x30, 0xe0, 0xa7, 0x12, 0x00, 0x00};
  CHECK(sentry::mqtt::read(enormous, sizeof(enormous), found, used) == Refusal::kUnsupported);
}

TEST(a_suback_names_the_subscription_and_what_it_was_granted) {
  const uint8_t packet[] = {0x90, 0x03, 0x00, 0x01, 0x01};
  Incoming found;
  size_t used = 0;
  CHECK(sentry::mqtt::read(packet, sizeof(packet), found, used) == Refusal::kNone);
  CHECK(found.type == Type::kSubAck);
  CHECK(found.packet_id == 1 && found.granted_qos == 1);

  // 0x80 is the broker saying no, which is not a quality and must not read as one.
  const uint8_t refused[] = {0x90, 0x03, 0x00, 0x01, 0x80};
  CHECK(sentry::mqtt::read(refused, sizeof(refused), found, used) == Refusal::kNone);
  CHECK(found.granted_qos == 0x80);
}

int main() { return harness::run_all("mqtt"); }
