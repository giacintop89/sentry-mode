// The one claim in the protocol a node cannot make up.

#include <cstring>

#include "harness.h"
#include "sentry/topics.h"

using sentry::Channel;
using sentry::topic;

TEST(each_channel_has_its_place_under_this_nodes_name) {
  char out[sentry::kMaxTopicText] = {};
  CHECK(topic(sentry::kTopicPrefix, "pico-ingresso", Channel::kEvents, out, sizeof(out)));
  CHECK_TEXT(out, "sentry/v1/nodes/pico-ingresso/events");
  CHECK(topic(sentry::kTopicPrefix, "pico-ingresso", Channel::kCommands, out, sizeof(out)));
  CHECK_TEXT(out, "sentry/v1/nodes/pico-ingresso/commands");
  CHECK(topic(sentry::kTopicPrefix, "pico-ingresso", Channel::kState, out, sizeof(out)));
  CHECK_TEXT(out, "sentry/v1/nodes/pico-ingresso/state");
  CHECK(topic(sentry::kTopicPrefix, "pico-ingresso", Channel::kHealth, out, sizeof(out)));
  CHECK_TEXT(out, "sentry/v1/nodes/pico-ingresso/health");
  CHECK(topic(sentry::kTopicPrefix, "pico-ingresso", Channel::kAcks, out, sizeof(out)));
  CHECK_TEXT(out, "sentry/v1/nodes/pico-ingresso/acks");
}

TEST(a_node_cannot_write_itself_a_topic_that_belongs_to_somebody_else) {
  char out[sentry::kMaxTopicText] = {};
  const char* names[] = {"pico/../zero-w", "pico+", "#", "", "Pico", "pico ingresso"};
  for (const char* name : names) {
    if (topic(sentry::kTopicPrefix, name, Channel::kEvents, out, sizeof(out))) {
      std::printf("    a topic was built for %s\n", name);
      CHECK(false);
    }
  }
  const char* prefixes[] = {"sentry//v1", "/sentry/v1", "sentry/v1/", "Sentry/v1", "sentry/+"};
  for (const char* prefix : prefixes) {
    if (topic(prefix, "pico-ingresso", Channel::kEvents, out, sizeof(out))) {
      std::printf("    a topic was built under %s\n", prefix);
      CHECK(false);
    }
  }
}

TEST(a_topic_that_does_not_fit_is_not_written_in_part) {
  char out[16];
  std::memset(out, 'x', sizeof(out));
  CHECK(!topic(sentry::kTopicPrefix, "pico-ingresso", Channel::kEvents, out, sizeof(out)));
  CHECK(out[0] == 'x');
}

int main() { return harness::run_all("topics"); }
