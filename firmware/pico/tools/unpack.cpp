// Read the two configuration slots the way the firmware would at boot, and say what it
// found: which slot won, and the identity in it.
//
// tools/pack_provisioning.py writes those slots with its own implementation of the framing
// and then reads this program's answer back. Two implementations of a header agreeing is
// the only way to find out that one of them has the length field in the wrong place before
// a board does.

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "sentry/identity.h"
#include "sentry/store.h"

namespace {

size_t slurp(const char* path, char* out, size_t capacity) {
  std::FILE* file = std::fopen(path, "rb");
  if (file == nullptr) return 0;
  size_t read = std::fread(out, 1, capacity, file);
  std::fclose(file);
  return read;
}

const char* name_of(sentry::Slot slot) {
  switch (slot) {
    case sentry::Slot::kFirst:
      return "first";
    case sentry::Slot::kSecond:
      return "second";
    case sentry::Slot::kNeither:
    default:
      return "neither";
  }
}

}  // namespace

int main(int argc, char** argv) {
  if (argc != 3) {
    std::fprintf(stderr, "usage: sentry_unpack <slot-a> <slot-b>\n");
    return 2;
  }
  static char first[sentry::kMaxRecordBytes + sentry::kRecordHeaderBytes];
  static char second[sizeof(first)];
  size_t first_size = slurp(argv[1], first, sizeof(first));
  size_t second_size = slurp(argv[2], second, sizeof(second));

  sentry::Record record;
  sentry::Slot chosen = sentry::choose(first, first_size, second, second_size, record);
  if (chosen == sentry::Slot::kNeither) {
    std::printf("{\"slot\":\"neither\"}\n");
    return 0;
  }
  sentry::Provisioning identity;
  if (!sentry::read_provisioning(record.payload, record.size, identity)) {
    std::printf("{\"slot\":\"%s\",\"sequence\":%u,\"identity\":null}\n", name_of(chosen),
                static_cast<unsigned>(record.sequence));
    return 0;
  }
  std::printf(
      "{\"slot\":\"%s\",\"sequence\":%u,\"node_id\":\"%s\",\"mqtt_host\":\"%s\","
      "\"mqtt_port\":%u}\n",
      name_of(chosen), static_cast<unsigned>(record.sequence), identity.node_id,
      identity.mqtt_host, static_cast<unsigned>(identity.mqtt_port));
  return 0;
}
