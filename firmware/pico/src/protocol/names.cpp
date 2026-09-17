#include "sentry/names.h"

#include <cstring>

#include "sentry/json.h"

namespace sentry {

bool is_name(const char* text, size_t length) {
  if (text == nullptr) return false;
  if (length == 0 || length > 40) return false;
  for (size_t index = 0; index < length; ++index) {
    char c = text[index];
    if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) continue;
    if (c == '-' && index > 0 && index + 1 < length) continue;
    return false;
  }
  return true;
}

bool is_name(const char* text) {
  return text != nullptr && is_name(text, std::strlen(text));
}

bool is_uuid(const char* text) {
  if (text == nullptr) return false;
  static const int kDashes[] = {8, 13, 18, 23};
  size_t length = std::strlen(text);
  if (length != 36) return false;
  for (size_t index = 0; index < length; ++index) {
    bool dash = false;
    for (int at : kDashes) dash = dash || index == static_cast<size_t>(at);
    char c = text[index];
    if (dash) {
      if (c != '-') return false;
      continue;
    }
    bool hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
    if (!hex) return false;
  }
  return true;
}

bool is_kind(const char* text) {
  if (text == nullptr) return false;
  const char* dot = std::strchr(text, '.');
  if (dot == nullptr || dot == text) return false;
  if (!(*text >= 'a' && *text <= 'z')) return false;
  for (const char* at = text; at < dot; ++at) {
    if (!((*at >= 'a' && *at <= 'z') || (*at >= '0' && *at <= '9'))) return false;
  }
  const char* name = dot + 1;
  if (!(*name >= 'a' && *name <= 'z')) return false;
  for (const char* at = name; *at != '\0'; ++at) {
    if ((*at >= 'a' && *at <= 'z') || (*at >= '0' && *at <= '9') || *at == '_') continue;
    return false;
  }
  return true;
}

bool is_driver(const char* text) {
  if (text == nullptr) return false;
  size_t length = std::strlen(text);
  if (length == 0 || length > 32) return false;
  if (!(text[0] >= 'a' && text[0] <= 'z')) return false;
  for (size_t index = 1; index < length; ++index) {
    char c = text[index];
    if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_') continue;
    return false;
  }
  return true;
}

bool is_timestamp(const char* text) {
  if (text == nullptr) return false;
  size_t length = std::strlen(text);
  if (length < 20) return false;
  static const int kDigits[] = {0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18};
  for (int index : kDigits) {
    if (text[index] < '0' || text[index] > '9') return false;
  }
  if (text[4] != '-' || text[7] != '-' || text[10] != 'T') return false;
  if (text[13] != ':' || text[16] != ':') return false;
  char last = text[length - 1];
  if (last == 'Z') return true;
  if (length < 6) return false;
  const char* offset = text + length - 6;
  return (offset[0] == '+' || offset[0] == '-') && offset[3] == ':';
}

bool is_url_safe(const char* text, size_t length) {
  if (text == nullptr) return false;
  for (size_t index = 0; index < length; ++index) {
    char c = text[index];
    bool allowed = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                   c == '_' || c == '-';
    if (!allowed) return false;
  }
  return true;
}

bool is_stream_id(const char* text, size_t length) {
  return length >= 8 && length <= 64 && is_url_safe(text, length);
}

bool is_token(const char* text, size_t length) {
  return length >= 32 && length <= 128 && is_url_safe(text, length);
}

bool is_text_within(const char* text, size_t limit) {
  if (text == nullptr) return false;
  size_t length = std::strlen(text);
  return length <= limit && is_clean_text(text, length);
}

}  // namespace sentry
