#include "sentry/mqtt.h"

#include <cstring>

namespace sentry {
namespace mqtt {
namespace {

constexpr uint8_t kProtocolLevel = 4;  // 3.1.1
constexpr size_t kMaxTopicBytes = 255;
constexpr size_t kMaxClientIdBytes = 64;

// A length-prefixed string, as every field in this protocol is spelled.
bool put_text(uint8_t* out, size_t capacity, size_t& at, const char* text, size_t length) {
  if (length > 0xFFFF) return false;
  if (at + 2 + length > capacity) return false;
  out[at++] = static_cast<uint8_t>(length >> 8);
  out[at++] = static_cast<uint8_t>(length & 0xFF);
  std::memcpy(out + at, text, length);
  at += length;
  return true;
}

// The remaining length: seven bits at a time, most significant bit meaning "more". Four
// bytes is the whole of what the protocol allows, and this codec refuses a fifth rather
// than reading a number nobody can have meant.
size_t put_remaining_length(uint8_t* out, size_t capacity, size_t at, size_t length) {
  size_t written = 0;
  do {
    if (at + written >= capacity) return 0;
    uint8_t byte = static_cast<uint8_t>(length % 128);
    length /= 128;
    if (length > 0) byte = static_cast<uint8_t>(byte | 0x80);
    out[at + written++] = byte;
    if (written > 4) return 0;
  } while (length > 0);
  return written;
}

// How many bytes the remaining length will take, so the body can be written after the
// header without moving it afterwards.
size_t length_of_length(size_t length) {
  if (length < 128) return 1;
  if (length < 16384) return 2;
  if (length < 2097152) return 3;
  return 4;
}

bool read_remaining_length(const uint8_t* data, size_t size, size_t at, size_t& length,
                           size_t& used, Refusal& refusal) {
  size_t multiplier = 1;
  length = 0;
  used = 0;
  for (size_t index = 0; index < 4; ++index) {
    if (at + index >= size) {
      refusal = Refusal::kIncomplete;
      return false;
    }
    const uint8_t byte = data[at + index];
    length += static_cast<size_t>(byte & 0x7F) * multiplier;
    multiplier *= 128;
    ++used;
    if ((byte & 0x80) == 0) return true;
  }
  refusal = Refusal::kMalformed;
  return false;
}

bool read_uint16(const uint8_t* data, size_t size, size_t& at, uint16_t& out) {
  if (at + 2 > size) return false;
  out = static_cast<uint16_t>((static_cast<uint16_t>(data[at]) << 8) | data[at + 1]);
  at += 2;
  return true;
}

bool sensible_topic(const char* topic, size_t& length) {
  if (topic == nullptr) return false;
  length = std::strlen(topic);
  if (length == 0 || length > kMaxTopicBytes) return false;
  // A node publishes to topics it built itself; a wildcard in one would mean the topic was
  // taken from somewhere it should not have been.
  for (size_t index = 0; index < length; ++index) {
    const char letter = topic[index];
    if (letter == '#' || letter == '+' || letter == '\0') return false;
  }
  return true;
}

}  // namespace

size_t write_connect(const Connect& connect, uint8_t* out, size_t capacity) {
  if (out == nullptr || connect.client_id == nullptr) return 0;
  const size_t client_id_length = std::strlen(connect.client_id);
  if (client_id_length == 0 || client_id_length > kMaxClientIdBytes) return 0;

  uint8_t flags = 0;
  if (connect.clean_session) flags = static_cast<uint8_t>(flags | 0x02);
  size_t will_topic_length = 0;
  if (connect.will != nullptr) {
    // A will this node cannot deliver is refused here rather than turned into a connection
    // with no will at all, which is the failure nobody notices until it matters.
    if (!sensible_topic(connect.will->topic, will_topic_length)) return 0;
    if (connect.will->qos > 1) return 0;
    if (connect.will->size > 0xFFFF) return 0;
    if (connect.will->size > 0 && connect.will->payload == nullptr) return 0;
    flags = static_cast<uint8_t>(flags | 0x04);
    flags = static_cast<uint8_t>(flags | (connect.will->qos << 3));
    if (connect.will->retain) flags = static_cast<uint8_t>(flags | 0x20);
  }
  size_t username_length = 0;
  if (connect.username != nullptr) {
    username_length = std::strlen(connect.username);
    if (username_length == 0 || username_length > kMaxClientIdBytes) return 0;
    flags = static_cast<uint8_t>(flags | 0x80);
  }

  size_t body = 10;  // the protocol name, the level, the flags and the keepalive
  body += 2 + client_id_length;
  if (connect.will != nullptr) body += 2 + will_topic_length + 2 + connect.will->size;
  if (connect.username != nullptr) body += 2 + username_length;
  if (body > kMaxPacketBytes) return 0;

  const size_t header = 1 + length_of_length(body);
  if (header + body > capacity) return 0;

  size_t at = 0;
  out[at++] = static_cast<uint8_t>(static_cast<uint8_t>(Type::kConnect) << 4);
  const size_t length_bytes = put_remaining_length(out, capacity, at, body);
  if (length_bytes == 0) return 0;
  at += length_bytes;

  if (!put_text(out, capacity, at, "MQTT", 4)) return 0;
  if (at + 4 > capacity) return 0;
  out[at++] = kProtocolLevel;
  out[at++] = flags;
  out[at++] = static_cast<uint8_t>(connect.keepalive_seconds >> 8);
  out[at++] = static_cast<uint8_t>(connect.keepalive_seconds & 0xFF);
  if (!put_text(out, capacity, at, connect.client_id, client_id_length)) return 0;
  if (connect.will != nullptr) {
    if (!put_text(out, capacity, at, connect.will->topic, will_topic_length)) return 0;
    if (!put_text(out, capacity, at, reinterpret_cast<const char*>(connect.will->payload),
                  connect.will->size)) {
      return 0;
    }
  }
  if (connect.username != nullptr) {
    if (!put_text(out, capacity, at, connect.username, username_length)) return 0;
  }
  return at;
}

size_t write_subscribe(uint16_t packet_id, const char* topic, uint8_t qos, uint8_t* out,
                       size_t capacity) {
  if (out == nullptr || packet_id == 0 || qos > 1) return 0;
  if (topic == nullptr) return 0;
  const size_t topic_length = std::strlen(topic);
  // A subscription may name a wildcard: it is the one place where one is meant.
  if (topic_length == 0 || topic_length > kMaxTopicBytes) return 0;

  const size_t body = 2 + 2 + topic_length + 1;
  if (1 + length_of_length(body) + body > capacity) return 0;

  size_t at = 0;
  // The reserved bits of SUBSCRIBE are 0010 and a broker may drop the connection for any
  // other value, which is a hard fault to find from the other end.
  out[at++] = static_cast<uint8_t>((static_cast<uint8_t>(Type::kSubscribe) << 4) | 0x02);
  const size_t length_bytes = put_remaining_length(out, capacity, at, body);
  if (length_bytes == 0) return 0;
  at += length_bytes;
  out[at++] = static_cast<uint8_t>(packet_id >> 8);
  out[at++] = static_cast<uint8_t>(packet_id & 0xFF);
  if (!put_text(out, capacity, at, topic, topic_length)) return 0;
  out[at++] = qos;
  return at;
}

size_t write_publish(const char* topic, const uint8_t* payload, size_t size, uint8_t qos,
                     bool retain, uint16_t packet_id, uint8_t* out, size_t capacity,
                     bool duplicate) {
  if (out == nullptr || qos > 1) return 0;
  // A repeat of something that was never acknowledged, and at QoS 0 nothing is.
  if (duplicate && qos == 0) return 0;
  if (size > 0 && payload == nullptr) return 0;
  size_t topic_length = 0;
  if (!sensible_topic(topic, topic_length)) return 0;
  // At QoS 1 the packet id is what the PUBACK will name. Zero is not one.
  if (qos == 1 && packet_id == 0) return 0;

  const size_t body = 2 + topic_length + (qos == 1 ? 2u : 0u) + size;
  if (body > kMaxPacketBytes) return 0;
  if (1 + length_of_length(body) + body > capacity) return 0;

  size_t at = 0;
  uint8_t first = static_cast<uint8_t>(static_cast<uint8_t>(Type::kPublish) << 4);
  first = static_cast<uint8_t>(first | (qos << 1));
  if (retain) first = static_cast<uint8_t>(first | 0x01);
  if (duplicate) first = static_cast<uint8_t>(first | 0x08);
  out[at++] = first;
  const size_t length_bytes = put_remaining_length(out, capacity, at, body);
  if (length_bytes == 0) return 0;
  at += length_bytes;
  if (!put_text(out, capacity, at, topic, topic_length)) return 0;
  if (qos == 1) {
    out[at++] = static_cast<uint8_t>(packet_id >> 8);
    out[at++] = static_cast<uint8_t>(packet_id & 0xFF);
  }
  if (size > 0) {
    if (at + size > capacity) return 0;
    std::memcpy(out + at, payload, size);
    at += size;
  }
  return at;
}

size_t write_puback(uint16_t packet_id, uint8_t* out, size_t capacity) {
  if (out == nullptr || capacity < 4 || packet_id == 0) return 0;
  out[0] = static_cast<uint8_t>(static_cast<uint8_t>(Type::kPubAck) << 4);
  out[1] = 2;
  out[2] = static_cast<uint8_t>(packet_id >> 8);
  out[3] = static_cast<uint8_t>(packet_id & 0xFF);
  return 4;
}

size_t write_pingreq(uint8_t* out, size_t capacity) {
  if (out == nullptr || capacity < 2) return 0;
  out[0] = static_cast<uint8_t>(static_cast<uint8_t>(Type::kPingReq) << 4);
  out[1] = 0;
  return 2;
}

size_t write_disconnect(uint8_t* out, size_t capacity) {
  if (out == nullptr || capacity < 2) return 0;
  out[0] = static_cast<uint8_t>(static_cast<uint8_t>(Type::kDisconnect) << 4);
  out[1] = 0;
  return 2;
}

Refusal read(const uint8_t* data, size_t size, Incoming& out, size_t& used) {
  used = 0;
  if (data == nullptr || size < 2) return Refusal::kIncomplete;

  const uint8_t first = data[0];
  const uint8_t type = static_cast<uint8_t>(first >> 4);
  const uint8_t flags = static_cast<uint8_t>(first & 0x0F);

  size_t body = 0;
  size_t length_bytes = 0;
  Refusal refusal = Refusal::kMalformed;
  if (!read_remaining_length(data, size, 1, body, length_bytes, refusal)) return refusal;
  const size_t whole = 1 + length_bytes + body;
  if (body > kMaxPacketBytes) return Refusal::kUnsupported;
  if (whole > size) return Refusal::kIncomplete;

  Incoming found;
  size_t at = 1 + length_bytes;
  const size_t end = whole;

  switch (type) {
    case static_cast<uint8_t>(Type::kConnAck): {
      if (body != 2) return Refusal::kMalformed;
      found.type = Type::kConnAck;
      found.session_present = (data[at] & 0x01) != 0;
      found.return_code = data[at + 1];
      break;
    }
    case static_cast<uint8_t>(Type::kPublish): {
      found.type = Type::kPublish;
      found.duplicate = (flags & 0x08) != 0;
      found.qos = static_cast<uint8_t>((flags >> 1) & 0x03);
      found.retain = (flags & 0x01) != 0;
      // QoS 2 is not implemented. Answering it as though it were QoS 1 would leave the
      // broker holding a message it thinks is still in flight.
      if (found.qos > 1) return Refusal::kUnsupported;
      uint16_t topic_length = 0;
      if (!read_uint16(data, end, at, topic_length)) return Refusal::kMalformed;
      if (at + topic_length > end) return Refusal::kMalformed;
      found.topic = reinterpret_cast<const char*>(data + at);
      found.topic_size = topic_length;
      at += topic_length;
      if (found.qos == 1) {
        if (!read_uint16(data, end, at, found.packet_id)) return Refusal::kMalformed;
        if (found.packet_id == 0) return Refusal::kMalformed;
      }
      found.payload = data + at;
      found.payload_size = end - at;
      break;
    }
    case static_cast<uint8_t>(Type::kPubAck): {
      if (body != 2) return Refusal::kMalformed;
      found.type = Type::kPubAck;
      if (!read_uint16(data, end, at, found.packet_id)) return Refusal::kMalformed;
      break;
    }
    case static_cast<uint8_t>(Type::kSubAck): {
      if (body != 3) return Refusal::kMalformed;
      found.type = Type::kSubAck;
      if (!read_uint16(data, end, at, found.packet_id)) return Refusal::kMalformed;
      found.granted_qos = data[at];
      break;
    }
    case static_cast<uint8_t>(Type::kPingResp): {
      if (body != 0) return Refusal::kMalformed;
      found.type = Type::kPingResp;
      break;
    }
    default:
      // CONNECT, SUBSCRIBE, PINGREQ and the rest are things a client sends. A broker
      // sending one is either broken or not a broker, and either way this is not a packet
      // to act on.
      return Refusal::kUnsupported;
  }

  out = found;
  used = whole;
  return Refusal::kNone;
}

const char* name_of_return_code(uint8_t code) {
  switch (code) {
    case 0:
      return "accepted";
    case 1:
      return "the broker does not speak this version of MQTT";
    case 2:
      return "the broker refused this client id";
    case 3:
      return "the broker is not available";
    case 4:
      return "the broker refused these credentials";
    case 5:
      return "this node is not authorised: check the certificate and the ACL";
    default:
      return "refused, for a reason this version does not know";
  }
}

}  // namespace mqtt
}  // namespace sentry
