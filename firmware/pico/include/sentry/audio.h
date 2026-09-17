// How sound leaves this node, if it ever does: the block format, and the line that asks
// for permission to send one.
//
// The layout is the hub's, published in `contracts/satellite/v1/audio.json` and read back
// by `tools/audio_check.py` against the bytes this file produces. Nothing here serialises
// a C++ struct: the fields are written little-endian at defined offsets, because the
// padding a compiler chooses is not part of anybody's contract.
//
// There is no encoder and no compression. A block is a header and the samples as they were
// captured — signed 16-bit, one channel, 16 kHz — and this file never holds one: the
// caller owns the buffer, fills it, and is told how many bytes to send.
//
// Nothing here opens a socket, and nothing here decides whether sound may be sent. That is
// the lease's business, and it is asked before any of this is called.

#ifndef SENTRY_AUDIO_H
#define SENTRY_AUDIO_H

#include <cstddef>
#include <cstdint>

namespace sentry {

// The samples are copied out of memory as they lie, so this has to be true. It is on both
// of these chips and on every machine the host tests run on; said out loud rather than
// assumed, because the day it is not, the hub would hear noise and nobody would know why.
static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__,
              "the wire is little-endian and so, here, is the machine");

inline constexpr size_t kAudioHeaderBytes = 30;
inline constexpr uint8_t kAudioVersion = 1;
inline constexpr uint32_t kAudioRate = 16000;
// A hundred milliseconds: what the hub expects and what the agent sends.
inline constexpr size_t kAudioBlockSamples = 1600;
inline constexpr size_t kMaxAudioBlockSamples = 2 * kAudioBlockSamples;
// This node lost samples before this block, because its own queue was full. The hub fills
// the hole with silence rather than playing the next block early.
inline constexpr uint8_t kAudioGap = 0x01;

struct AudioBlock {
  uint32_t sequence = 0;       // from zero on every connection
  uint64_t first_sample = 0;   // which sample of the capture this block starts at
  uint64_t captured_ns = 0;    // this node's clock, for diagnosis and not for alignment
  bool gap = false;
  size_t samples = 0;
};

// The thirty bytes in front of the samples. False, and nothing written, if the block says
// something the format cannot carry — no samples, or more than the hub will read.
bool write_audio_header(const AudioBlock& block, uint8_t* out, size_t capacity);

// A whole block, header and samples, into one buffer. This is what the tests and the
// cross-check use; the device writes the header in front of samples that are already
// where they will be sent from, and never copies them twice.
bool write_audio_block(const AudioBlock& block, const int16_t* samples, uint8_t* out,
                       size_t capacity, size_t& written);

// The line the hub's media gateway wants before it will take any sound: one object and a
// newline. `out` is written with `written` bytes, newline included.
bool write_audio_hello(const char* stream_id, const char* source_id, const char* token,
                       char* out, size_t capacity, size_t& written);

// What the gateway answered. True only for a plain `ok`; anything else is a refusal, and
// `detail` is filled with whatever reason came with it — or with what was wrong with the
// answer itself, which is a refusal too.
bool read_gateway_answer(const char* line, size_t size, char* detail, size_t capacity);

}  // namespace sentry

#endif  // SENTRY_AUDIO_H
