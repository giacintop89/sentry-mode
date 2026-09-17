// Two slots, one of which is always readable — including in the middle of writing the other.

#include <cstring>
#include <string>

#include "harness.h"
#include "sentry/store.h"

using sentry::choose;
using sentry::kRecordHeaderBytes;
using sentry::next_write;
using sentry::read_record;
using sentry::Record;
using sentry::Slot;
using sentry::write_record;

namespace {

const char* kConfiguration = R"({"sources":[{"id":"pir-1","driver":"gpio_motion"}]})";

// What a record is, as the vault numbers the five things a node keeps. Everything here is
// a configuration unless it says otherwise.
constexpr uint8_t kKindConfiguration = 4;
constexpr uint8_t kKindCertificate = 2;

std::string framed(const char* payload, uint32_t sequence, uint8_t kind = kKindConfiguration) {
  char buffer[512];
  size_t size =
      write_record(payload, std::strlen(payload), sequence, kind, buffer, sizeof(buffer));
  return std::string(buffer, size);
}

}  // namespace

TEST(what_was_written_is_what_is_read_back) {
  std::string slot = framed(kConfiguration, 9);
  CHECK(slot.size() == kRecordHeaderBytes + std::strlen(kConfiguration));
  Record record;
  CHECK(read_record(slot.data(), slot.size(), kKindConfiguration, record));
  CHECK(record.sequence == 9);
  CHECK(record.size == std::strlen(kConfiguration));
  CHECK(std::memcmp(record.payload, kConfiguration, record.size) == 0);
}

TEST(a_write_that_stopped_halfway_is_not_a_configuration) {
  std::string whole = framed(kConfiguration, 9);
  for (size_t kept = 0; kept < whole.size(); ++kept) {
    Record record;
    if (read_record(whole.data(), kept, kKindConfiguration, record)) {
      std::printf("    %zu of %zu bytes was accepted\n", kept, whole.size());
      CHECK(false);
    }
  }
}

TEST(a_byte_that_changed_after_the_write_is_not_a_configuration) {
  std::string whole = framed(kConfiguration, 9);
  for (size_t index = 0; index < whole.size(); ++index) {
    std::string damaged = whole;
    damaged[index] = static_cast<char>(damaged[index] ^ 0x01);
    Record record;
    if (read_record(damaged.data(), damaged.size(), kKindConfiguration, record)) {
      std::printf("    a flipped bit at %zu was accepted\n", index);
      CHECK(false);
    }
  }
}

TEST(an_erased_slot_is_not_mistaken_for_an_empty_configuration) {
  // Erased flash is all ones, and a slot that was never written is all zeroes in a test.
  std::string erased(64, static_cast<char>(0xFF));
  std::string blank(64, '\0');
  Record record;
  CHECK(!read_record(erased.data(), erased.size(), kKindConfiguration, record));
  CHECK(!read_record(blank.data(), blank.size(), kKindConfiguration, record));
}

TEST(the_newer_of_the_two_slots_is_the_one_that_boots) {
  std::string older = framed(kConfiguration, 4);
  std::string newer = framed(R"({"sources":[]})", 5);
  Record record;
  CHECK(choose(older.data(), older.size(), newer.data(), newer.size(), kKindConfiguration, record) == Slot::kSecond);
  CHECK(record.sequence == 5);
  CHECK(choose(newer.data(), newer.size(), older.data(), older.size(), kKindConfiguration, record) == Slot::kFirst);
  CHECK(record.sequence == 5);
}

TEST(an_interrupted_write_leaves_the_previous_configuration_in_charge) {
  std::string good = framed(kConfiguration, 4);
  std::string torn = framed(R"({"sources":[]})", 5);
  torn.resize(torn.size() - 3);  // the power went out three bytes from the end
  Record record;
  CHECK(choose(good.data(), good.size(), torn.data(), torn.size(), kKindConfiguration, record) == Slot::kFirst);
  CHECK(record.sequence == 4);
  CHECK(std::memcmp(record.payload, kConfiguration, record.size) == 0);
}

TEST(a_node_with_nothing_valid_in_either_slot_says_so) {
  std::string erased(64, static_cast<char>(0xFF));
  Record record;
  CHECK(choose(erased.data(), erased.size(), erased.data(), erased.size(), kKindConfiguration, record) ==
        Slot::kNeither);
}

TEST(a_sequence_number_that_wrapped_round_is_still_newer) {
  // 2^32 configuration changes is not a plausible number, and a node that refused to boot
  // if it ever happened would be refusing for a reason nobody could work out.
  std::string old_high = framed(kConfiguration, 0xFFFFFFFEu);
  std::string wrapped = framed(R"({"sources":[]})", 1);
  Record record;
  CHECK(choose(old_high.data(), old_high.size(), wrapped.data(), wrapped.size(), kKindConfiguration, record) ==
        Slot::kSecond);
  CHECK(record.sequence == 1);
}

TEST(the_next_write_goes_to_the_slot_that_is_not_in_use) {
  CHECK(next_write(Slot::kFirst) == Slot::kSecond);
  CHECK(next_write(Slot::kSecond) == Slot::kFirst);
  CHECK(next_write(Slot::kNeither) == Slot::kFirst);  // nothing to preserve, start at one
}

TEST(a_configuration_larger_than_the_node_can_hold_is_refused_before_it_is_written) {
  std::string huge(sentry::kMaxRecordBytes + 1, 'x');
  char buffer[sentry::kMaxRecordBytes + 64];
  CHECK(write_record(huge.data(), huge.size(), 1, kKindConfiguration, buffer, sizeof(buffer)) ==
        0);
  char small[8];
  CHECK(write_record(kConfiguration, std::strlen(kConfiguration), 1, kKindConfiguration, small,
                     sizeof(small)) == 0);
}

TEST(a_slot_that_holds_something_else_is_not_an_answer_about_this) {
  // The vault moved by three sectors once, and the sector that had held one node's
  // authority became the one its certificate is kept in. Both are PEM, both check out as
  // records, and the second would have been believed. What each record is now says so.
  std::string certificate = framed(kConfiguration, 9, kKindCertificate);
  Record record;
  CHECK(!read_record(certificate.data(), certificate.size(), kKindConfiguration, record));
  CHECK(read_record(certificate.data(), certificate.size(), kKindCertificate, record));
  CHECK(record.kind == kKindCertificate);

  // And a slot pair where only the older one is the right kind answers with the older one
  // rather than with the newest thing that happens to be framed.
  std::string older = framed(kConfiguration, 8);
  std::string newer = framed(kConfiguration, 9, kKindCertificate);
  CHECK(choose(older.data(), older.size(), newer.data(), newer.size(), kKindConfiguration,
               record) == Slot::kFirst);
  CHECK(record.sequence == 8);
}

int main() { return harness::run_all("store"); }
