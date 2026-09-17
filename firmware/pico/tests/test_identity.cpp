// The name the hub knows, and the identifier that must differ every time.

#include <cstring>
#include <string>

#include "harness.h"
#include "sentry/identity.h"

using sentry::Provisioning;
using sentry::read_provisioning;
using sentry::write_uuid4;

namespace {

bool reads(const char* document, Provisioning& out) {
  return read_provisioning(document, std::strlen(document), out);
}

}  // namespace

TEST(a_provisioned_node_knows_its_name_and_where_the_broker_is) {
  Provisioning found;
  CHECK(reads(R"({"node_id":"pico-ingresso","mqtt_host":"hub.lan","mqtt_port":8883})", found));
  CHECK_TEXT(found.node_id, "pico-ingresso");
  CHECK_TEXT(found.mqtt_host, "hub.lan");
  CHECK(found.mqtt_port == 8883);
}

TEST(the_identity_read_back_is_the_identity_that_was_written) {
  const char* record = R"({"node_id":"pico-ingresso","mqtt_host":"192.168.11.240","mqtt_port":8883})";
  Provisioning first;
  Provisioning again;
  CHECK(reads(record, first));
  CHECK(reads(record, again));
  CHECK(std::strcmp(first.node_id, again.node_id) == 0);
  CHECK(std::strcmp(first.mqtt_host, again.mqtt_host) == 0);
}

TEST(half_an_identity_is_not_an_identity) {
  Provisioning found;
  const char* refused[] = {
      R"({"mqtt_host":"hub.lan","mqtt_port":8883})",             // no name
      R"({"node_id":"pico-ingresso","mqtt_port":8883})",         // nowhere to connect
      R"({"node_id":"pico-ingresso","mqtt_host":"hub.lan"})",    // no port
      R"({"node_id":"Pico_Ingresso","mqtt_host":"hub.lan","mqtt_port":8883})",
      R"({"node_id":"pico-ingresso","mqtt_host":"","mqtt_port":8883})",
      R"({"node_id":"pico-ingresso","mqtt_host":"hub.lan","mqtt_port":0})",
      R"({"node_id":"pico-ingresso","mqtt_host":"hub.lan","mqtt_port":70000})",
      R"({"node_id":"pico-ingresso","mqtt_host":"mqtts://hub.lan/x","mqtt_port":8883})",
      R"({"node_id":"pico-ingresso","mqtt_host":"hub.lan","mqtt_port":"8883"})",
      "{",
  };
  for (const char* one : refused) {
    if (reads(one, found)) {
      std::printf("    %s was accepted\n", one);
      CHECK(false);
    }
  }
}

TEST(a_field_a_later_release_added_does_not_cost_this_one_its_identity) {
  Provisioning found;
  CHECK(reads(
      R"({"node_id":"pico-ingresso","mqtt_host":"hub.lan","mqtt_port":8883,"ntp_host":"hub.lan"})",
      found));
  CHECK_TEXT(found.node_id, "pico-ingresso");
}

TEST(a_boot_id_is_a_uuid_and_a_different_one_every_boot) {
  uint8_t entropy[16];
  for (size_t index = 0; index < sizeof(entropy); ++index) {
    entropy[index] = static_cast<uint8_t>(index * 7 + 1);
  }
  char first[37] = {};
  CHECK(write_uuid4(entropy, first, sizeof(first)));
  CHECK(std::strlen(first) == 36);
  CHECK(first[8] == '-' && first[13] == '-' && first[18] == '-' && first[23] == '-');
  CHECK(first[14] == '4');                                              // version
  CHECK(first[19] == '8' || first[19] == '9' || first[19] == 'a' || first[19] == 'b');

  entropy[0] = static_cast<uint8_t>(entropy[0] + 1);
  char second[37] = {};
  CHECK(write_uuid4(entropy, second, sizeof(second)));
  CHECK(std::strcmp(first, second) != 0);

  char small[16] = {};
  CHECK(!write_uuid4(entropy, small, sizeof(small)));
}

int main() { return harness::run_all("identity"); }
