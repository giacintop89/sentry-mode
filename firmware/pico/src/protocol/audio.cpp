#include "sentry/audio.h"

#include <cstring>

#include "sentry/json.h"
#include "sentry/names.h"

namespace sentry {
namespace {

// Little-endian at a defined offset, which is the whole of the format's byte order. A
// `memcpy` of an integer would be the compiler's byte order rather than the contract's.
void put16(uint8_t* out, uint16_t value) {
  out[0] = static_cast<uint8_t>(value & 0xff);
  out[1] = static_cast<uint8_t>((value >> 8) & 0xff);
}

void put32(uint8_t* out, uint32_t value) {
  for (size_t byte = 0; byte < 4; ++byte) {
    out[byte] = static_cast<uint8_t>((value >> (8 * byte)) & 0xff);
  }
}

void put64(uint8_t* out, uint64_t value) {
  for (size_t byte = 0; byte < 8; ++byte) {
    out[byte] = static_cast<uint8_t>((value >> (8 * byte)) & 0xff);
  }
}

}  // namespace

bool write_audio_header(const AudioBlock& block, uint8_t* out, size_t capacity) {
  if (out == nullptr || capacity < kAudioHeaderBytes) return false;
  if (block.samples == 0 || block.samples > kMaxAudioBlockSamples) return false;

  std::memcpy(out, "SMA1", 4);
  out[4] = kAudioVersion;
  out[5] = block.gap ? kAudioGap : 0;
  put16(out + 6, 0);  // reserved, and zero is part of the contract rather than a filler
  put32(out + 8, block.sequence);
  put64(out + 12, block.first_sample);
  put64(out + 20, block.captured_ns);
  put16(out + 28, static_cast<uint16_t>(block.samples));
  return true;
}

bool write_audio_block(const AudioBlock& block, const int16_t* samples, uint8_t* out,
                       size_t capacity, size_t& written) {
  written = 0;
  if (samples == nullptr) return false;
  const size_t needed = kAudioHeaderBytes + block.samples * 2;
  if (capacity < needed) return false;
  if (!write_audio_header(block, out, capacity)) return false;
  for (size_t index = 0; index < block.samples; ++index) {
    put16(out + kAudioHeaderBytes + index * 2, static_cast<uint16_t>(samples[index]));
  }
  written = needed;
  return true;
}

bool write_audio_hello(const char* stream_id, const char* source_id, const char* token,
                       char* out, size_t capacity, size_t& written) {
  written = 0;
  if (out == nullptr || capacity == 0) return false;
  // The three things the gateway checks. A token this node made up, a stream it was never
  // granted or a source it is not running are all refused at the other end; they are
  // checked here as text so that a malformed one never reaches a socket at all.
  if (stream_id == nullptr || !is_stream_id(stream_id, std::strlen(stream_id))) return false;
  if (!is_name(source_id)) return false;
  if (token == nullptr || token[0] == '\0' || !is_clean_text(token, std::strlen(token))) {
    return false;
  }

  Writer writer(out, capacity - 1);  // room kept for the newline the gateway reads to
  writer.object_open();
  writer.key("schema_version");
  writer.integer(1);
  writer.key("kind");
  writer.string("audio");
  writer.key("stream_id");
  writer.string(stream_id);
  writer.key("source_id");
  writer.string(source_id);
  writer.key("token");
  writer.string(token);
  writer.object_close();
  if (!writer.ok()) return false;
  out[writer.size()] = '\n';
  written = writer.size() + 1;
  return true;
}

bool read_gateway_answer(const char* line, size_t size, char* detail, size_t capacity) {
  const char* why_not = "the hub's answer is not an answer";
  bool said_ok = false;
  bool ok = false;

  Reader reader(line, size);
  if (reader.begin_object()) {
    Span key;
    Kind kind = Kind::kEnd;
    bool sound = true;
    // Read to the end of the object rather than to the end of what is useful: an answer
    // that stops halfway is not an answer, however promising its first member was.
    bool whole = false;
    while (sound) {
      if (!reader.member(key, kind)) {
        sound = false;
        break;
      }
      if (kind == Kind::kEnd) {
        whole = true;
        break;
      }
      if (key.is("ok") && (kind == Kind::kTrue || kind == Kind::kFalse)) {
        sound = reader.boolean(ok);
        said_ok = sound;
      } else if (key.is("detail") && kind == Kind::kString) {
        size_t length = 0;
        char text[kMaxString + 1] = {};
        sound = reader.string(text, sizeof(text), length);
        if (sound && length > 0 && detail != nullptr && capacity > 0) {
          std::strncpy(detail, text, capacity - 1);
          detail[capacity - 1] = '\0';
          why_not = nullptr;  // the hub said why, and its words are better than these
        }
      } else {
        sound = reader.skip();
      }
    }
    if (whole && said_ok && ok) return true;
    if (whole && said_ok && why_not != nullptr) {
      why_not = "the hub refused, and did not say why";
    }
  }
  if (why_not != nullptr && detail != nullptr && capacity > 0) {
    std::strncpy(detail, why_not, capacity - 1);
    detail[capacity - 1] = '\0';
  }
  return false;
}

}  // namespace sentry
