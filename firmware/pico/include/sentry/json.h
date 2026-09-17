// Reading and writing the contract's JSON, in a fixed amount of memory.
//
// The satellite contract is JSON, and a microcontroller has 264 kB of RAM and no business
// running a general parser over a payload a broker handed it. What is here is not a JSON
// library: it is a reader for the shapes the contract actually uses — an object of plain
// members, at most one array of objects of plain members — and a writer that appends into
// a buffer whose size the caller decided in advance.
//
// Everything refuses rather than truncates. A string that does not fit, a document nested
// deeper than the contract can be, a number that is not one, a key repeated, a byte that
// is not valid UTF-8: each of these ends the parse with false. A parser that quietly kept
// the last of two values for the same key would let whoever sent the second one decide
// what this node does, which is the whole of the attack.

#ifndef SENTRY_JSON_H
#define SENTRY_JSON_H

#include <cstddef>
#include <cstdint>

namespace sentry {

// The most nesting the contract ever asks for: a command, its `sources` array, one source.
inline constexpr int kMaxDepth = 4;
// The longest string the contract allows anywhere, from `control.py`.
inline constexpr size_t kMaxString = 128;
// Keys are short and known; one longer than this cannot be a key we act on.
inline constexpr size_t kMaxKey = 32;
// At most this many members in one object, so a malicious document cannot make us loop.
inline constexpr int kMaxMembers = 64;

enum class Kind { kEnd, kString, kNumber, kTrue, kFalse, kNull, kObject, kArray };

// A piece of the document, pointing into the caller's buffer. Never NUL-terminated.
struct Span {
  const char* data = nullptr;
  size_t size = 0;

  bool is(const char* text) const;
};

// Walks one document once, forwards. It holds no memory of its own beyond the cursor.
class Reader {
 public:
  Reader(const char* data, size_t size) : data_(data), size_(size) {}

  // Position on the members of the object the document is. False if it is not one.
  bool begin_object();
  // The next member's key, with the reader positioned on its value. kEnd when done.
  bool member(Span& key, Kind& kind);
  // Position on the elements of the array the cursor is on.
  bool begin_array();
  // The next element's kind, with the reader positioned on it. kEnd when done.
  bool element(Kind& kind);

  // Read the value the cursor is on. Each of these consumes it.
  bool string(char* out, size_t capacity, size_t& length);
  bool text(Span& out);
  bool integer(int64_t& out);
  bool number(double& out);
  // Read a number without being told in advance which kind it is. `whole` says whether it
  // was written as one: a pin is 17, and a node that turned 17.0 into a pin would be
  // deciding for the hub what it meant.
  bool scalar_number(bool& whole, int64_t& as_integer, double& as_number);
  bool boolean(bool& out);
  bool null();
  bool skip();

  bool done() const { return failed_ || at_ >= size_; }
  bool failed() const { return failed_; }

 private:
  bool fail() {
    failed_ = true;
    return false;
  }
  void space();
  bool literal(const char* word);
  bool scan_string(Span& out);
  bool scan_number(Span& out);
  Kind peek();
  bool seen(const Span& key);

  const char* data_;
  size_t size_;
  size_t at_ = 0;
  bool failed_ = false;
  int depth_ = 0;
  int members_ = 0;
  // The keys read so far in each object on the way down, so a repeat is refused rather
  // than silently taken. Closing an object forgets its own keys and no others.
  Span keys_[kMaxMembers];
  int keys_seen_ = 0;
  // Where each open object's keys start in keys_, so closing one forgets exactly its own.
  int frame_[kMaxDepth] = {};
};

// Appends JSON into a caller's buffer, and remembers if it ever ran out of room.
//
// It does not grow, it does not allocate, and a writer that overflowed produces nothing:
// `ok()` is false and the buffer is not to be sent. Half a message is worse than none.
class Writer {
 public:
  Writer(char* buffer, size_t capacity) : buffer_(buffer), capacity_(capacity) {}

  void object_open();
  void object_close();
  void array_open();
  void array_close();
  void key(const char* name);
  void string(const char* value);
  void string(const char* value, size_t length);
  void integer(int64_t value);
  // A number with `decimals` places, written without the platform's printf("%f").
  void fixed(double value, int decimals);
  void boolean(bool value);
  void null();
  void raw(const char* text);

  bool ok() const { return ok_; }
  size_t size() const { return at_; }
  const char* data() const { return buffer_; }

 private:
  void put(char character);
  void append(const char* text, size_t length);
  void comma();

  char* buffer_;
  size_t capacity_;
  size_t at_ = 0;
  bool ok_ = true;
  bool first_ = true;
};

// Whether a run of bytes is text this node may keep: valid UTF-8, no control characters,
// no NUL. The hub's contract says a string; this says what a string is allowed to contain.
bool is_clean_text(const char* data, size_t size);

}  // namespace sentry

#endif  // SENTRY_JSON_H
