// The firmware's command grammar, against the fixtures the hub and the agent both use.
//
// These are the same files `tests/unit/test_satellite_control.py` reads. A third
// implementation is exactly when a contract earns its keep, or fails to: every message the
// other two accept is accepted here, and every message they refuse is refused here.

#include <cstring>
#include <string>

#include "harness.h"
#include "sentry/command.h"
#include "sentry/json.h"

using sentry::Action;
using sentry::Capability;
using sentry::Command;
using sentry::Kind;
using sentry::parse_command;
using sentry::Reader;
using sentry::Refusal;
using sentry::Span;

namespace {

std::string fixtures(const char* which) {
  return std::string(SENTRY_CONTRACTS_DIR) + "/control/fixtures/" + which + "/";
}

// Read a command the way a node would: addressed to whoever the message says, so that the
// test is about the grammar rather than about which node is running it.
bool accepts(const std::string& document, Command& out, Refusal& why) {
  char addressed[sentry::kMaxNameText] = {};
  Reader reader(document.data(), document.size());
  Span key;
  Kind kind = Kind::kEnd;
  if (reader.begin_object()) {
    while (reader.member(key, kind) && kind != Kind::kEnd) {
      if (key.is("node_id") && kind == Kind::kString) {
        size_t length = 0;
        if (reader.string(addressed, sizeof(addressed) - 1, length)) addressed[length] = '\0';
        break;
      }
      if (!reader.skip()) break;
    }
  }
  return parse_command(document.data(), document.size(), addressed, out, why);
}

const char* kValid[] = {
    "command-grant.json",
    "command-configure.json",
    "command-video-start.json",
    "command-audio-stop.json",
};

const char* kInvalid[] = {
    "command-action-nobody-agreed-on.json",
    "command-configure-numbered-from-zero.json",
    "command-grant-without-a-capability.json",
    "command-stop-carrying-a-token.json",
    "command-stream-to-a-privileged-port.json",
};

}  // namespace

TEST(what_the_hub_accepts_this_firmware_also_accepts) {
  for (const char* name : kValid) {
    std::string document = harness::slurp(fixtures("valid") + name);
    CHECK(!document.empty());
    Command command;
    Refusal why = Refusal::kNone;
    if (!accepts(document, command, why)) {
      std::printf("    %s was refused: %s\n", name, sentry::name_of(why));
      CHECK(false);
    }
  }
}

TEST(what_the_hub_refuses_this_firmware_also_refuses) {
  for (const char* name : kInvalid) {
    std::string document = harness::slurp(fixtures("invalid") + name);
    CHECK(!document.empty());
    Command command;
    Refusal why = Refusal::kNone;
    if (accepts(document, command, why)) {
      std::printf("    %s was accepted\n", name);
      CHECK(false);
    }
    CHECK(why != Refusal::kNone);
  }
}

TEST(a_grant_is_read_field_for_field) {
  std::string document = harness::slurp(fixtures("valid") + "command-grant.json");
  Command command;
  Refusal why = Refusal::kNone;
  CHECK(accepts(document, command, why));
  CHECK(command.action == Action::kGrant);
  CHECK(command.capability == Capability::kEvents);
  CHECK_TEXT(command.node_id, "pico-ingresso");
  CHECK(command.hub_epoch == 7);
  CHECK(command.duration_seconds > 299.9 && command.duration_seconds < 300.1);
  CHECK(command.sequence == 1);
  CHECK(command.source_count == 0);
}

TEST(a_configuration_is_a_list_of_drivers_with_plain_options) {
  std::string document = harness::slurp(fixtures("valid") + "command-configure.json");
  Command command;
  Refusal why = Refusal::kNone;
  CHECK(accepts(document, command, why));
  CHECK(command.action == Action::kConfigure);
  CHECK(command.revision == 4);
  CHECK(command.source_count == 2);
  CHECK_TEXT(command.sources[0].id, "pir-1");
  CHECK_TEXT(command.sources[0].kind, "gpio");
  CHECK(command.sources[0].option_count == 2);
  CHECK_TEXT(command.sources[0].options[0].name, "pin");
  CHECK(command.sources[0].options[0].type == sentry::Option::Type::kInteger);
  CHECK(command.sources[0].options[0].integer == 17);
  CHECK_TEXT(command.sources[1].options[1].name, "pull");
  CHECK(command.sources[1].options[1].type == sentry::Option::Type::kString);
  CHECK_TEXT(command.sources[1].options[1].text, "up");
}

TEST(a_command_addressed_to_another_node_is_not_obeyed_here) {
  std::string document = harness::slurp(fixtures("valid") + "command-grant.json");
  Command command;
  Refusal why = Refusal::kNone;
  CHECK(!parse_command(document.data(), document.size(), "somewhere-else", command, why));
  CHECK(why == Refusal::kNotForThisNode);
}

TEST(a_field_nobody_agreed_on_is_refused_rather_than_skipped) {
  const char* document =
      R"({"command_id":"c-1","action":"stop","node_id":"pico-ingresso","hub_epoch":1,)"
      R"("run":"/bin/sh"})";
  Command command;
  Refusal why = Refusal::kNone;
  CHECK(!parse_command(document, std::strlen(document), "pico-ingresso", command, why));
  CHECK(why == Refusal::kUnknownField);
}

TEST(a_command_that_names_no_action_or_no_sender_is_not_a_command) {
  const char* missing[] = {
      R"({"action":"stop","node_id":"pico-ingresso","hub_epoch":1})",
      R"({"command_id":"c-1","node_id":"pico-ingresso","hub_epoch":1})",
      R"({"command_id":"c-1","action":"stop","hub_epoch":1})",
      R"({"command_id":"c-1","action":"stop","node_id":"pico-ingresso"})",
  };
  for (const char* document : missing) {
    Command command;
    Refusal why = Refusal::kNone;
    CHECK(!parse_command(document, std::strlen(document), "pico-ingresso", command, why));
  }
}

// A configuration of `count` sources, named apart so that nothing is refused for being a
// repeat of something.
std::string with_sources(size_t count) {
  std::string document =
      R"({"command_id":"c-1","action":"configure","node_id":"n","hub_epoch":1,)"
      R"("revision":1,"sources":[)";
  for (size_t index = 0; index < count; ++index) {
    if (index > 0) document += ",";
    document += R"({"id":"pir-)" + std::to_string(index) + R"(","kind":"gpio"})";
  }
  return document + "]}";
}

TEST(more_sources_than_the_node_can_hold_are_refused_not_truncated) {
  // Eight is what this board plans, and the grammar holds exactly that many: a command it
  // could only refuse is refused by size, before fourteen kilobytes are filled in to find
  // out. The contract allows a hub 32, which is for a satellite with a Linux under it.
  Command command;
  Refusal why = Refusal::kNone;
  const std::string eight = with_sources(sentry::kMaxSources);
  CHECK(parse_command(eight.data(), eight.size(), "n", command, why));
  CHECK(command.source_count == sentry::kMaxSources);

  const std::string one_more = with_sources(sentry::kMaxSources + 1);
  CHECK(!parse_command(one_more.data(), one_more.size(), "n", command, why));
  CHECK(why == Refusal::kTooMany);
}

TEST(a_command_is_emptied_without_a_copy_of_one_being_made) {
  // What `clear` is for: the board keeps one command and reads each new one into it, so
  // nothing of the last one may survive. `parse_command` starts with it, which is why a
  // refused command leaves nothing of the one before behind either.
  Command command;
  const std::string eight = with_sources(sentry::kMaxSources);
  Refusal why = Refusal::kNone;
  CHECK(parse_command(eight.data(), eight.size(), "n", command, why));
  CHECK(command.source_count == sentry::kMaxSources);
  sentry::clear(command);
  CHECK(command.source_count == 0);
  CHECK(command.command_id[0] == '\0');
  CHECK(command.action == Action::kUnknown);
  CHECK(command.sources[0].id[0] == '\0');

  const char* nonsense = R"({"command_id":"c-2","action":"configure"})";
  CHECK(!parse_command(nonsense, std::strlen(nonsense), "n", command, why));
  CHECK(command.source_count == 0);
}

TEST(a_stream_command_carries_a_ticket_and_a_stop_does_not) {
  const char* start =
      R"({"command_id":"c-1","action":"video_start","node_id":"n","hub_epoch":1,)"
      R"("stream_id":"s7Kd2Lm9","source_id":"camera-1","port":8555,)"
      R"("token":"Yb3xK9tQ2wR7mN4pL8vZ1cD5fH6jS0aU","duration_seconds":120})";
  Command command;
  Refusal why = Refusal::kNone;
  CHECK(parse_command(start, std::strlen(start), "n", command, why));
  CHECK(command.port == 8555);

  const char* without_a_token =
      R"({"command_id":"c-1","action":"video_start","node_id":"n","hub_epoch":1,)"
      R"("stream_id":"s7Kd2Lm9","source_id":"camera-1","port":8555,"duration_seconds":120})";
  CHECK(!parse_command(without_a_token, std::strlen(without_a_token), "n", command, why));
  CHECK(why == Refusal::kMissingField);

  const char* too_long =
      R"({"command_id":"c-1","action":"video_start","node_id":"n","hub_epoch":1,)"
      R"("stream_id":"s7Kd2Lm9","source_id":"camera-1","port":8555,)"
      R"("token":"Yb3xK9tQ2wR7mN4pL8vZ1cD5fH6jS0aU","duration_seconds":601})";
  CHECK(!parse_command(too_long, std::strlen(too_long), "n", command, why));
  CHECK(why == Refusal::kOutOfRange);
}

TEST(a_node_id_that_is_not_a_name_is_refused_before_it_is_compared) {
  const char* document =
      R"({"command_id":"c-1","action":"stop","node_id":"../etc/passwd","hub_epoch":1})";
  Command command;
  Refusal why = Refusal::kNone;
  CHECK(!parse_command(document, std::strlen(document), "../etc/passwd", command, why));
  CHECK(why == Refusal::kMalformed);
}

int main() { return harness::run_all("command"); }
