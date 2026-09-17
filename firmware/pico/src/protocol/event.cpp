#include "sentry/event.h"

#include <cstring>

#include "sentry/json.h"
#include "sentry/names.h"

namespace sentry {
namespace {

bool leap(int year) { return (year % 4 == 0 && year % 100 != 0) || year % 400 == 0; }

}  // namespace

void put_value(Writer& writer, const Value& value) {
  switch (value.type) {
    case Value::Type::kBoolean:
      writer.boolean(value.boolean);
      return;
    case Value::Type::kInteger:
      writer.integer(value.integer);
      return;
    case Value::Type::kNumber:
      writer.fixed(value.number, value.decimals);
      return;
    case Value::Type::kText:
      writer.string(value.text == nullptr ? "" : value.text);
      return;
    case Value::Type::kNone:
    default:
      writer.null();
      return;
  }
}

Value Value::of(bool value) {
  Value made;
  made.type = Type::kBoolean;
  made.boolean = value;
  return made;
}

Value Value::of(int64_t value) {
  Value made;
  made.type = Type::kInteger;
  made.integer = value;
  return made;
}

Value Value::of(double value, int decimals) {
  Value made;
  made.type = Type::kNumber;
  made.number = value;
  made.decimals = decimals;
  return made;
}

Value Value::of(const char* value) {
  Value made;
  made.type = Type::kText;
  made.text = value;
  return made;
}

const char* name_of(Quality quality) {
  switch (quality) {
    case Quality::kDegraded:
      return "degraded";
    case Quality::kUnavailable:
      return "unavailable";
    case Quality::kUnknown:
      return "unknown";
    case Quality::kValid:
    default:
      return "valid";
  }
}

const char* name_of(Clock clock) {
  switch (clock) {
    case Clock::kSynced:
      return "synced";
    case Clock::kUnsynced:
      return "unsynced";
    case Clock::kUnknown:
    default:
      return "unknown";
  }
}

bool write_timestamp(int64_t unix_ms, char* out, size_t capacity) {
  if (capacity < 25 || unix_ms < 0) return false;
  int64_t seconds = unix_ms / 1000;
  int milliseconds = static_cast<int>(unix_ms % 1000);
  int64_t days = seconds / 86400;
  int rest = static_cast<int>(seconds % 86400);
  int year = 1970;
  while (true) {
    int length = leap(year) ? 366 : 365;
    if (days < length) break;
    days -= length;
    ++year;
    if (year > 9999) return false;
  }
  static const int kMonths[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  int month = 0;
  while (month < 12) {
    int length = kMonths[month] + ((month == 1 && leap(year)) ? 1 : 0);
    if (days < length) break;
    days -= length;
    ++month;
  }
  int day = static_cast<int>(days) + 1;
  int hour = rest / 3600;
  int minute = (rest % 3600) / 60;
  int second = rest % 60;
  char* at = out;
  auto two = [&at](int value) {
    *at++ = static_cast<char>('0' + (value / 10) % 10);
    *at++ = static_cast<char>('0' + value % 10);
  };
  two(year / 100);
  two(year % 100);
  *at++ = '-';
  two(month + 1);
  *at++ = '-';
  two(day);
  *at++ = 'T';
  two(hour);
  *at++ = ':';
  two(minute);
  *at++ = ':';
  two(second);
  *at++ = '.';
  *at++ = static_cast<char>('0' + (milliseconds / 100) % 10);
  *at++ = static_cast<char>('0' + (milliseconds / 10) % 10);
  *at++ = static_cast<char>('0' + milliseconds % 10);
  *at++ = 'Z';
  *at = '\0';
  return true;
}

size_t write_event(const Reading& reading, const Delivery& delivery, char* buffer,
                   size_t capacity) {
  // Checked before a byte is written, so that a refusal costs nothing and a buffer is
  // never left holding most of an event.
  if (!is_uuid(reading.event_id) || !is_uuid(reading.boot_id)) return 0;
  if (!is_name(reading.node_id) || !is_name(reading.source_id)) return 0;
  if (!is_kind(reading.kind) || !is_timestamp(reading.occurred_at)) return 0;
  if (reading.sequence < 0 || delivery.hub_epoch < 0 || delivery.queued_ms < 0) return 0;
  if (!is_uuid(delivery.connection_id)) return 0;
  if (delivery.grant_id != nullptr && !is_uuid(delivery.grant_id)) return 0;
  if (reading.unit != nullptr && std::strlen(reading.unit) > 16) return 0;

  Writer writer(buffer, capacity);
  writer.object_open();
  writer.key("schema_version");
  writer.integer(1);
  writer.key("event");
  writer.object_open();
  writer.key("event_id");
  writer.string(reading.event_id);
  writer.key("node_id");
  writer.string(reading.node_id);
  writer.key("source_id");
  writer.string(reading.source_id);
  writer.key("boot_id");
  writer.string(reading.boot_id);
  writer.key("sequence");
  writer.integer(reading.sequence);
  writer.key("kind");
  writer.string(reading.kind);
  writer.key("occurred_at");
  writer.string(reading.occurred_at);
  writer.key("clock_status");
  writer.string(name_of(reading.clock));
  writer.key("value");
  put_value(writer, reading.value);
  writer.key("unit");
  if (reading.unit == nullptr) {
    writer.null();
  } else {
    writer.string(reading.unit);
  }
  writer.key("quality");
  writer.string(name_of(reading.quality));
  writer.object_close();
  writer.key("delivery");
  writer.object_open();
  writer.key("connection_id");
  writer.string(delivery.connection_id);
  writer.key("hub_epoch");
  writer.integer(delivery.hub_epoch);
  writer.key("grant_id");
  if (delivery.grant_id == nullptr) {
    writer.null();
  } else {
    writer.string(delivery.grant_id);
  }
  writer.key("queued_ms");
  writer.integer(delivery.queued_ms);
  writer.key("replayed");
  writer.boolean(delivery.replayed);
  writer.key("initial_state");
  writer.boolean(delivery.initial_state);
  writer.object_close();
  writer.object_close();
  return writer.ok() ? writer.size() : 0;
}

}  // namespace sentry
