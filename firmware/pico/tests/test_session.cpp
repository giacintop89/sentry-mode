// The order things have to happen in before anything this node says means anything.

#include "harness.h"
#include "sentry/session.h"

using sentry::Link;
using sentry::Session;

namespace {
const char* kFirst = "9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb";
const char* kSecond = "3f1b7c0e-8d4a-4e2b-9f61-0a5c8d7e4b12";
}  // namespace

TEST(nothing_is_published_before_the_broker_has_accepted_the_connection) {
  Session session;
  CHECK(session.link() == Link::kOffline);
  CHECK(!session.may_publish_events());
  CHECK(!session.subscribed());  // no connection to subscribe on
  CHECK(!session.announced());
  CHECK(session.connection_id() == nullptr);
}

TEST(a_node_waits_for_its_subscription_before_saying_it_is_here) {
  // The MCU client connects with a clean session: a command sent before the SUBACK is
  // gone, and the first grant is the one that matters.
  Session session;
  CHECK(session.connected(kFirst));
  CHECK(session.link() == Link::kSubscribing);
  CHECK(!session.ready_to_announce());
  CHECK(!session.announced());
  CHECK(!session.may_publish_events());
  CHECK(session.subscribed());
  CHECK(session.ready_to_announce());
  CHECK(!session.may_publish_events());  // the hub has not been told yet
  CHECK(session.announced());
  CHECK(session.may_publish_events());
}

TEST(every_connection_gets_an_identifier_of_its_own) {
  // The will the broker holds names the connection it was registered for. Two connections
  // with one identifier would make an old goodbye indistinguishable from being gone now.
  Session session;
  CHECK(session.connected(kFirst));
  session.dropped();
  CHECK(!session.connected(kFirst));
  CHECK(session.link() == Link::kOffline);
  CHECK(session.connected(kSecond));
  CHECK(session.connections() == 2);
}

TEST(a_connection_that_is_not_a_uuid_is_not_a_connection) {
  Session session;
  CHECK(!session.connected("41"));
  CHECK(!session.connected(nullptr));
  CHECK(session.link() == Link::kOffline);
}

TEST(a_dropped_link_stops_everything_until_it_is_back) {
  Session session;
  session.connected(kFirst);
  session.subscribed();
  session.announced();
  CHECK(session.may_publish_events());
  session.dropped();
  CHECK(!session.may_publish_events());
  CHECK(session.connection_id() == nullptr);
  CHECK(!session.announced());  // and it starts again from the beginning
  CHECK(session.connected(kSecond));
  CHECK(!session.may_publish_events());
  CHECK(session.subscribed() && session.announced());
  CHECK(session.may_publish_events());
}

TEST(a_second_connect_while_connected_is_refused) {
  Session session;
  CHECK(session.connected(kFirst));
  CHECK(!session.connected(kSecond));
  CHECK(session.connections() == 1);
}

int main() { return harness::run_all("session"); }
