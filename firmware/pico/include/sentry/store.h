// Two configuration slots, and the framing that lets an interrupted write be recognised.
//
// A node loses power in the middle of writing its configuration. What it must not do is
// come back with half a record and treat it as a record: the flash held something, the
// length field said how much, and nothing said whether the write ever finished. So every
// record carries its own length and a checksum over its own bytes, and the two slots are
// compared rather than trusted — the newest one that checks out wins, and the other is
// what an interrupted write leaves behind to fall back on.
//
// There is no flash in this file. These are functions over buffers the caller owns, which
// is what lets the recovery behaviour be tested without a board.

#ifndef SENTRY_STORE_H
#define SENTRY_STORE_H

#include <cstddef>
#include <cstdint>

namespace sentry {

// "SENT", little endian, so a slot that was never written reads as neither 0 nor this.
inline constexpr uint32_t kRecordMagic = 0x544E4553;
inline constexpr uint16_t kRecordVersion = 2;
// magic, version, what this record is, reserved, length, sequence, checksum.
inline constexpr size_t kRecordHeaderBytes = 20;
// A configuration this node could not act on anyway; a length past it is refused before
// anything reads that far.
inline constexpr size_t kMaxRecordBytes = 4096;

struct Record {
  uint16_t version = kRecordVersion;
  // What the thing in this slot is, as the layer above numbers them — `Held` in `vault.h`.
  // It is in the record because a slot is a place in flash and a place can be given a new
  // meaning: move the vault by three sectors and the sector that held one node's authority
  // is where its certificate now goes. A PEM read out of the wrong slot is still a PEM,
  // and would be believed. This is one byte that says it is not.
  uint8_t kind = 0;
  uint32_t sequence = 0;
  const char* payload = nullptr;  // into the caller's buffer; never NUL-terminated
  size_t size = 0;
};

// Frame one record of `kind`. Returns the bytes written, or 0 if it does not fit or is
// too large.
size_t write_record(const char* payload, size_t size, uint32_t sequence, uint8_t kind,
                    char* out, size_t capacity);

// Read one framed record. False when the magic, the version, the length or the checksum
// disagree — which is every way an interrupted write can look from here — and false when
// it is a record of something other than `kind`, which is every way a slot that has been
// given a new meaning can look.
bool read_record(const char* bytes, size_t size, uint8_t kind, Record& out);

enum class Slot { kNeither, kFirst, kSecond };

// Which of the two slots to boot from, and the record in it. A slot that does not check
// out is not compared: a torn write cannot win by claiming a high sequence number.
Slot choose(const char* first, size_t first_size, const char* second, size_t second_size,
            uint8_t kind, Record& out);

// Which slot the next write goes to: the one that is not in use, so the one in use stays
// readable the whole time the other is being erased and programmed.
Slot next_write(Slot in_use);

uint32_t crc32(const char* data, size_t size);

}  // namespace sentry

#endif  // SENTRY_STORE_H
