// MQTT 3.1.1, as much of it as a satellite uses and no more.
//
// This is a codec and nothing else: it turns a packet into bytes in a buffer the caller
// owns, and reads bytes back into a packet whose strings point into that same buffer. It
// has no socket, no clock and no memory of what it sent, because the order things happen
// in belongs to `session.h` and the permission to publish belongs to `lease.h`.
//
// The node speaks 3.1.1 while the Linux agent speaks 5. That is deliberate: 3.1.1 is what
// fits here, and the broker takes both. Nothing in the hub's contract depends on the
// version — the topics, the payloads and the will are the same either way.
//
// What it refuses is as much of the point as what it writes. QoS 2 is not implemented and
// is refused rather than downgraded; a remaining length longer than four bytes, or longer
// than the buffer, is refused rather than trusted; a packet type a client can never receive
// is refused rather than ignored.

#ifndef SENTRY_MQTT_H
#define SENTRY_MQTT_H

#include <cstddef>
#include <cstdint>

namespace sentry {
namespace mqtt {

// The largest packet this node will write or read. An event is capped at 2 kB by
// `kMaxEventBytes`, a command is smaller, and a satellite has nothing else to say.
inline constexpr size_t kMaxPacketBytes = 2560;
// The header of a QoS 1 PUBLISH: type, up to four length bytes, the topic and a packet id.
inline constexpr size_t kMaxPublishOverhead = 7;

enum class Type : uint8_t {
  kConnect = 1,
  kConnAck = 2,
  kPublish = 3,
  kPubAck = 4,
  kSubscribe = 8,
  kSubAck = 9,
  kPingReq = 12,
  kPingResp = 13,
  kDisconnect = 14,
};

// What the broker says on this node's behalf when the link dies without a goodbye. It is
// part of CONNECT and cannot be set afterwards, which is why it is here and not in a call
// of its own: by the time a node knows it is in trouble it can no longer register one.
struct Will {
  const char* topic = nullptr;
  const uint8_t* payload = nullptr;
  size_t size = 0;
  uint8_t qos = 1;
  bool retain = true;
};

struct Connect {
  const char* client_id = nullptr;
  uint16_t keepalive_seconds = 30;
  // A satellite always starts clean: the subscription is confirmed on every connection
  // before it says it is here, and a session the broker kept would let it skip that.
  bool clean_session = true;
  const Will* will = nullptr;
  // The certificate is the identity. A username is written only when the broker was also
  // configured to want one, and it is never a password: there is no password field here.
  const char* username = nullptr;
};

// Every writer returns how many bytes it wrote, or 0 when the packet would not fit or a
// field is not one this codec will produce.
size_t write_connect(const Connect& connect, uint8_t* out, size_t capacity);
size_t write_subscribe(uint16_t packet_id, const char* topic, uint8_t qos, uint8_t* out,
                       size_t capacity);
size_t write_publish(const char* topic, const uint8_t* payload, size_t size, uint8_t qos,
                     bool retain, uint16_t packet_id, uint8_t* out, size_t capacity);
size_t write_puback(uint16_t packet_id, uint8_t* out, size_t capacity);
size_t write_pingreq(uint8_t* out, size_t capacity);
size_t write_disconnect(uint8_t* out, size_t capacity);

enum class Refusal {
  kNone,
  kIncomplete,   // the rest of the packet has not arrived yet; keep the bytes and wait
  kMalformed,    // it cannot be a packet, and the connection should be dropped
  kUnsupported,  // it is a packet, and not one this node will act on
};

// A packet read back. `topic` and `payload` point into the caller's buffer and are valid
// for as long as it is: nothing here copies, because a board that copied every message
// would need somewhere to put it.
struct Incoming {
  Type type = Type::kPingResp;
  bool session_present = false;  // CONNACK
  uint8_t return_code = 0;       // CONNACK
  const char* topic = nullptr;   // PUBLISH
  size_t topic_size = 0;
  const uint8_t* payload = nullptr;
  size_t payload_size = 0;
  uint8_t qos = 0;
  bool retain = false;
  bool duplicate = false;
  uint16_t packet_id = 0;    // PUBLISH at QoS 1, PUBACK, SUBACK
  uint8_t granted_qos = 0;   // SUBACK
};

// Read one packet from the front of `data`. `used` is how many bytes it took, which is
// zero unless the answer is kNone.
Refusal read(const uint8_t* data, size_t size, Incoming& out, size_t& used);

// What a CONNACK's return code means, for a log line a person has to read.
const char* name_of_return_code(uint8_t code);

}  // namespace mqtt
}  // namespace sentry

#endif  // SENTRY_MQTT_H
