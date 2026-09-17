#include "sentry/control.h"

#include <cstring>

#include "sentry/json.h"
#include "sentry/names.h"

namespace sentry {
namespace {

// The hub's MAX_TEXT: the longest one option value may be.
constexpr size_t kMaxOptionText = 128;
// agent_version and profile, from the model.
constexpr size_t kMaxShortText = 32;
// What the contract allows a unit to be, and what `spool.h` keeps one in.
constexpr size_t kMaxUnitText = 16;
// An option name is a key in the node's own configuration file, not free text.
constexpr size_t kMaxOptionName = 64;

bool put_option(Writer& writer, const DeclaredOption& option) {
  if (!is_text_within(option.name, kMaxOptionName)) return false;
  writer.key(option.name);
  switch (option.value.type) {
    case Value::Type::kBoolean:
      writer.boolean(option.value.boolean);
      return true;
    case Value::Type::kInteger:
      writer.integer(option.value.integer);
      return true;
    case Value::Type::kNumber:
      writer.fixed(option.value.number, option.value.decimals);
      return true;
    case Value::Type::kText:
      if (!is_text_within(option.value.text, kMaxOptionText)) return false;
      writer.string(option.value.text);
      return true;
    case Value::Type::kNone:
    default:
      writer.null();
      return true;
  }
}

bool sources_are_sayable(const DeclaredSource* sources, size_t count) {
  if (count > kMaxDeclaredSources) return false;
  if (count > 0 && sources == nullptr) return false;
  for (size_t index = 0; index < count; ++index) {
    const DeclaredSource& source = sources[index];
    if (!is_name(source.source_id) || !is_driver(source.kind)) return false;
    if (source.option_count > kMaxOptionsPerSource) return false;
    if (source.option_count > 0 && source.options == nullptr) return false;
    for (size_t at = 0; at < source.option_count; ++at) {
      const DeclaredOption& option = source.options[at];
      if (!is_text_within(option.name, kMaxOptionName)) return false;
      if (option.value.type == Value::Type::kText &&
          !is_text_within(option.value.text, kMaxOptionText)) {
        return false;
      }
    }
  }
  return true;
}

void put_who(Writer& writer, const char* node_id, const char* boot_id) {
  writer.key("schema_version");
  writer.integer(1);
  writer.key("node_id");
  writer.string(node_id);
  writer.key("boot_id");
  writer.string(boot_id);
}

}  // namespace

const char* name_of(Outcome outcome) {
  switch (outcome) {
    case Outcome::kReceived:
      return "received";
    case Outcome::kFailed:
      return "failed";
    case Outcome::kApplied:
    default:
      return "applied";
  }
}

size_t write_state(const State& state, char* buffer, size_t capacity) {
  if (!is_name(state.node_id) || !is_uuid(state.boot_id) || !is_uuid(state.connection_id)) {
    return 0;
  }
  if (state.firmware_version != nullptr &&
      !is_text_within(state.firmware_version, kMaxShortText)) {
    return 0;
  }
  if (state.profile != nullptr && !is_text_within(state.profile, kMaxShortText)) return 0;
  if (!sources_are_sayable(state.sources, state.source_count)) return 0;

  Writer writer(buffer, capacity);
  writer.object_open();
  put_who(writer, state.node_id, state.boot_id);
  writer.key("connection_id");
  writer.string(state.connection_id);
  writer.key("online");
  writer.boolean(state.online);
  if (state.firmware_version != nullptr) {
    writer.key("agent_version");
    writer.string(state.firmware_version);
  }
  if (state.profile != nullptr) {
    writer.key("profile");
    writer.string(state.profile);
  }
  if (state.config_revision >= 0) {
    writer.key("config_revision");
    writer.integer(state.config_revision);
  }
  writer.key("sources");
  writer.array_open();
  for (size_t index = 0; index < state.source_count; ++index) {
    const DeclaredSource& source = state.sources[index];
    writer.object_open();
    writer.key("source_id");
    writer.string(source.source_id);
    writer.key("kind");
    writer.string(source.kind);
    writer.key("enabled");
    writer.boolean(source.enabled);
    writer.key("options");
    writer.object_open();
    for (size_t at = 0; at < source.option_count; ++at) {
      if (!put_option(writer, source.options[at])) return 0;
    }
    writer.object_close();
    writer.object_close();
  }
  writer.array_close();
  writer.object_close();
  return writer.ok() ? writer.size() : 0;
}

size_t write_goodbye(const char* node_id, const char* boot_id, const char* connection_id,
                     size_t topic_bytes, char* buffer, size_t capacity) {
  if (!is_name(node_id) || !is_uuid(boot_id) || !is_uuid(connection_id)) return 0;

  // Only the required fields, and not even an empty `sources`. That is not tidiness: with
  // the longest name the contract allows, the five fields come to 192 bytes and the topic
  // to 62, which is 254 of the 255 lwIP can express. An empty array would cost 13 of them
  // and the node with the long name would silently have no will at all.
  Writer writer(buffer, capacity);
  writer.object_open();
  put_who(writer, node_id, boot_id);
  writer.key("connection_id");
  writer.string(connection_id);
  writer.key("online");
  writer.boolean(false);
  writer.object_close();
  if (!writer.ok()) return 0;
  // A goodbye that does not fit is not a goodbye that gets truncated; it is a CONNECT that
  // fails, months from now, on a board. So it fails here instead.
  if (writer.size() + topic_bytes > kMaxWillBytes) return 0;
  return writer.size();
}

size_t write_health(const Health& health, char* buffer, size_t capacity) {
  if (!is_name(health.node_id) || !is_uuid(health.boot_id)) return 0;
  if (health.uptime_seconds < 0) return 0;
  const QueueHealth& queue = health.queue;
  if (queue.events < 0 || queue.bytes < 0 || queue.published < 0 || queue.refused < 0) return 0;
  if (queue.drops_count < 0 || queue.drops_bytes < 0 || queue.drops_age < 0 ||
      queue.drops_total < 0) {
    return 0;
  }
  if (health.board.has_uptime && health.board.uptime_seconds < 0) return 0;
  if (health.board.has_free_heap && health.board.memory_available_kb < 0) return 0;
  if (health.source_count > kMaxDeclaredSources) return 0;
  if (health.source_count > 0 && health.sources == nullptr) return 0;
  for (size_t index = 0; index < health.source_count; ++index) {
    const SourceHealth& source = health.sources[index];
    if (!is_name(source.source_id) || source.readings < 0) return 0;
    if (source.driver != nullptr && !is_text_within(source.driver, kMaxShortText)) return 0;
    if (source.error != nullptr && !is_text_within(source.error, kMaxDetailText)) return 0;
    if (source.unit != nullptr && !is_text_within(source.unit, kMaxUnitText)) return 0;
    if (source.has_last && source.last.type == Value::Type::kText &&
        (source.last.text == nullptr || !is_text_within(source.last.text, kMaxShortText))) {
      return 0;
    }
    if (source.has_age && source.last_reading_age_seconds < 0) return 0;
  }

  Writer writer(buffer, capacity);
  writer.object_open();
  put_who(writer, health.node_id, health.boot_id);
  writer.key("agent_uptime_seconds");
  writer.fixed(health.uptime_seconds, 1);
  writer.key("clock_status");
  writer.string(name_of(health.clock));
  writer.key("queue");
  writer.object_open();
  writer.key("events");
  writer.integer(queue.events);
  writer.key("bytes");
  writer.integer(queue.bytes);
  writer.key("published");
  writer.integer(queue.published);
  writer.key("refused");
  writer.integer(queue.refused);
  writer.key("drops");
  writer.object_open();
  writer.key("count");
  writer.integer(queue.drops_count);
  writer.key("bytes");
  writer.integer(queue.drops_bytes);
  writer.key("age");
  writer.integer(queue.drops_age);
  writer.key("total");
  writer.integer(queue.drops_total);
  writer.object_close();
  writer.key("granted");
  writer.boolean(queue.granted);
  writer.object_close();
  writer.key("sources");
  writer.object_open();
  for (size_t index = 0; index < health.source_count; ++index) {
    const SourceHealth& source = health.sources[index];
    writer.key(source.source_id);
    writer.object_open();
    writer.key("readings");
    writer.integer(source.readings);
    if (source.driver != nullptr) {
      writer.key("driver");
      writer.string(source.driver);
    }
    if (source.error != nullptr) {
      writer.key("error");
      writer.string(source.error);
    }
    if (source.has_age) {
      writer.key("last_reading_age_seconds");
      writer.fixed(source.last_reading_age_seconds, 1);
    }
    if (source.has_last) {
      // The same three fields an event carries, written by the same code: a hub comparing
      // what health says with the last event it was sent is comparing like with like.
      writer.key("last");
      writer.object_open();
      writer.key("value");
      put_value(writer, source.last);
      writer.key("unit");
      if (source.unit == nullptr) {
        writer.null();
      } else {
        writer.string(source.unit);
      }
      writer.key("quality");
      writer.string(name_of(source.quality));
      writer.object_close();
    }
    writer.object_close();
  }
  writer.object_close();
  // A board says what it measured and nothing about the rest. What is missing is unknown,
  // and unknown is written by not being there — never as a zero somebody could plot.
  writer.key("board");
  writer.object_open();
  if (health.board.has_uptime) {
    writer.key("uptime_seconds");
    writer.fixed(health.board.uptime_seconds, 1);
  }
  if (health.board.has_temperature) {
    writer.key("temperature_c");
    writer.fixed(health.board.temperature_c, 1);
  }
  if (health.board.has_free_heap) {
    writer.key("memory_available_kb");
    writer.integer(health.board.memory_available_kb);
  }
  writer.object_close();
  writer.object_close();
  return writer.ok() ? writer.size() : 0;
}

size_t write_ack(const char* command_id, const char* node_id, Outcome outcome,
                 const char* detail, char* buffer, size_t capacity) {
  if (command_id == nullptr || std::strlen(command_id) == 0) return 0;
  if (!is_text_within(command_id, 64)) return 0;
  if (!is_name(node_id)) return 0;
  if (detail != nullptr && !is_text_within(detail, kMaxDetailText)) return 0;

  Writer writer(buffer, capacity);
  writer.object_open();
  writer.key("schema_version");
  writer.integer(1);
  writer.key("command_id");
  writer.string(command_id);
  writer.key("node_id");
  writer.string(node_id);
  writer.key("outcome");
  writer.string(name_of(outcome));
  writer.key("detail");
  if (detail == nullptr) {
    writer.null();
  } else {
    writer.string(detail);
  }
  writer.object_close();
  return writer.ok() ? writer.size() : 0;
}

}  // namespace sentry
