// The two halves of the cable, driven from outside the firmware.
//
// With no argument it prints the frames this firmware would put on the wire, as hex, one
// per line. With `--read` it does the opposite: hex frames on standard input, and what the
// firmware's reader made of each one on standard output. `tools/link_check.py` runs it
// both ways against the bridge's own codec, so the two ends are checked against each other
// rather than each against itself.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "sentry/link.h"

namespace {

void emit(const char* what, const uint8_t* bytes, size_t size) {
  if (size == 0) {
    std::fprintf(stderr, "refused to write the %s frame\n", what);
    std::exit(1);
  }
  std::printf("%s\t", what);
  for (size_t index = 0; index < size; ++index) std::printf("%02x", bytes[index]);
  std::putchar('\n');
}

void write_one(const char* what, sentry::Carries carries, uint32_t counter,
               const std::string& payload) {
  static uint8_t out[sentry::kMaxLinkFrame];
  size_t written = 0;
  if (!sentry::write_frame(carries, counter, reinterpret_cast<const uint8_t*>(payload.data()),
                           payload.size(), out, sizeof(out), written)) {
    written = 0;
  }
  emit(what, out, written);
}

int written_frames() {
  write_one("state", sentry::Carries::kState, 0,
            "{\"schema_version\":1,\"node_id\":\"pico-cablato\",\"online\":true}");
  write_one("event", sentry::Carries::kEvents, 1,
            "{\"schema_version\":1,\"event\":{\"kind\":\"sensor.motion\"}}");
  write_one("health", sentry::Carries::kHealth, 2, "{\"schema_version\":1,\"uptime\":61}");
  write_one("ack", sentry::Carries::kAcks, 3, "{\"schema_version\":1,\"outcome\":\"applied\"}");
  // A line of this board's console, which goes down the same cable as everything else and
  // is the reason no unframed text ever does.
  write_one("said", sentry::Carries::kSaid, 4, "# ready pico2 node=pico-cablato\n");
  // Nothing in it at all, which is a frame and not an absence of one.
  write_one("empty", sentry::Carries::kEvents, 5, "");
  // The largest one the format carries, so that a reader with a smaller idea of the limit
  // fails here rather than on a cable.
  write_one("longest", sentry::Carries::kEvents, 6, std::string(sentry::kMaxLinkPayload, 'x'));
  // And the counter at the top of its range, which is where a field that is quietly
  // signed, or quietly sixteen bits, stops agreeing with the other end. It is read on its
  // own rather than after the others, because as a run it would be a gap of four billion.
  write_one("top-counter", sentry::Carries::kEvents, 0xFFFFFFFFu, "{\"wrapped\":false}");
  return 0;
}

int read_them() {
  sentry::LinkReader reader(false);  // the board's side: what a bridge sends it
  std::string line;
  int status = 0;
  for (int letter = std::getchar(); ; letter = std::getchar()) {
    if (letter != EOF && letter != '\n') {
      line.push_back(static_cast<char>(letter));
      continue;
    }
    if (!line.empty()) {
      const size_t tab = line.find('\t');
      const std::string what = tab == std::string::npos ? "?" : line.substr(0, tab);
      const std::string hexed = tab == std::string::npos ? line : line.substr(tab + 1);
      if (hexed.size() % 2 != 0) {
        std::fprintf(stderr, "%s: half a byte\n", what.c_str());
        return 1;
      }
      sentry::Frame frame;
      bool got = false;
      for (size_t index = 0; index + 1 < hexed.size(); index += 2) {
        const uint8_t byte =
            static_cast<uint8_t>(std::strtoul(hexed.substr(index, 2).c_str(), nullptr, 16));
        if (reader.eat(byte, frame)) {
          std::printf("%s\t%s\t%u\t", what.c_str(), sentry::name_of(frame.what), frame.counter);
          std::fwrite(frame.payload, 1, frame.size, stdout);
          std::putchar('\n');
          got = true;
        }
      }
      if (!got) {
        std::printf("%s\trefused\t0\t\n", what.c_str());
        status = 0;  // the checker decides whether a refusal was the right answer
      }
      line.clear();
    }
    if (letter == EOF) break;
  }
  std::printf("#\tdiscarded=%u\trefused=%u\tmissed=%u\n", reader.discarded(), reader.refused(),
              reader.missed());
  return status;
}

}  // namespace

int main(int count, char** arguments) {
  if (count > 1 && std::strcmp(arguments[1], "--read") == 0) return read_them();
  return written_frames();
}
