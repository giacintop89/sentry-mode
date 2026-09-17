#include "sentry/store.h"

#include <cstring>

namespace sentry {
namespace {

void put_u32(char* out, uint32_t value) {
  out[0] = static_cast<char>(value & 0xFF);
  out[1] = static_cast<char>((value >> 8) & 0xFF);
  out[2] = static_cast<char>((value >> 16) & 0xFF);
  out[3] = static_cast<char>((value >> 24) & 0xFF);
}

uint32_t get_u32(const char* bytes) {
  return static_cast<uint32_t>(static_cast<unsigned char>(bytes[0])) |
         (static_cast<uint32_t>(static_cast<unsigned char>(bytes[1])) << 8) |
         (static_cast<uint32_t>(static_cast<unsigned char>(bytes[2])) << 16) |
         (static_cast<uint32_t>(static_cast<unsigned char>(bytes[3])) << 24);
}

// Bit by bit rather than from a table: this runs twice per configuration change, and a
// kilobyte of table in a 264 kB part is a poor trade for microseconds nobody waits on.
uint32_t crc32_over(uint32_t remainder, const char* data, size_t size) {
  for (size_t index = 0; index < size; ++index) {
    remainder ^= static_cast<unsigned char>(data[index]);
    for (int bit = 0; bit < 8; ++bit) {
      remainder = (remainder >> 1) ^ (0xEDB88320u & (~(remainder & 1u) + 1u));
    }
  }
  return remainder;
}

}  // namespace

uint32_t crc32(const char* data, size_t size) {
  return crc32_over(0xFFFFFFFFu, data, size) ^ 0xFFFFFFFFu;
}

size_t write_record(const char* payload, size_t size, uint32_t sequence, char* out,
                    size_t capacity) {
  if (out == nullptr || (payload == nullptr && size > 0)) return 0;
  if (size > kMaxRecordBytes) return 0;
  if (capacity < kRecordHeaderBytes + size) return 0;
  put_u32(out, kRecordMagic);
  out[4] = static_cast<char>(kRecordVersion & 0xFF);
  out[5] = static_cast<char>((kRecordVersion >> 8) & 0xFF);
  out[6] = 0;
  out[7] = 0;
  put_u32(out + 8, static_cast<uint32_t>(size));
  put_u32(out + 12, sequence);
  put_u32(out + 16, 0);
  if (size > 0) std::memcpy(out + kRecordHeaderBytes, payload, size);
  // The checksum covers the header as well as the payload, so a length that was written
  // and a payload that was not cannot agree with each other.
  put_u32(out + 16, crc32(out, kRecordHeaderBytes + size));
  return kRecordHeaderBytes + size;
}

bool read_record(const char* bytes, size_t size, Record& out) {
  if (bytes == nullptr || size < kRecordHeaderBytes) return false;
  if (get_u32(bytes) != kRecordMagic) return false;
  uint16_t version = static_cast<uint16_t>(
      static_cast<unsigned char>(bytes[4]) |
      (static_cast<uint32_t>(static_cast<unsigned char>(bytes[5])) << 8));
  if (version != kRecordVersion) return false;
  uint32_t length = get_u32(bytes + 8);
  if (length > kMaxRecordBytes) return false;
  if (kRecordHeaderBytes + length > size) return false;  // the write stopped short

  // The checksum was taken with its own field zeroed, which is the only way to include the
  // header without chasing our own tail. The payload is checksummed where it lies.
  char header[kRecordHeaderBytes];
  std::memcpy(header, bytes, kRecordHeaderBytes);
  put_u32(header + 16, 0);
  uint32_t running = crc32_over(0xFFFFFFFFu, header, kRecordHeaderBytes);
  running = crc32_over(running, bytes + kRecordHeaderBytes, length) ^ 0xFFFFFFFFu;
  if (running != get_u32(bytes + 16)) return false;

  out.version = version;
  out.sequence = get_u32(bytes + 12);
  out.payload = length > 0 ? bytes + kRecordHeaderBytes : nullptr;
  out.size = length;
  return true;
}

Slot choose(const char* first, size_t first_size, const char* second, size_t second_size,
            Record& out) {
  Record one;
  Record two;
  bool one_ok = read_record(first, first_size, one);
  bool two_ok = read_record(second, second_size, two);
  if (!one_ok && !two_ok) return Slot::kNeither;
  if (one_ok && !two_ok) {
    out = one;
    return Slot::kFirst;
  }
  if (!one_ok && two_ok) {
    out = two;
    return Slot::kSecond;
  }
  // Sequence numbers wrap, and a node reconfigured 2^32 times should still boot: the
  // comparison is on the difference between them, not on the values themselves.
  bool second_is_newer = static_cast<int32_t>(two.sequence - one.sequence) > 0;
  out = second_is_newer ? two : one;
  return second_is_newer ? Slot::kSecond : Slot::kFirst;
}

Slot next_write(Slot in_use) { return in_use == Slot::kFirst ? Slot::kSecond : Slot::kFirst; }

}  // namespace sentry
