#include "sentry/topics.h"

#include <cstring>

#include "sentry/names.h"

namespace sentry {
namespace {

// The hub's own pattern for the prefix: lower-case segments separated by single slashes,
// which leaves no room for a wildcard, an empty segment or a walk upwards.
bool is_prefix(const char* text) {
  if (text == nullptr) return false;
  size_t length = std::strlen(text);
  if (length == 0 || length > 32) return false;
  size_t segment = 0;
  for (size_t index = 0; index < length; ++index) {
    char c = text[index];
    if (c == '/') {
      if (segment == 0) return false;
      segment = 0;
      continue;
    }
    if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9'))) return false;
    ++segment;
  }
  return segment > 0;
}

}  // namespace

const char* name_of(Channel channel) {
  switch (channel) {
    case Channel::kState:
      return "state";
    case Channel::kHealth:
      return "health";
    case Channel::kAcks:
      return "acks";
    case Channel::kCommands:
      return "commands";
    case Channel::kEvents:
    default:
      return "events";
  }
}

bool topic(const char* prefix, const char* node_id, Channel channel, char* out,
           size_t capacity) {
  if (out == nullptr || !is_prefix(prefix) || !is_name(node_id)) return false;
  const char* tail = name_of(channel);
  size_t needed = std::strlen(prefix) + 7 + std::strlen(node_id) + 1 + std::strlen(tail) + 1;
  if (needed > capacity) return false;
  char* at = out;
  auto append = [&at](const char* text) {
    size_t length = std::strlen(text);
    std::memcpy(at, text, length);
    at += length;
  };
  append(prefix);
  append("/nodes/");
  append(node_id);
  append("/");
  append(tail);
  *at = '\0';
  return true;
}

}  // namespace sentry
