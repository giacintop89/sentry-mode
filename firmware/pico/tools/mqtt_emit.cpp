// Print the packets the firmware would put on the wire, as hex, one per line.
//
// `tools/mqtt_check.py` decodes them with an implementation written from the specification
// rather than from this code, and hands the payloads to the hub's models. A codec checked
// only by tests written beside it agrees with itself; this is what makes it agree with
// somebody else.

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "sentry/control.h"
#include "sentry/event.h"
#include "sentry/mqtt.h"
#include "sentry/topics.h"

namespace {

void emit(const char* what, const uint8_t* packet, size_t size) {
  if (size == 0) {
    std::fprintf(stderr, "refused to write the %s packet\n", what);
    std::exit(1);
  }
  std::printf("%s\t", what);
  for (size_t index = 0; index < size; ++index) std::printf("%02x", packet[index]);
  std::putchar('\n');
}

}  // namespace

int main() {
  const char* node_id = "pico-ingresso";
  const char* boot_id = "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611";
  const char* connection_id = "9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb";

  char state_topic[sentry::kMaxTopicText] = {};
  char events_topic[sentry::kMaxTopicText] = {};
  char health_topic[sentry::kMaxTopicText] = {};
  char acks_topic[sentry::kMaxTopicText] = {};
  char commands_topic[sentry::kMaxTopicText] = {};
  const bool topics =
      sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kState, state_topic,
                    sizeof(state_topic)) &&
      sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kEvents, events_topic,
                    sizeof(events_topic)) &&
      sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kHealth, health_topic,
                    sizeof(health_topic)) &&
      sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kAcks, acks_topic,
                    sizeof(acks_topic)) &&
      sentry::topic(sentry::kTopicPrefix, node_id, sentry::Channel::kCommands, commands_topic,
                    sizeof(commands_topic));
  if (!topics) return 1;

  // The goodbye, written by the firmware itself, carried by the CONNECT that registers it.
  char goodbye[256];
  const size_t goodbye_size = sentry::write_goodbye(node_id, boot_id, connection_id,
                                                    std::strlen(state_topic), goodbye,
                                                    sizeof(goodbye));
  if (goodbye_size == 0) return 1;

  uint8_t packet[sentry::mqtt::kMaxPacketBytes];

  sentry::mqtt::Will will;
  will.topic = state_topic;
  will.payload = reinterpret_cast<const uint8_t*>(goodbye);
  will.size = goodbye_size;
  sentry::mqtt::Connect connect;
  connect.client_id = node_id;
  connect.keepalive_seconds = 30;
  connect.will = &will;
  emit("connect", packet, sentry::mqtt::write_connect(connect, packet, sizeof(packet)));

  emit("subscribe", packet,
       sentry::mqtt::write_subscribe(1, commands_topic, 1, packet, sizeof(packet)));

  char body[1024];
  sentry::Reading reading;
  reading.event_id = "0f6c4a1e-9a5b-4c2d-8e11-5b7c9d0a1f23";
  reading.node_id = node_id;
  reading.source_id = "pir-1";
  reading.boot_id = boot_id;
  reading.sequence = 41;
  reading.kind = "sensor.motion";
  reading.occurred_at = "2026-09-16T21:04:07.512Z";
  reading.clock = sentry::Clock::kSynced;
  reading.value = sentry::Value::of(true);
  sentry::Delivery delivery;
  delivery.connection_id = connection_id;
  delivery.hub_epoch = 7;
  const size_t event_size = sentry::write_event(reading, delivery, body, sizeof(body));
  if (event_size == 0) return 1;
  emit("publish.events", packet,
       sentry::mqtt::write_publish(events_topic, reinterpret_cast<const uint8_t*>(body),
                                   event_size, 1, false, 2, packet, sizeof(packet)));

  emit("publish.state", packet,
       sentry::mqtt::write_publish(state_topic, reinterpret_cast<const uint8_t*>(goodbye),
                                   goodbye_size, 1, true, 3, packet, sizeof(packet)));

  // Health and the answer come from the firmware's own serializers too: what is being
  // checked here is the packet around them, and a payload typed into this file would be one
  // more thing to keep in step with the contract.
  sentry::Health health;
  health.node_id = node_id;
  health.boot_id = boot_id;
  health.uptime_seconds = 61.0;
  health.clock = sentry::Clock::kSynced;
  health.queue.events = 0;
  health.queue.bytes = 0;
  const size_t health_size = sentry::write_health(health, body, sizeof(body));
  if (health_size == 0) return 1;
  emit("publish.health", packet,
       sentry::mqtt::write_publish(health_topic, reinterpret_cast<const uint8_t*>(body),
                                   health_size, 0, false, 0, packet, sizeof(packet)));

  const size_t ack_size = sentry::write_ack("3f1b7c0e-8d4a-4e2b-9f61-0a5c8d7e4b12", node_id,
                                            sentry::Outcome::kApplied, nullptr, body,
                                            sizeof(body));
  if (ack_size == 0) return 1;
  emit("publish.acks", packet,
       sentry::mqtt::write_publish(acks_topic, reinterpret_cast<const uint8_t*>(body), ack_size, 1,
                                   false, 4, packet, sizeof(packet)));

  emit("puback", packet, sentry::mqtt::write_puback(9, packet, sizeof(packet)));
  emit("pingreq", packet, sentry::mqtt::write_pingreq(packet, sizeof(packet)));
  emit("disconnect", packet, sentry::mqtt::write_disconnect(packet, sizeof(packet)));
  return 0;
}
