// Where this node keeps what it must still have after the power goes off.
//
// Five things outlive a boot: who this node is, the authority it trusts, its own
// certificate, its private key, and the configuration the hub last told it to run. Each of
// them gets a pair of slots, and each slot holds one framed record — the framing is
// `store.h`, which is what makes an interrupted write recognisable as one.
//
// Everything is a whole erase sector, because a sector is what a flash part can be told to
// forget. Two sectors per item, never one: the slot in use stays readable for the whole
// time the other is being erased and programmed, so a node that loses power in the middle
// of being reconfigured comes back running what it ran before rather than nothing.
//
// There is no flash in this file either. These are the offsets and the sizes, which is
// what can be checked without a board; `src/device/flash_vault.cpp` is the part that
// erases and programs, and it asks here where to do it.
//
// The layout has a version, and it is the record version in `store.h`: every slot carries
// it, and a record written by a firmware that laid the vault out differently fails to read
// rather than being taken for one of these. That is deliberately blunt — a board whose
// layout changed loses what it kept and is provisioned again, which is a cable and a
// minute, and the alternative is a firmware that guesses at the meaning of old bytes.

#ifndef SENTRY_VAULT_H
#define SENTRY_VAULT_H

#include <cstddef>

#include "sentry/store.h"

namespace sentry {

// What a node keeps. The order is the order they sit in flash, and it is also the order
// they are needed in at boot: a name, then the three things one connection is made of,
// then what to run once there is one.
enum class Held { kIdentity, kAuthority, kCertificate, kPrivateKey, kConfiguration };
inline constexpr size_t kHeldKinds = 5;

// One erase sector on both of these parts. Nothing kept here comes close to filling one;
// the size is the flash's unit of forgetting, not a measure of what goes in it.
inline constexpr size_t kVaultSlotBytes = 4096;
inline constexpr size_t kVaultSlots = 2;
inline constexpr size_t kVaultBytes = kHeldKinds * kVaultSlots * kVaultSlotBytes;

// Where a slot starts, counted from the beginning of the vault. `Slot::kNeither` is not a
// place, and answers `kVaultBytes`: an offset no slot has, which a caller that used it
// without looking would read past the end of.
size_t offset_of(Held held, Slot slot);

// The most a record of this kind may carry, payload only. It is the smaller of what fits
// in a slot after the framing and what the thing itself is allowed to be — a certificate
// longer than `kMaxCertificateText` is refused here rather than at the handshake.
size_t most_of(Held held);

const char* name_of(Held held);

}  // namespace sentry

#endif  // SENTRY_VAULT_H
