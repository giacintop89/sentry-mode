// The bytes that would leave this node if the hub ever asked it for sound.
//
// The layout belongs to the hub, so most of what is checked here is checked again by
// `tools/audio_check.py` against the published contract and the hub's own decoder. What
// this file adds is the refusals: the blocks that are never written, the hello that is
// never sent, and the answers this node will not take for a yes.

#include <cstdint>
#include <cstring>
#include <vector>

#include "harness.h"
#include "sentry/audio.h"

using sentry::AudioBlock;
using sentry::kAudioBlockSamples;
using sentry::kAudioGap;
using sentry::kAudioHeaderBytes;
using sentry::kMaxAudioBlockSamples;
using sentry::read_gateway_answer;
using sentry::write_audio_block;
using sentry::write_audio_header;
using sentry::write_audio_hello;

namespace {

uint16_t at16(const uint8_t* bytes) {
  return static_cast<uint16_t>(bytes[0] | (bytes[1] << 8));
}

uint32_t at32(const uint8_t* bytes) {
  uint32_t value = 0;
  for (size_t byte = 0; byte < 4; ++byte) value |= static_cast<uint32_t>(bytes[byte]) << (8 * byte);
  return value;
}

uint64_t at64(const uint8_t* bytes) {
  uint64_t value = 0;
  for (size_t byte = 0; byte < 8; ++byte) value |= static_cast<uint64_t>(bytes[byte]) << (8 * byte);
  return value;
}

}  // namespace

TEST(a_header_is_thirty_bytes_at_offsets_nobody_chose_at_compile_time) {
  AudioBlock block;
  block.sequence = 0x01020304;
  block.first_sample = 0x1122334455667788ull;
  block.captured_ns = 0x99aabbccddeeff00ull;
  block.samples = kAudioBlockSamples;

  uint8_t out[kAudioHeaderBytes] = {};
  CHECK(write_audio_header(block, out, sizeof(out)));
  CHECK(std::memcmp(out, "SMA1", 4) == 0);
  CHECK(out[4] == 1);
  CHECK(out[5] == 0);
  CHECK(at16(out + 6) == 0);  // reserved, and zero because the contract says zero
  CHECK(at32(out + 8) == 0x01020304u);
  CHECK(at64(out + 12) == 0x1122334455667788ull);
  CHECK(at64(out + 20) == 0x99aabbccddeeff00ull);
  CHECK(at16(out + 28) == 1600);
}

TEST(a_block_that_lost_samples_in_front_of_it_says_so_in_one_bit) {
  AudioBlock block;
  block.samples = 8;
  block.gap = true;
  uint8_t out[kAudioHeaderBytes] = {};
  CHECK(write_audio_header(block, out, sizeof(out)));
  CHECK(out[5] == kAudioGap);
}

TEST(a_block_the_format_cannot_carry_is_never_written) {
  uint8_t out[kAudioHeaderBytes + 16] = {};
  AudioBlock empty;
  empty.samples = 0;
  CHECK(!write_audio_header(empty, out, sizeof(out)));

  AudioBlock huge;
  huge.samples = kMaxAudioBlockSamples + 1;
  CHECK(!write_audio_header(huge, out, sizeof(out)));

  // And a buffer that would hold a header but not the sound after it.
  AudioBlock ordinary;
  ordinary.samples = 8;
  const std::vector<int16_t> samples(8, 1);
  size_t written = 99;
  CHECK(!write_audio_block(ordinary, samples.data(), out, kAudioHeaderBytes + 8, written));
  CHECK(written == 0);
  CHECK(!write_audio_header(ordinary, out, kAudioHeaderBytes - 1));
}

TEST(the_samples_go_out_little_endian_whatever_the_machine_is) {
  // Values chosen so that a machine that wrote them the other way round would be caught by
  // the bytes rather than by the numbers: -2 is ff fe one way and fe ff the other.
  const std::vector<int16_t> samples = {0, 1, -1, -2, 32767, -32768};
  AudioBlock block;
  block.sequence = 7;
  block.samples = samples.size();

  uint8_t out[kAudioHeaderBytes + 12] = {};
  size_t written = 0;
  CHECK(write_audio_block(block, samples.data(), out, sizeof(out), written));
  CHECK(written == kAudioHeaderBytes + 12);
  const uint8_t* pcm = out + kAudioHeaderBytes;
  CHECK(pcm[0] == 0x00 && pcm[1] == 0x00);
  CHECK(pcm[2] == 0x01 && pcm[3] == 0x00);
  CHECK(pcm[4] == 0xff && pcm[5] == 0xff);
  CHECK(pcm[6] == 0xfe && pcm[7] == 0xff);
  CHECK(pcm[8] == 0xff && pcm[9] == 0x7f);
  CHECK(pcm[10] == 0x00 && pcm[11] == 0x80);
}

TEST(the_hello_says_what_it_is_carrying_and_ends_with_a_newline) {
  char out[256] = {};
  size_t written = 0;
  CHECK(write_audio_hello("3f7c1a9e5b204d86", "hall-noise",
                          "9f2b6c1d8a3e4f50", out, sizeof(out), written));
  CHECK(written == std::strlen(out));
  CHECK(out[written - 1] == '\n');
  CHECK(std::strcmp(out,
                    "{\"schema_version\":1,\"kind\":\"audio\","
                    "\"stream_id\":\"3f7c1a9e5b204d86\","
                    "\"source_id\":\"hall-noise\",\"token\":\"9f2b6c1d8a3e4f50\"}\n") == 0);
}

TEST(a_hello_this_node_could_not_have_been_granted_never_reaches_a_socket) {
  char out[256] = {};
  size_t written = 0;
  // A stream id that is not a uuid, a source that is not a name, and no token at all: the
  // gateway would refuse each of them, and none of them is worth a connection.
  // The gateway makes a stream id out of random bytes, so it is not a uuid; what it may
  // not be is short enough to guess or shaped like anything but a ticket.
  CHECK(!write_audio_hello("short", "hall-noise", "t", out, sizeof(out), written));
  CHECK(!write_audio_hello("3f7c1a9e 5b204d86", "hall-noise", "t", out, sizeof(out), written));
  CHECK(!write_audio_hello("3f7c1a9e5b204d86", "hall noise", "t", out,
                           sizeof(out), written));
  CHECK(!write_audio_hello("3f7c1a9e5b204d86", "hall-noise", "", out,
                           sizeof(out), written));
  CHECK(written == 0);
  // And a buffer too small for it is a refusal rather than half a line.
  CHECK(!write_audio_hello("3f7c1a9e5b204d86", "hall-noise", "token", out,
                           40, written));
}

TEST(only_a_plain_yes_is_taken_for_one) {
  char detail[64] = {};
  const char yes[] = "{\"ok\":true}";
  CHECK(read_gateway_answer(yes, std::strlen(yes), detail, sizeof(detail)));

  const char no[] = "{\"ok\":false,\"detail\":\"that stream is not open\"}";
  CHECK(!read_gateway_answer(no, std::strlen(no), detail, sizeof(detail)));
  CHECK(std::strcmp(detail, "that stream is not open") == 0);

  const char quiet[] = "{\"ok\":false}";
  CHECK(!read_gateway_answer(quiet, std::strlen(quiet), detail, sizeof(detail)));
  CHECK(std::strcmp(detail, "the hub refused, and did not say why") == 0);
}

TEST(an_answer_that_is_not_an_answer_is_not_a_yes_either) {
  char detail[64] = {};
  const char* nonsense[] = {"", "ok", "{}", "[true]", "{\"ok\":\"true\"}", "{\"ok\":1}",
                            "{\"ok\":true", "null"};
  for (const char* line : nonsense) {
    CHECK(!read_gateway_answer(line, std::strlen(line), detail, sizeof(detail)));
  }
  CHECK(std::strcmp(detail, "the hub's answer is not an answer") == 0);
}

int main() { return harness::run_all("audio"); }
