#include "sentry/identity.h"

#include <cstring>

#include "sentry/json.h"
#include "sentry/names.h"

namespace sentry {
namespace {

// A host name or a literal address, and nothing that could be a URL: whatever is here is
// handed to the TLS layer as the name to verify, and a value with a scheme or a path in it
// would be verified against something other than what it connects to.
bool is_host(const char* text) {
  size_t length = std::strlen(text);
  if (length == 0 || length > kMaxHostText - 1) return false;
  for (size_t index = 0; index < length; ++index) {
    char c = text[index];
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) continue;
    if ((c == '.' || c == '-') && index > 0 && index + 1 < length) continue;
    return false;
  }
  return true;
}

// An SSID is bytes, not text: it may hold anything a router was configured with, and this
// firmware only insists that it is something a person could have typed and that it fits.
bool is_ssid(const char* text) {
  size_t length = std::strlen(text);
  if (length == 0 || length > kMaxSsidText - 1) return false;
  for (size_t index = 0; index < length; ++index) {
    unsigned char c = static_cast<unsigned char>(text[index]);
    if (c < 0x20 || c == 0x7f) return false;
  }
  return true;
}

// Empty for an open network, 8 to 63 characters for a passphrase, or exactly 64 hex digits
// for a key that has already been derived. Anything else would be refused by the radio
// after a join that looks like a network problem, which is a bad way to learn about a typo.
bool is_passphrase(const char* text) {
  size_t length = std::strlen(text);
  if (length == 0) return true;
  if (length == 64) {
    for (size_t index = 0; index < 64; ++index) {
      char c = text[index];
      bool hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
      if (!hex) return false;
    }
    return true;
  }
  if (length < 8 || length > kMaxPassphraseText - 2) return false;
  for (size_t index = 0; index < length; ++index) {
    unsigned char c = static_cast<unsigned char>(text[index]);
    if (c < 0x20 || c == 0x7f) return false;
  }
  return true;
}

bool take_text(Reader& reader, char* out, size_t capacity) {
  size_t length = 0;
  if (!reader.string(out, capacity - 1, length)) return false;
  out[length] = '\0';
  return true;
}

}  // namespace

bool read_provisioning(const char* document, size_t size, Provisioning& out) {
  Provisioning found;
  bool has_node = false;
  bool has_host = false;
  bool has_port = false;

  Reader reader(document, size);
  if (!reader.begin_object()) return false;
  Span key;
  Kind kind = Kind::kEnd;
  while (reader.member(key, kind)) {
    if (kind == Kind::kEnd) break;
    // A field this firmware knows is read as what it is meant to be. Written as something
    // else, it is a record somebody got wrong, and the wrong network or the wrong broker is
    // worse than no identity at all.
    if (key.is("node_id")) {
      if (kind != Kind::kString) return false;
      if (!take_text(reader, found.node_id, sizeof(found.node_id))) return false;
      has_node = true;
    } else if (key.is("mqtt_host")) {
      if (kind != Kind::kString) return false;
      if (!take_text(reader, found.mqtt_host, sizeof(found.mqtt_host))) return false;
      has_host = true;
    } else if (key.is("mqtt_port")) {
      if (kind != Kind::kNumber) return false;
      int64_t port = 0;
      if (!reader.integer(port)) return false;
      if (port < 1 || port > 65535) return false;
      found.mqtt_port = static_cast<uint16_t>(port);
      has_port = true;
    } else if (key.is("wifi_ssid")) {
      if (kind != Kind::kString) return false;
      if (!take_text(reader, found.wifi_ssid, sizeof(found.wifi_ssid))) return false;
    } else if (key.is("wifi_password")) {
      if (kind != Kind::kString) return false;
      if (!take_text(reader, found.wifi_password, sizeof(found.wifi_password))) return false;
    } else {
      // A field this firmware does not know is skipped rather than refused: the
      // provisioning tool may write more than a given release reads, and an identity that
      // is otherwise complete should not be lost to an addition made for a later one.
      if (!reader.skip()) return false;
    }
  }
  if (!has_node || !has_host || !has_port) return false;
  if (!is_name(found.node_id) || !is_host(found.mqtt_host)) return false;
  // A passphrase with nothing to join it to is a record somebody got wrong, and joining
  // the wrong network is worse than refusing to join one.
  if (found.wifi_password[0] != '\0' && !found.has_wifi()) return false;
  if (found.has_wifi() && !is_ssid(found.wifi_ssid)) return false;
  if (!is_passphrase(found.wifi_password)) return false;
  out = found;
  return true;
}

bool write_uuid4(const uint8_t entropy[16], char* out, size_t capacity) {
  if (entropy == nullptr || out == nullptr || capacity < kUuidText) return false;
  uint8_t bytes[16];
  std::memcpy(bytes, entropy, sizeof(bytes));
  bytes[6] = static_cast<uint8_t>((bytes[6] & 0x0F) | 0x40);  // version 4
  bytes[8] = static_cast<uint8_t>((bytes[8] & 0x3F) | 0x80);  // variant 1
  static const char kHex[] = "0123456789abcdef";
  size_t at = 0;
  for (size_t index = 0; index < 16; ++index) {
    if (index == 4 || index == 6 || index == 8 || index == 10) out[at++] = '-';
    out[at++] = kHex[(bytes[index] >> 4) & 0x0F];
    out[at++] = kHex[bytes[index] & 0x0F];
  }
  out[at] = '\0';
  return true;
}

}  // namespace sentry
