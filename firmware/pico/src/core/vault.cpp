#include "sentry/vault.h"

#include "sentry/credentials.h"
#include "sentry/identity.h"
#include "sentry/mqtt.h"

namespace sentry {
namespace {

size_t index_of(Held held) { return static_cast<size_t>(held); }

// What a provisioning record is: four fields and the JSON around them, with the longest
// value each of them may hold. Half a kilobyte is twice that.
constexpr size_t kMostIdentityBytes = 512;

// What fits in a slot once the framing has taken its share.
constexpr size_t kMostInASlot = kVaultSlotBytes - kRecordHeaderBytes;

size_t smaller_of(size_t one, size_t two) { return one < two ? one : two; }

}  // namespace

size_t offset_of(Held held, Slot slot) {
  if (slot == Slot::kNeither) return kVaultBytes;
  const size_t which = slot == Slot::kFirst ? 0 : 1;
  return (index_of(held) * kVaultSlots + which) * kVaultSlotBytes;
}

size_t most_of(Held held) {
  switch (held) {
    case Held::kIdentity:
      return smaller_of(kMostIdentityBytes, kMostInASlot);
    case Held::kAuthority:
      return smaller_of(kMaxAuthorityText, kMostInASlot);
    case Held::kCertificate:
      return smaller_of(kMaxCertificateText, kMostInASlot);
    case Held::kPrivateKey:
      return smaller_of(kMaxPrivateKeyText, kMostInASlot);
    case Held::kConfiguration:
      // The command as it arrived, which is at most one packet's worth of it.
      return smaller_of(mqtt::kMaxPacketBytes, kMostInASlot);
  }
  return 0;
}

const char* name_of(Held held) {
  switch (held) {
    case Held::kIdentity:
      return "identity";
    case Held::kAuthority:
      return "authority";
    case Held::kCertificate:
      return "certificate";
    case Held::kPrivateKey:
      return "key";
    case Held::kConfiguration:
      return "configuration";
  }
  return "";
}

}  // namespace sentry
