// Print the blocks this firmware would put on a media connection, as hex, one per line.
//
// `tools/audio_check.py` reads them with the hub's own decoder and against the published
// contract, rather than against anything written beside this code. A format checked only
// by the tests next to it agrees with itself.

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include "sentry/audio.h"

namespace {

void emit(const char* what, const uint8_t* bytes, size_t size) {
  if (size == 0) {
    std::fprintf(stderr, "refused to write the %s block\n", what);
    std::exit(1);
  }
  std::printf("%s\t", what);
  for (size_t index = 0; index < size; ++index) std::printf("%02x", bytes[index]);
  std::putchar('\n');
}

// A tone, so that a decoder which took the bytes in the wrong order would produce noise
// rather than something that still looks like sound.
std::vector<int16_t> a_tone(size_t samples) {
  std::vector<int16_t> block(samples);
  for (size_t index = 0; index < samples; ++index) {
    const double turn = 2.0 * 3.14159265358979323846 * static_cast<double>(index) / 40.0;
    block[index] = static_cast<int16_t>(0.5 * 32767.0 * std::sin(turn));
  }
  return block;
}

}  // namespace

int main() {
  static uint8_t out[sentry::kAudioHeaderBytes + 2 * sentry::kMaxAudioBlockSamples];
  size_t written = 0;

  const std::vector<int16_t> tone = a_tone(sentry::kAudioBlockSamples);
  sentry::AudioBlock first;
  first.sequence = 0;
  first.first_sample = 0;
  first.captured_ns = 1234567890123456789ull;
  first.samples = tone.size();
  if (!sentry::write_audio_block(first, tone.data(), out, sizeof(out), written)) written = 0;
  emit("first", out, written);

  // The one after it, a hundred milliseconds of capture later.
  sentry::AudioBlock second = first;
  second.sequence = 1;
  second.first_sample = sentry::kAudioBlockSamples;
  second.captured_ns += 100000000ull;
  if (!sentry::write_audio_block(second, tone.data(), out, sizeof(out), written)) written = 0;
  emit("second", out, written);

  // One that admits it lost what should have come before it.
  sentry::AudioBlock after_a_gap = first;
  after_a_gap.sequence = 2;
  after_a_gap.first_sample = 10 * sentry::kAudioBlockSamples;
  after_a_gap.captured_ns += 1000000000ull;
  after_a_gap.gap = true;
  if (!sentry::write_audio_block(after_a_gap, tone.data(), out, sizeof(out), written)) written = 0;
  emit("after-a-gap", out, written);

  // The shortest and the longest the format allows, which are the two a decoder written
  // from the field alone would get wrong.
  const std::vector<int16_t> one = {-32768};
  sentry::AudioBlock shortest = first;
  shortest.sequence = 3;
  shortest.samples = 1;
  if (!sentry::write_audio_block(shortest, one.data(), out, sizeof(out), written)) written = 0;
  emit("shortest", out, written);

  const std::vector<int16_t> full = a_tone(sentry::kMaxAudioBlockSamples);
  sentry::AudioBlock longest = first;
  longest.sequence = 4;
  longest.samples = full.size();
  if (!sentry::write_audio_block(longest, full.data(), out, sizeof(out), written)) written = 0;
  emit("longest", out, written);

  // And the hello that has to be accepted before any of the above may be sent.
  char hello[256] = {};
  size_t hello_size = 0;
  if (!sentry::write_audio_hello("3f7c1a9e5b204d86", "hall-noise",
                                 "9f2b6c1d8a3e4f50", hello, sizeof(hello), hello_size)) {
    std::fprintf(stderr, "refused to write the hello\n");
    return 1;
  }
  emit("hello", reinterpret_cast<const uint8_t*>(hello), hello_size);
  return 0;
}
