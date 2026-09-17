#include "sentry/command.h"

#include <cstring>

#include "sentry/json.h"

namespace sentry {
namespace {

struct Fields {
  static constexpr uint32_t kCapability = 1u << 0;
  static constexpr uint32_t kGrantId = 1u << 1;
  static constexpr uint32_t kDuration = 1u << 2;
  static constexpr uint32_t kSequence = 1u << 3;
  static constexpr uint32_t kRevision = 1u << 4;
  static constexpr uint32_t kSources = 1u << 5;
  static constexpr uint32_t kStreamId = 1u << 6;
  static constexpr uint32_t kSourceId = 1u << 7;
  static constexpr uint32_t kPort = 1u << 8;
  static constexpr uint32_t kToken = 1u << 9;
  static constexpr uint32_t kCommandId = 1u << 10;
  static constexpr uint32_t kAction = 1u << 11;
  static constexpr uint32_t kNodeId = 1u << 12;
  static constexpr uint32_t kEpoch = 1u << 13;
};

constexpr uint32_t kStreamFields =
    Fields::kStreamId | Fields::kSourceId | Fields::kPort | Fields::kToken;
constexpr uint32_t kTicketFields = Fields::kSourceId | Fields::kPort | Fields::kToken;
constexpr uint32_t kConfigureFields = Fields::kRevision | Fields::kSources;

// A name, spelled the way `sources/models.py` spells it and nowhere else.
bool is_name(const char* text, size_t length) {
  if (length == 0 || length > 40) return false;
  for (size_t index = 0; index < length; ++index) {
    char c = text[index];
    bool plain = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
    if (plain) continue;
    if (c == '-' && index > 0 && index + 1 < length) continue;
    return false;
  }
  return true;
}

// The alphabet a stream id and a ticket are written in: nothing that could be a path, a
// host or a shell word, whatever the hub thinks it is sending.
bool is_url_safe(const char* text, size_t length) {
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

Action action_of(const char* text, size_t length) {
  struct Known {
    const char* name;
    Action action;
  };
  static const Known kKnown[] = {
      {"grant", Action::kGrant},
      {"renew", Action::kRenew},
      {"revoke", Action::kRevoke},
      {"stop", Action::kStop},
      {"configure", Action::kConfigure},
      {"video_start", Action::kVideoStart},
      {"video_renew", Action::kVideoRenew},
      {"video_stop", Action::kVideoStop},
      {"audio_start", Action::kAudioStart},
      {"audio_renew", Action::kAudioRenew},
      {"audio_stop", Action::kAudioStop},
  };
  for (const Known& known : kKnown) {
    size_t size = std::strlen(known.name);
    if (size == length && std::memcmp(known.name, text, size) == 0) return known.action;
  }
  return Action::kUnknown;
}

Capability capability_of(const char* text, size_t length) {
  if (length == 6 && std::memcmp(text, "events", 6) == 0) return Capability::kEvents;
  if (length == 5 && std::memcmp(text, "video", 5) == 0) return Capability::kVideo;
  if (length == 5 && std::memcmp(text, "audio", 5) == 0) return Capability::kAudio;
  return Capability::kNone;
}

bool is_grant(Action action) {
  return action == Action::kGrant || action == Action::kRenew || action == Action::kRevoke;
}

bool is_start(Action action) {
  return action == Action::kVideoStart || action == Action::kAudioStart;
}

bool is_renewal(Action action) {
  return action == Action::kVideoRenew || action == Action::kAudioRenew;
}

bool is_stop(Action action) {
  return action == Action::kVideoStop || action == Action::kAudioStop;
}

bool is_media(Action action) {
  return is_start(action) || is_renewal(action) || is_stop(action);
}

// Copy a string value into a fixed field, refusing anything that would not fit whole.
bool take(Reader& reader, char* out, size_t capacity) {
  size_t length = 0;
  if (!reader.string(out, capacity - 1, length)) return false;
  out[length] = '\0';
  return true;
}

bool read_option(Reader& reader, Span key, Kind kind, Option& option) {
  if (key.size + 1 > sizeof(option.name)) return false;
  std::memcpy(option.name, key.data, key.size);
  option.name[key.size] = '\0';
  switch (kind) {
    case Kind::kString: {
      option.type = Option::Type::kString;
      return take(reader, option.text, sizeof(option.text));
    }
    case Kind::kNumber: {
      // An option is a whole number when it was written as one: a pin is 17, and a node
      // that turned 17.0 into a pin would be deciding for the hub what it meant.
      bool whole = false;
      if (!reader.scalar_number(whole, option.integer, option.number)) return false;
      option.type = whole ? Option::Type::kInteger : Option::Type::kNumber;
      return true;
    }
    case Kind::kTrue:
    case Kind::kFalse:
      option.type = Option::Type::kBoolean;
      return reader.boolean(option.boolean);
    case Kind::kNull:
      option.type = Option::Type::kNull;
      return reader.null();
    default:
      return false;  // an option is a plain value, never a structure
  }
}

bool read_source(Reader& reader, Source& source, Refusal& why) {
  if (!reader.begin_object()) {
    why = Refusal::kWrongShape;
    return false;
  }
  bool has_id = false;
  bool has_kind = false;
  Span key;
  Kind kind = Kind::kEnd;
  while (reader.member(key, kind)) {
    if (kind == Kind::kEnd) break;
    if (key.is("id")) {
      if (kind != Kind::kString || !take(reader, source.id, sizeof(source.id))) {
        why = Refusal::kMalformed;
        return false;
      }
      if (!is_name(source.id, std::strlen(source.id))) {
        why = Refusal::kMalformed;
        return false;
      }
      has_id = true;
    } else if (key.is("kind")) {
      if (kind != Kind::kString || !take(reader, source.kind, sizeof(source.kind))) {
        why = Refusal::kMalformed;
        return false;
      }
      has_kind = true;
    } else {
      if (source.option_count >= kMaxOptions) {
        why = Refusal::kTooMany;
        return false;
      }
      if (!read_option(reader, key, kind, source.options[source.option_count])) {
        why = Refusal::kMalformed;
        return false;
      }
      ++source.option_count;
    }
  }
  if (reader.failed()) {
    why = Refusal::kMalformed;
    return false;
  }
  if (!has_id || !has_kind) {
    why = Refusal::kMissingField;
    return false;
  }
  return true;
}

}  // namespace

const char* name_of(Action action) {
  switch (action) {
    case Action::kGrant:
      return "grant";
    case Action::kRenew:
      return "renew";
    case Action::kRevoke:
      return "revoke";
    case Action::kStop:
      return "stop";
    case Action::kConfigure:
      return "configure";
    case Action::kVideoStart:
      return "video_start";
    case Action::kVideoRenew:
      return "video_renew";
    case Action::kVideoStop:
      return "video_stop";
    case Action::kAudioStart:
      return "audio_start";
    case Action::kAudioRenew:
      return "audio_renew";
    case Action::kAudioStop:
      return "audio_stop";
    case Action::kUnknown:
    default:
      return "unknown";
  }
}

const char* name_of(Refusal refusal) {
  switch (refusal) {
    case Refusal::kNone:
      return "none";
    case Refusal::kNotAnObject:
      return "not_an_object";
    case Refusal::kUnknownField:
      return "unknown_field";
    case Refusal::kMalformed:
      return "malformed";
    case Refusal::kMissingField:
      return "missing_field";
    case Refusal::kUnknownAction:
      return "unknown_action";
    case Refusal::kNotForThisNode:
      return "not_for_this_node";
    case Refusal::kWrongShape:
      return "wrong_shape";
    case Refusal::kTooMany:
      return "too_many";
    case Refusal::kOutOfRange:
      return "out_of_range";
    default:
      return "unknown";
  }
}

bool parse_command(const char* payload, size_t size, const char* node_id, Command& out,
                   Refusal& why) {
  out = Command{};
  why = Refusal::kNone;
  Reader reader(payload, size);
  if (!reader.begin_object()) {
    why = Refusal::kNotAnObject;
    return false;
  }
  uint32_t said = 0;
  Span key;
  Kind kind = Kind::kEnd;
  while (reader.member(key, kind)) {
    if (kind == Kind::kEnd) break;
    if (key.is("command_id")) {
      if (kind != Kind::kString || !take(reader, out.command_id, sizeof(out.command_id)) ||
          out.command_id[0] == '\0') {
        why = Refusal::kMalformed;
        return false;
      }
      said |= Fields::kCommandId;
    } else if (key.is("action")) {
      char text[24] = {};
      if (kind != Kind::kString || !take(reader, text, sizeof(text))) {
        why = Refusal::kUnknownAction;
        return false;
      }
      out.action = action_of(text, std::strlen(text));
      if (out.action == Action::kUnknown) {
        why = Refusal::kUnknownAction;
        return false;
      }
      said |= Fields::kAction;
    } else if (key.is("node_id")) {
      if (kind != Kind::kString || !take(reader, out.node_id, sizeof(out.node_id)) ||
          !is_name(out.node_id, std::strlen(out.node_id))) {
        why = Refusal::kMalformed;
        return false;
      }
      said |= Fields::kNodeId;
    } else if (key.is("hub_epoch")) {
      if (kind != Kind::kNumber || !reader.integer(out.hub_epoch) || out.hub_epoch < 0) {
        why = Refusal::kOutOfRange;
        return false;
      }
      said |= Fields::kEpoch;
    } else if (key.is("capability")) {
      char text[16] = {};
      if (kind != Kind::kString || !take(reader, text, sizeof(text))) {
        why = Refusal::kMalformed;
        return false;
      }
      out.capability = capability_of(text, std::strlen(text));
      if (out.capability == Capability::kNone) {
        why = Refusal::kMalformed;
        return false;
      }
      said |= Fields::kCapability;
    } else if (key.is("grant_id")) {
      if (kind == Kind::kNull) {
        if (!reader.null()) {
          why = Refusal::kMalformed;
          return false;
        }
      } else if (kind != Kind::kString || !take(reader, out.grant_id, sizeof(out.grant_id))) {
        why = Refusal::kMalformed;
        return false;
      }
      said |= Fields::kGrantId;
    } else if (key.is("duration_seconds")) {
      if (kind != Kind::kNumber || !reader.number(out.duration_seconds) ||
          out.duration_seconds < 0) {
        why = Refusal::kOutOfRange;
        return false;
      }
      said |= Fields::kDuration;
    } else if (key.is("sequence")) {
      if (kind != Kind::kNumber || !reader.integer(out.sequence) || out.sequence < 0) {
        why = Refusal::kOutOfRange;
        return false;
      }
      said |= Fields::kSequence;
    } else if (key.is("revision")) {
      if (kind != Kind::kNumber || !reader.integer(out.revision) || out.revision < 0) {
        why = Refusal::kOutOfRange;
        return false;
      }
      said |= Fields::kRevision;
    } else if (key.is("sources")) {
      if (kind != Kind::kArray || !reader.begin_array()) {
        why = Refusal::kWrongShape;
        return false;
      }
      Kind element = Kind::kEnd;
      while (reader.element(element)) {
        if (element == Kind::kEnd) break;
        if (element != Kind::kObject) {
          why = Refusal::kWrongShape;
          return false;
        }
        if (out.source_count >= kMaxSources) {
          why = Refusal::kTooMany;
          return false;
        }
        if (!read_source(reader, out.sources[out.source_count], why)) return false;
        ++out.source_count;
      }
      if (reader.failed()) {
        why = Refusal::kMalformed;
        return false;
      }
      said |= Fields::kSources;
    } else if (key.is("stream_id")) {
      if (kind != Kind::kString || !take(reader, out.stream_id, sizeof(out.stream_id)) ||
          !is_stream_id(out.stream_id, std::strlen(out.stream_id))) {
        why = Refusal::kMalformed;
        return false;
      }
      said |= Fields::kStreamId;
    } else if (key.is("source_id")) {
      if (kind != Kind::kString || !take(reader, out.source_id, sizeof(out.source_id)) ||
          !is_name(out.source_id, std::strlen(out.source_id))) {
        why = Refusal::kMalformed;
        return false;
      }
      said |= Fields::kSourceId;
    } else if (key.is("port")) {
      if (kind != Kind::kNumber || !reader.integer(out.port) || out.port < 1024 ||
          out.port > 65535) {
        why = Refusal::kOutOfRange;
        return false;
      }
      said |= Fields::kPort;
    } else if (key.is("token")) {
      if (kind != Kind::kString || !take(reader, out.token, sizeof(out.token)) ||
          !is_token(out.token, std::strlen(out.token))) {
        why = Refusal::kMalformed;
        return false;
      }
      said |= Fields::kToken;
    } else {
      // Not a field of the grammar. It is refused rather than skipped, so that a hub which
      // believes it asked for something never has that request silently dropped.
      why = Refusal::kUnknownField;
      return false;
    }
  }
  if (reader.failed()) {
    why = Refusal::kMalformed;
    return false;
  }
  if ((said & (Fields::kCommandId | Fields::kAction | Fields::kNodeId | Fields::kEpoch)) !=
      (Fields::kCommandId | Fields::kAction | Fields::kNodeId | Fields::kEpoch)) {
    why = Refusal::kMissingField;
    return false;
  }
  if (std::strcmp(out.node_id, node_id) != 0) {
    why = Refusal::kNotForThisNode;
    return false;
  }
  if (is_grant(out.action) && out.capability == Capability::kNone) {
    why = Refusal::kMissingField;
    return false;
  }
  if (out.action == Action::kConfigure) {
    if (out.revision < 1) {
      why = Refusal::kOutOfRange;
      return false;
    }
  } else if (said & kConfigureFields) {
    why = Refusal::kWrongShape;
    return false;
  }
  if (is_media(out.action)) {
    if ((said & Fields::kStreamId) == 0) {
      why = Refusal::kMissingField;
      return false;
    }
    if (is_stop(out.action)) {
      if (said & kTicketFields) {
        why = Refusal::kWrongShape;
        return false;
      }
      return true;
    }
    if (!(out.duration_seconds > 0 && out.duration_seconds <= kMaxStreamSeconds)) {
      why = Refusal::kOutOfRange;
      return false;
    }
    if (is_renewal(out.action)) {
      if (said & kTicketFields) {
        why = Refusal::kWrongShape;
        return false;
      }
      return true;
    }
    if (is_start(out.action) && (said & kTicketFields) != kTicketFields) {
      why = Refusal::kMissingField;
      return false;
    }
  } else if (said & kStreamFields) {
    why = Refusal::kWrongShape;
    return false;
  }
  return true;
}

}  // namespace sentry
