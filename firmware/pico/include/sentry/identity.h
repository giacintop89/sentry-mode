// Who this node is between boots, and who it is only for this boot.
//
// The identity is provisioned once and read back on every boot: the node id the hub knows,
// and where the broker is. The boot id is the opposite — it must differ every time, because
// it is what tells the hub that a sequence number starting again at zero is a new boot and
// not a replay.

#ifndef SENTRY_IDENTITY_H
#define SENTRY_IDENTITY_H

#include <cstddef>
#include <cstdint>

namespace sentry {

// A node id is a name: the same 40-character limit the hub and the agent enforce.
inline constexpr size_t kMaxNodeIdText = 41;
// Long enough for a host name on a home network, short enough to sit in a struct.
inline constexpr size_t kMaxHostText = 64;
// An SSID is at most 32 bytes, and a WPA2 passphrase at most 63 characters or 64 hex
// digits. Both are as the standards define them, not as a particular router prints them.
inline constexpr size_t kMaxSsidText = 33;
inline constexpr size_t kMaxPassphraseText = 65;
// 36 characters and the NUL after them.
inline constexpr size_t kUuidText = 37;

struct Provisioning {
  char node_id[kMaxNodeIdText] = {};
  char mqtt_host[kMaxHostText] = {};
  uint16_t mqtt_port = 0;
  // The network this board joins. Optional, because a node reached over the USB bridge has
  // no radio to configure — and because a record that refused to parse without a
  // passphrase would lose a node's name over a network it does not need.
  //
  // Whatever holds this holds a secret in the clear: a passphrase written to flash can be
  // read back out of a board somebody walks away with. That is a property of the board,
  // not something this file can fix, and it is the reason the satellite gets the guest
  // network and its own certificate rather than anything else on it.
  char wifi_ssid[kMaxSsidText] = {};
  char wifi_password[kMaxPassphraseText] = {};

  // Whether there is a network to join at all. An open network is one with no passphrase,
  // which is different from not having been told about a network.
  bool has_wifi() const { return wifi_ssid[0] != '\0'; }
};

// Read the provisioning record written into flash by the provisioning tool. Every field is
// required and every field is checked: a node that boots with half an identity would
// announce itself as something the hub does not know and be refused at the ACL anyway.
bool read_provisioning(const char* document, size_t size, Provisioning& out);

// Format 16 bytes of entropy as a version 4 UUID, for the boot id and the connection id.
// The caller provides the entropy; this file has no opinion about where randomness on an
// RP2040 comes from, which is a question with a real answer and no room for a guess here.
bool write_uuid4(const uint8_t entropy[16], char* out, size_t capacity);

}  // namespace sentry

#endif  // SENTRY_IDENTITY_H
