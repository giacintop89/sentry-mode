#include "sentry/json.h"

#include <cstdlib>
#include <cstring>

namespace sentry {
namespace {

bool is_space(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r'; }
bool is_digit(char c) { return c >= '0' && c <= '9'; }

int hex_value(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Which control characters a piece of text may contain. The two callers disagree about
// exactly one thing: what this node writes is plain text, while what it reads may carry
// the whitespace JSON has escapes for, because a hub is allowed to send a name with a
// newline in it. Neither of them accepts NUL, which would cut the text in half for the
// next piece of code that treats the buffer as a C string.
bool plain(unsigned char c) { return c >= 0x20 && c != 0x7F; }
bool plain_or_whitespace(unsigned char c) {
  return plain(c) || c == '\t' || c == '\n' || c == '\r';
}

// Write one code point as UTF-8. Returns how many bytes it took, or 0 if it does not fit.
size_t put_utf8(uint32_t code, char* out, size_t capacity) {
  if (code < 0x80) {
    if (capacity < 1) return 0;
    out[0] = static_cast<char>(code);
    return 1;
  }
  if (code < 0x800) {
    if (capacity < 2) return 0;
    out[0] = static_cast<char>(0xC0 | (code >> 6));
    out[1] = static_cast<char>(0x80 | (code & 0x3F));
    return 2;
  }
  if (code < 0x10000) {
    if (capacity < 3) return 0;
    out[0] = static_cast<char>(0xE0 | (code >> 12));
    out[1] = static_cast<char>(0x80 | ((code >> 6) & 0x3F));
    out[2] = static_cast<char>(0x80 | (code & 0x3F));
    return 3;
  }
  if (capacity < 4) return 0;
  out[0] = static_cast<char>(0xF0 | (code >> 18));
  out[1] = static_cast<char>(0x80 | ((code >> 12) & 0x3F));
  out[2] = static_cast<char>(0x80 | ((code >> 6) & 0x3F));
  out[3] = static_cast<char>(0x80 | (code & 0x3F));
  return 4;
}

}  // namespace

bool Span::is(const char* text) const {
  size_t length = std::strlen(text);
  return size == length && std::memcmp(data, text, length) == 0;
}

namespace {

bool walk_text(const char* data, size_t size, bool (*keeps)(unsigned char)) {
  size_t at = 0;
  while (at < size) {
    unsigned char lead = static_cast<unsigned char>(data[at]);
    if (lead < 0x80 && !keeps(lead)) return false;
    size_t extra = 0;
    uint32_t code = 0;
    if (lead < 0x80) {
      at += 1;
      continue;
    } else if ((lead & 0xE0) == 0xC0) {
      extra = 1;
      code = lead & 0x1F;
    } else if ((lead & 0xF0) == 0xE0) {
      extra = 2;
      code = lead & 0x0F;
    } else if ((lead & 0xF8) == 0xF0) {
      extra = 3;
      code = lead & 0x07;
    } else {
      return false;  // a continuation byte where a lead byte should be
    }
    if (at + extra >= size) return false;
    for (size_t step = 1; step <= extra; ++step) {
      unsigned char next = static_cast<unsigned char>(data[at + step]);
      if ((next & 0xC0) != 0x80) return false;
      code = (code << 6) | (next & 0x3F);
    }
    // The shortest form is the only form: an overlong encoding is a way of smuggling a
    // character past something that compared bytes.
    if ((extra == 1 && code < 0x80) || (extra == 2 && code < 0x800) ||
        (extra == 3 && code < 0x10000)) {
      return false;
    }
    if (code > 0x10FFFF || (code >= 0xD800 && code <= 0xDFFF)) return false;
    at += extra + 1;
  }
  return true;
}

}  // namespace

bool is_clean_text(const char* data, size_t size) { return walk_text(data, size, plain); }

// -- reading ---------------------------------------------------------------------------

void Reader::space() {
  while (at_ < size_ && is_space(data_[at_])) ++at_;
}

Kind Reader::peek() {
  space();
  if (at_ >= size_) return Kind::kEnd;
  switch (data_[at_]) {
    case '{':
      return Kind::kObject;
    case '[':
      return Kind::kArray;
    case '"':
      return Kind::kString;
    case 't':
      return Kind::kTrue;
    case 'f':
      return Kind::kFalse;
    case 'n':
      return Kind::kNull;
    default:
      return (data_[at_] == '-' || is_digit(data_[at_])) ? Kind::kNumber : Kind::kEnd;
  }
}

bool Reader::literal(const char* word) {
  size_t length = std::strlen(word);
  if (at_ + length > size_ || std::memcmp(data_ + at_, word, length) != 0) return fail();
  at_ += length;
  return true;
}

bool Reader::scan_string(Span& out) {
  space();
  if (at_ >= size_ || data_[at_] != '"') return fail();
  size_t start = ++at_;
  while (at_ < size_) {
    char c = data_[at_];
    if (c == '\\') {
      if (at_ + 1 >= size_) return fail();
      at_ += 2;
      continue;
    }
    if (c == '"') {
      out = Span{data_ + start, at_ - start};
      ++at_;
      return true;
    }
    if (static_cast<unsigned char>(c) < 0x20) return fail();  // a raw control byte
    ++at_;
  }
  return fail();
}

bool Reader::scan_number(Span& out) {
  space();
  size_t start = at_;
  if (at_ < size_ && data_[at_] == '-') ++at_;
  if (at_ >= size_ || !is_digit(data_[at_])) return fail();
  // A leading zero is not JSON: 01 would otherwise read as 1 and leave a digit behind for
  // the next thing to trip over, which is a strange way to learn a document was malformed.
  size_t whole = at_;
  while (at_ < size_ && is_digit(data_[at_])) ++at_;
  if (data_[whole] == '0' && at_ - whole > 1) return fail();
  if (at_ < size_ && data_[at_] == '.') {
    ++at_;
    if (at_ >= size_ || !is_digit(data_[at_])) return fail();
    while (at_ < size_ && is_digit(data_[at_])) ++at_;
  }
  if (at_ < size_ && (data_[at_] == 'e' || data_[at_] == 'E')) {
    ++at_;
    if (at_ < size_ && (data_[at_] == '+' || data_[at_] == '-')) ++at_;
    if (at_ >= size_ || !is_digit(data_[at_])) return fail();
    while (at_ < size_ && is_digit(data_[at_])) ++at_;
  }
  out = Span{data_ + start, at_ - start};
  return true;
}

bool Reader::seen(const Span& key) {
  int from = depth_ > 0 ? frame_[depth_ - 1] : 0;
  for (int index = from; index < keys_seen_; ++index) {
    if (keys_[index].size == key.size && std::memcmp(keys_[index].data, key.data, key.size) == 0) {
      return true;
    }
  }
  return false;
}

bool Reader::begin_object() {
  space();
  if (failed_ || at_ >= size_ || data_[at_] != '{') return fail();
  if (depth_ >= kMaxDepth) return fail();
  frame_[depth_] = keys_seen_;
  ++depth_;
  ++at_;
  members_ = 0;
  return true;
}

bool Reader::member(Span& key, Kind& kind) {
  if (failed_) return false;
  space();
  if (at_ >= size_) return fail();
  if (data_[at_] == '}') {
    ++at_;
    --depth_;
    keys_seen_ = frame_[depth_];
    kind = Kind::kEnd;
    return true;
  }
  if (data_[at_] == ',') {
    ++at_;
    space();
  }
  if (at_ < size_ && data_[at_] == '}') return fail();  // a comma with nothing after it
  if (!scan_string(key)) return false;
  if (key.size > kMaxKey) return fail();
  if (seen(key)) return fail();  // the same key twice: whoever sent the second one decides
  if (keys_seen_ >= kMaxMembers) return fail();
  keys_[keys_seen_++] = key;
  if (++members_ > kMaxMembers) return fail();
  space();
  if (at_ >= size_ || data_[at_] != ':') return fail();
  ++at_;
  kind = peek();
  if (kind == Kind::kEnd) return fail();
  return true;
}

bool Reader::begin_array() {
  space();
  if (failed_ || at_ >= size_ || data_[at_] != '[') return fail();
  if (depth_ >= kMaxDepth) return fail();
  ++depth_;
  ++at_;
  return true;
}

bool Reader::element(Kind& kind) {
  if (failed_) return false;
  space();
  if (at_ >= size_) return fail();
  if (data_[at_] == ']') {
    ++at_;
    --depth_;
    kind = Kind::kEnd;
    return true;
  }
  if (data_[at_] == ',') {
    ++at_;
    space();
    if (at_ < size_ && data_[at_] == ']') return fail();
  }
  kind = peek();
  if (kind == Kind::kEnd) return fail();
  return true;
}

bool Reader::text(Span& out) { return scan_string(out); }

bool Reader::string(char* out, size_t capacity, size_t& length) {
  Span raw;
  if (!scan_string(raw)) return false;
  length = 0;
  for (size_t index = 0; index < raw.size;) {
    char c = raw.data[index];
    if (c != '\\') {
      if (length >= capacity) return fail();
      out[length++] = c;
      ++index;
      continue;
    }
    if (index + 1 >= raw.size) return fail();
    char escape = raw.data[index + 1];
    index += 2;
    char plain = 0;
    switch (escape) {
      case '"':
        plain = '"';
        break;
      case '\\':
        plain = '\\';
        break;
      case '/':
        plain = '/';
        break;
      case 'b':
        plain = '\b';
        break;
      case 'f':
        plain = '\f';
        break;
      case 'n':
        plain = '\n';
        break;
      case 'r':
        plain = '\r';
        break;
      case 't':
        plain = '\t';
        break;
      case 'u': {
        if (index + 4 > raw.size) return fail();
        uint32_t code = 0;
        for (int step = 0; step < 4; ++step) {
          int digit = hex_value(raw.data[index + step]);
          if (digit < 0) return fail();
          code = (code << 4) | static_cast<uint32_t>(digit);
        }
        index += 4;
        if (code >= 0xD800 && code <= 0xDBFF) {
          // A character outside the basic plane is written as two halves, and half of one
          // is not a character: an unpaired surrogate is refused rather than replaced.
          if (index + 6 > raw.size || raw.data[index] != '\\' || raw.data[index + 1] != 'u') {
            return fail();
          }
          uint32_t low = 0;
          for (int step = 0; step < 4; ++step) {
            int digit = hex_value(raw.data[index + 2 + step]);
            if (digit < 0) return fail();
            low = (low << 4) | static_cast<uint32_t>(digit);
          }
          if (low < 0xDC00 || low > 0xDFFF) return fail();
          index += 6;
          code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
        } else if (code >= 0xDC00 && code <= 0xDFFF) {
          return fail();
        }
        size_t written = put_utf8(code, out + length, capacity - length);
        if (written == 0) return fail();
        length += written;
        continue;
      }
      default:
        return fail();
    }
    if (length >= capacity) return fail();
    out[length++] = plain;
  }
  if (!walk_text(out, length, plain_or_whitespace)) return fail();
  return true;
}

namespace {

bool written_whole(const Span& raw) {
  for (size_t index = 0; index < raw.size; ++index) {
    char c = raw.data[index];
    if (c == '.' || c == 'e' || c == 'E') return false;
  }
  return true;
}

// An integer, refusing one too large to hold rather than wrapping it into a small one.
bool to_integer(const Span& raw, int64_t& out) {
  bool negative = raw.size > 0 && raw.data[0] == '-';
  uint64_t magnitude = 0;
  for (size_t index = negative ? 1 : 0; index < raw.size; ++index) {
    uint64_t digit = static_cast<uint64_t>(raw.data[index] - '0');
    if (magnitude > (UINT64_C(9223372036854775807) - digit) / 10) return false;
    magnitude = magnitude * 10 + digit;
  }
  out = negative ? -static_cast<int64_t>(magnitude) : static_cast<int64_t>(magnitude);
  return true;
}

bool to_number(const Span& raw, double& out) {
  char buffer[40];
  if (raw.size >= sizeof(buffer)) return false;
  std::memcpy(buffer, raw.data, raw.size);
  buffer[raw.size] = '\0';
  char* end = nullptr;
  out = std::strtod(buffer, &end);
  return end == buffer + raw.size;
}

}  // namespace

bool Reader::integer(int64_t& out) {
  Span raw;
  if (!scan_number(raw)) return false;
  if (!written_whole(raw)) return fail();  // an integer, not a number that happens to be one
  return to_integer(raw, out) ? true : fail();
}

bool Reader::number(double& out) {
  Span raw;
  if (!scan_number(raw)) return false;
  return to_number(raw, out) ? true : fail();
}

bool Reader::scalar_number(bool& whole, int64_t& as_integer, double& as_number) {
  Span raw;
  if (!scan_number(raw)) return false;
  whole = written_whole(raw) && to_integer(raw, as_integer);
  return to_number(raw, as_number) ? true : fail();
}

bool Reader::boolean(bool& out) {
  space();
  if (at_ < size_ && data_[at_] == 't') {
    out = true;
    return literal("true");
  }
  out = false;
  return literal("false");
}

bool Reader::null() { return literal("null"); }

bool Reader::skip() {
  Kind kind = peek();
  switch (kind) {
    case Kind::kString: {
      Span ignored;
      return scan_string(ignored);
    }
    case Kind::kNumber: {
      Span ignored;
      return scan_number(ignored);
    }
    case Kind::kTrue:
      return literal("true");
    case Kind::kFalse:
      return literal("false");
    case Kind::kNull:
      return literal("null");
    case Kind::kObject: {
      if (!begin_object()) return false;
      Span key;
      Kind inner = Kind::kEnd;
      while (member(key, inner)) {
        if (inner == Kind::kEnd) return true;
        if (!skip()) return false;
      }
      return false;
    }
    case Kind::kArray: {
      if (!begin_array()) return false;
      Kind inner = Kind::kEnd;
      while (element(inner)) {
        if (inner == Kind::kEnd) return true;
        if (!skip()) return false;
      }
      return false;
    }
    case Kind::kEnd:
    default:
      return fail();
  }
}

// -- writing ---------------------------------------------------------------------------

void Writer::put(char character) {
  if (!ok_) return;
  if (at_ >= capacity_) {
    ok_ = false;
    return;
  }
  buffer_[at_++] = character;
}

void Writer::append(const char* text, size_t length) {
  for (size_t index = 0; index < length && ok_; ++index) put(text[index]);
}

void Writer::comma() {
  if (!first_) put(',');
  first_ = false;
}

void Writer::object_open() {
  comma();
  put('{');
  first_ = true;
}

void Writer::object_close() {
  put('}');
  first_ = false;
}

void Writer::array_open() {
  comma();
  put('[');
  first_ = true;
}

void Writer::array_close() {
  put(']');
  first_ = false;
}

void Writer::key(const char* name) {
  comma();
  put('"');
  append(name, std::strlen(name));
  put('"');
  put(':');
  first_ = true;  // the value is not separated from its own key by a comma
}

void Writer::string(const char* value) { string(value, std::strlen(value)); }

void Writer::string(const char* value, size_t length) {
  comma();
  if (!is_clean_text(value, length)) {
    ok_ = false;
    return;
  }
  put('"');
  for (size_t index = 0; index < length && ok_; ++index) {
    char c = value[index];
    if (c == '"' || c == '\\') {
      put('\\');
      put(c);
    } else {
      put(c);
    }
  }
  put('"');
}

void Writer::integer(int64_t value) {
  comma();
  char digits[24];
  int length = 0;
  bool negative = value < 0;
  uint64_t magnitude = negative ? ~static_cast<uint64_t>(value) + 1 : static_cast<uint64_t>(value);
  do {
    digits[length++] = static_cast<char>('0' + magnitude % 10);
    magnitude /= 10;
  } while (magnitude > 0);
  if (negative) put('-');
  while (length > 0) put(digits[--length]);
}

void Writer::fixed(double value, int decimals) {
  comma();
  if (decimals < 0 || decimals > 6) {
    ok_ = false;
    return;
  }
  bool negative = value < 0;
  if (negative) value = -value;
  int64_t scale = 1;
  for (int step = 0; step < decimals; ++step) scale *= 10;
  // Rounded once, here, rather than by whatever printf the toolchain happens to bring.
  double scaled = value * static_cast<double>(scale) + 0.5;
  if (scaled >= 9.2e18) {
    ok_ = false;
    return;
  }
  int64_t whole = static_cast<int64_t>(scaled);
  if (negative && whole != 0) put('-');
  first_ = true;
  integer(whole / scale);
  if (decimals == 0) return;
  put('.');
  int64_t fraction = whole % scale;
  for (int place = decimals - 1; place >= 0; --place) {
    int64_t step = 1;
    for (int count = 0; count < place; ++count) step *= 10;
    put(static_cast<char>('0' + (fraction / step) % 10));
  }
}

void Writer::boolean(bool value) {
  comma();
  const char* text = value ? "true" : "false";
  append(text, std::strlen(text));
}

void Writer::null() {
  comma();
  append("null", 4);
}

void Writer::raw(const char* text) {
  comma();
  append(text, std::strlen(text));
}

}  // namespace sentry
