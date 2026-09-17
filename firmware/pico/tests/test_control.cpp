// What this node says about itself: its state, its health, and its answers.

#include <cstring>
#include <string>

#include "harness.h"
#include "sentry/control.h"
#include "sentry/topics.h"

using sentry::BoardHealth;
using sentry::Channel;
using sentry::DeclaredOption;
using sentry::DeclaredSource;
using sentry::Health;
using sentry::Outcome;
using sentry::SourceHealth;
using sentry::State;
using sentry::Value;
using sentry::write_ack;
using sentry::write_goodbye;
using sentry::write_health;
using sentry::write_state;

namespace {

State a_state() {
  State state;
  state.node_id = "pico-ingresso";
  state.boot_id = "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611";
  state.connection_id = "9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb";
  state.online = true;
  return state;
}

Health a_health() {
  Health health;
  health.node_id = "pico-ingresso";
  health.boot_id = "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611";
  health.uptime_seconds = 61.0;
  health.clock = sentry::Clock::kUnsynced;
  health.queue.events = 3;
  health.queue.bytes = 512;
  return health;
}

std::string written_state(const State& state) {
  char buffer[2048];
  size_t size = write_state(state, buffer, sizeof(buffer));
  return std::string(buffer, size);
}

}  // namespace

TEST(a_node_says_who_it_is_which_boot_and_which_connection) {
  std::string written = written_state(a_state());
  CHECK(written.find(R"("schema_version":1)") != std::string::npos);
  CHECK(written.find(R"("node_id":"pico-ingresso")") != std::string::npos);
  CHECK(written.find(R"("online":true)") != std::string::npos);
  CHECK(written.find(R"("sources":[])") != std::string::npos);
}

TEST(a_goodbye_fits_in_the_will_the_client_can_actually_send) {
  // lwIP writes the will's topic and payload with 8-bit lengths. A goodbye that did not
  // fit would not be truncated, it would be a CONNECT that fails on a board months later.
  char topic[sentry::kMaxTopicText] = {};
  CHECK(sentry::topic(sentry::kTopicPrefix, "pico-ingresso", Channel::kState, topic,
                      sizeof(topic)));
  size_t topic_bytes = std::strlen(topic);
  char buffer[sentry::kMaxWillBytes];
  size_t size = write_goodbye("pico-ingresso", "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611",
                              "9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb", topic_bytes, buffer,
                              sizeof(buffer));
  CHECK(size > 0);
  CHECK(size + topic_bytes <= sentry::kMaxWillBytes);
  std::string goodbye(buffer, size);
  CHECK(goodbye.find(R"("online":false)") != std::string::npos);
  CHECK(goodbye.find("agent_version") == std::string::npos);  // nothing optional in a will
}

TEST(a_goodbye_that_would_not_fit_is_refused_rather_than_registered) {
  char buffer[sentry::kMaxWillBytes];
  // A topic long enough to push the will past what the client can express.
  CHECK(write_goodbye("pico-ingresso", "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611",
                      "9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb", 120, buffer, sizeof(buffer)) == 0);
}

TEST(the_snapshot_is_the_same_message_with_the_optional_parts_filled_in) {
  DeclaredOption options[] = {
      {"pin", Value::of(static_cast<int64_t>(17))},
      {"zone", Value::of("entrance")},
      {"debounce_ms", Value::of(static_cast<int64_t>(200))},
  };
  DeclaredSource sources[] = {
      {"pir-1", "gpio", true, options, 3},
      {"board-temperature", "board", true, nullptr, 0},
  };
  State state = a_state();
  state.firmware_version = "0.1.0";
  state.profile = "sensor-presence";
  state.config_revision = 3;
  state.sources = sources;
  state.source_count = 2;
  std::string written = written_state(state);
  CHECK(written.find(R"("agent_version":"0.1.0")") != std::string::npos);
  CHECK(written.find(R"("profile":"sensor-presence")") != std::string::npos);
  CHECK(written.find(R"("config_revision":3)") != std::string::npos);
  CHECK(written.find(R"("source_id":"pir-1")") != std::string::npos);
  CHECK(written.find(R"("pin":17)") != std::string::npos);
  CHECK(written.find(R"("zone":"entrance")") != std::string::npos);
  CHECK(written.find(R"("options":{})") != std::string::npos);
}

TEST(a_state_the_hub_would_refuse_is_never_written) {
  State bad_node = a_state();
  bad_node.node_id = "Pico_Ingresso";
  State bad_boot = a_state();
  bad_boot.boot_id = "41";
  State long_profile = a_state();
  long_profile.profile = "a-profile-name-far-longer-than-the-thirty-two-characters-allowed";
  DeclaredSource wrong_driver[] = {{"pir-1", "GPIO", true, nullptr, 0}};
  State bad_driver = a_state();
  bad_driver.sources = wrong_driver;
  bad_driver.source_count = 1;
  DeclaredSource joined[] = {{"pico-ingresso.pir-1", "gpio", true, nullptr, 0}};
  State bad_source = a_state();
  bad_source.sources = joined;
  bad_source.source_count = 1;

  const State cases[] = {bad_node, bad_boot, long_profile, bad_driver, bad_source};
  char buffer[2048];
  for (const State& one : cases) {
    CHECK(write_state(one, buffer, sizeof(buffer)) == 0);
  }
}

TEST(a_board_that_cannot_measure_itself_says_nothing_rather_than_zero) {
  // A zero is a measurement. An RP2040 has no load average, and writing 0.0 for one would
  // put a number on a dashboard that nobody ever took.
  char buffer[2048];
  size_t size = write_health(a_health(), buffer, sizeof(buffer));
  CHECK(size > 0);
  std::string written(buffer, size);
  CHECK(written.find(R"("board":{})") != std::string::npos);
  CHECK(written.find("load1") == std::string::npos);
  CHECK(written.find("memory_available_kb") == std::string::npos);
  CHECK(written.find(R"("clock_status":"unsynced")") != std::string::npos);
  CHECK(written.find(R"("granted":false)") != std::string::npos);
}

TEST(what_the_board_can_measure_it_reports) {
  Health health = a_health();
  health.board.has_uptime = true;
  health.board.uptime_seconds = 61.0;
  health.board.has_temperature = true;
  health.board.temperature_c = 24.5;
  health.board.has_free_heap = true;
  health.board.memory_available_kb = 58;
  SourceHealth sources[] = {
      {"pir-1", 12, "gpio", nullptr},
      {"ds18b20-1", 0, "onewire", "no sensor answered on the bus"},
  };
  health.sources = sources;
  health.source_count = 2;
  char buffer[2048];
  size_t size = write_health(health, buffer, sizeof(buffer));
  CHECK(size > 0);
  std::string written(buffer, size);
  CHECK(written.find(R"("temperature_c":24.5)") != std::string::npos);
  CHECK(written.find(R"("memory_available_kb":58)") != std::string::npos);
  CHECK(written.find(R"("pir-1":{"readings":12,"driver":"gpio"})") != std::string::npos);
  CHECK(written.find(R"("error":"no sensor answered on the bus")") != std::string::npos);
}

TEST(a_counter_that_counts_backwards_is_not_written) {
  Health health = a_health();
  health.queue.drops_count = -1;
  char buffer[2048];
  CHECK(write_health(health, buffer, sizeof(buffer)) == 0);
}

TEST(an_answer_is_about_one_command_and_says_what_became_of_it) {
  char buffer[1024];
  size_t size = write_ack("3f1b7c0e-8d4a-4e2b-9f61-0a5c8d7e4b12", "pico-ingresso",
                          Outcome::kApplied, nullptr, buffer, sizeof(buffer));
  CHECK(size > 0);
  CHECK_TEXT(std::string(buffer, size).c_str(),
             R"({"schema_version":1,"command_id":"3f1b7c0e-8d4a-4e2b-9f61-0a5c8d7e4b12",)"
             R"("node_id":"pico-ingresso","outcome":"applied","detail":null})");

  size = write_ack("c-1", "pico-ingresso", Outcome::kFailed, "pin 17 is already taken by door-1",
                   buffer, sizeof(buffer));
  CHECK(size > 0);
  std::string failed(buffer, size);
  CHECK(failed.find(R"("outcome":"failed")") != std::string::npos);
  CHECK(failed.find(R"("detail":"pin 17 is already taken by door-1")") != std::string::npos);
}

TEST(an_answer_without_a_command_or_with_too_much_to_say_is_not_written) {
  char buffer[1024];
  std::string long_detail(sentry::kMaxDetailText + 1, 'x');
  CHECK(write_ack("", "pico-ingresso", Outcome::kApplied, nullptr, buffer, sizeof(buffer)) == 0);
  CHECK(write_ack("c-1", "Pico", Outcome::kApplied, nullptr, buffer, sizeof(buffer)) == 0);
  CHECK(write_ack("c-1", "pico-ingresso", Outcome::kFailed, long_detail.c_str(), buffer,
                  sizeof(buffer)) == 0);
  char small[16];
  CHECK(write_ack("c-1", "pico-ingresso", Outcome::kApplied, nullptr, small, sizeof(small)) == 0);
}

int main() { return harness::run_all("control"); }
