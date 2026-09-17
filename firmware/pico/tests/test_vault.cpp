// The map of what this node keeps: five things, two slots each, and no two in one place.

#include <cstring>
#include <set>
#include <string>

#include "harness.h"
#include "sentry/credentials.h"
#include "sentry/store.h"
#include "sentry/vault.h"

using sentry::Held;
using sentry::kHeldKinds;
using sentry::kVaultBytes;
using sentry::kVaultSlotBytes;
using sentry::most_of;
using sentry::name_of;
using sentry::offset_of;
using sentry::Slot;

namespace {

Held every[kHeldKinds] = {Held::kIdentity, Held::kAuthority, Held::kCertificate,
                          Held::kPrivateKey, Held::kConfiguration};

}  // namespace

TEST(no_two_slots_share_a_sector) {
  std::set<size_t> seen;
  for (Held held : every) {
    for (Slot slot : {Slot::kFirst, Slot::kSecond}) {
      const size_t offset = offset_of(held, slot);
      CHECK(offset % kVaultSlotBytes == 0);
      CHECK(offset + kVaultSlotBytes <= kVaultBytes);
      CHECK(seen.insert(offset).second);
    }
  }
  CHECK(seen.size() == kHeldKinds * 2);
}

TEST(the_two_slots_of_one_thing_are_different_sectors) {
  // The whole point of the pair: erasing one cannot take the other with it, because a
  // flash part forgets a sector at a time and these are never the same sector.
  for (Held held : every) {
    const size_t first = offset_of(held, Slot::kFirst);
    const size_t second = offset_of(held, Slot::kSecond);
    CHECK(first != second);
    CHECK(first / kVaultSlotBytes != second / kVaultSlotBytes);
  }
}

TEST(a_slot_that_is_not_a_place_answers_with_one_that_is_not_either) {
  // `Slot::kNeither` is what an empty pair reads as. It has no offset, and what it gives
  // back is past the end of the vault rather than the beginning of it.
  for (Held held : every) {
    CHECK(offset_of(held, Slot::kNeither) == kVaultBytes);
  }
}

TEST(everything_kept_fits_in_the_slot_it_is_kept_in) {
  for (Held held : every) {
    CHECK(most_of(held) > 0);
    CHECK(most_of(held) + sentry::kRecordHeaderBytes <= kVaultSlotBytes);
    // And within what a record may be at all, or the framing would refuse the write on a
    // board with nobody watching.
    CHECK(most_of(held) <= sentry::kMaxRecordBytes);
  }
}

TEST(a_certificate_is_allowed_to_be_as_long_as_a_certificate_may_be) {
  // The vault does not get to be the shorter limit. Whatever `credentials.h` will accept
  // has somewhere to live, or a board would take a certificate it could not keep.
  CHECK(most_of(Held::kAuthority) >= sentry::kMaxAuthorityText);
  CHECK(most_of(Held::kCertificate) >= sentry::kMaxCertificateText);
  CHECK(most_of(Held::kPrivateKey) >= sentry::kMaxPrivateKeyText);
}

TEST(every_kind_is_named_and_no_two_share_a_name) {
  std::set<std::string> names;
  for (Held held : every) {
    const char* name = name_of(held);
    CHECK(name[0] != '\0');
    CHECK(names.insert(name).second);
  }
}

TEST(the_vault_is_as_big_as_the_slots_in_it_and_no_bigger) {
  CHECK(kVaultBytes == kHeldKinds * 2 * kVaultSlotBytes);
  // A round number of sectors, because the region is taken out of the end of the flash
  // and a part of a sector at the end of it would be a sector somebody else erases.
  CHECK(kVaultBytes % kVaultSlotBytes == 0);
}

int main() { return harness::run_all("vault"); }
