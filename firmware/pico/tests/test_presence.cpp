// Whether one device is here, and everything this node refuses to conclude from a radio.
//
// The rules under test are the Linux agent's, deliberately: a hub joining an arrival to a
// rule must not be able to tell which kind of node heard it. What is mostly being tested
// is the refusals — one packet is not an arrival, a scanner that stopped is not an empty
// room, and an advertisement that is not an iBeacon is not made into one.

#include <cstring>
#include <string>

#include "harness.h"
#include "sentry/presence.h"

using sentry::Beacon;
using sentry::is_the_one;
using sentry::name_of;
using sentry::Presence;
using sentry::read_address;
using sentry::read_beacon;
using sentry::read_uuid;
using sentry::Watch;
using sentry::Watched;
using sentry::Watchfulness;
using sentry::write_address;

namespace {

Watch a_watch(uint32_t sightings = 3, uint32_t window_ms = 10000,
              uint32_t absent_ms = 120000, int32_t rssi_min = -100) {
  Watch watch;
  Watchfulness how;
  how.enter_sightings = sightings;
  how.enter_window_ms = window_ms;
  how.absent_after_ms = absent_ms;
  how.rssi_min = rssi_min;
  watch.configure(how);  // every set of numbers here is one a configuration may ask for
  return watch;
}

// An advertisement with one iBeacon in it, and a name before it the way a phone sends one.
size_t an_ibeacon(uint8_t* out, const uint8_t uuid[16], uint16_t major, uint16_t minor) {
  size_t at = 0;
  out[at++] = 3;  // a shortened local name, which is not what is being looked for
  out[at++] = 0x08;
  out[at++] = 'h';
  out[at++] = 'i';
  out[at++] = 26;
  out[at++] = 0xff;
  out[at++] = 0x4c;
  out[at++] = 0x00;
  out[at++] = 0x02;
  out[at++] = 0x15;
  std::memcpy(out + at, uuid, 16);
  at += 16;
  out[at++] = static_cast<uint8_t>(major >> 8);
  out[at++] = static_cast<uint8_t>(major & 0xff);
  out[at++] = static_cast<uint8_t>(minor >> 8);
  out[at++] = static_cast<uint8_t>(minor & 0xff);
  out[at++] = 0xc5;  // the transmit power, which this node has no use for
  return at;
}

}  // namespace

TEST(one_sighting_is_not_an_arrival) {
  Watch watch = a_watch();
  CHECK(!watch.covered(true, 1000));
  CHECK(watch.state() == Presence::kUnknown);
  CHECK(!watch.seen(1000, -60, true));
  CHECK(!watch.seen(2200, -61, true));
  CHECK(watch.state() == Presence::kUnknown);
  CHECK(watch.seen(3400, -59, true));
  CHECK(watch.state() == Presence::kPresent);
  // The first state after unknown is where things stand, not something that happened.
  CHECK(watch.is_baseline());
}

TEST(a_beacon_that_shouts_ten_times_a_second_is_not_ten_times_as_present) {
  Watch watch = a_watch();
  CHECK(!watch.covered(true, 0));
  for (uint64_t at = 0; at < 900; at += 100) {
    CHECK(!watch.seen(at, -50, true));
  }
  CHECK(watch.state() == Presence::kUnknown);
  CHECK(watch.sightings() == 9);  // heard nine times, counted once
}

TEST(sightings_too_far_apart_never_add_up_to_an_arrival) {
  Watch watch = a_watch(3, 10000);
  CHECK(!watch.covered(true, 0));
  CHECK(!watch.seen(0, -50, true));
  CHECK(!watch.seen(6000, -50, true));
  CHECK(!watch.seen(12000, -50, true));  // the first one has fallen out of the window
  CHECK(watch.state() == Presence::kUnknown);
  CHECK(watch.seen(13500, -50, true));
  CHECK(watch.state() == Presence::kPresent);
}

TEST(a_signal_too_weak_to_be_believed_is_counted_and_not_acted_on) {
  Watch watch = a_watch(2, 10000, 120000, -70);
  CHECK(!watch.covered(true, 0));
  CHECK(!watch.seen(0, -80, true));
  CHECK(!watch.seen(1500, -90, true));
  CHECK(watch.state() == Presence::kUnknown);
  CHECK(watch.too_weak() == 2);
  CHECK(watch.sightings() == 0);
  CHECK(!watch.has_rssi());
  // And one that is strong enough is believed, with the two before it still counted.
  CHECK(!watch.seen(3000, -60, true));
  CHECK(watch.seen(4500, -60, true));
  CHECK(watch.state() == Presence::kPresent);
  CHECK(watch.too_weak() == 2);
}

TEST(a_long_quiet_while_the_radio_was_listening_is_absence) {
  Watch watch = a_watch(1, 10000, 60000);
  CHECK(!watch.covered(true, 0));
  CHECK(watch.seen(1000, -50, true));
  CHECK(watch.state() == Presence::kPresent);
  CHECK(!watch.quiet(30000));
  CHECK(watch.state() == Presence::kPresent);
  CHECK(watch.quiet(61000));
  CHECK(watch.state() == Presence::kAbsent);
  CHECK(!watch.is_baseline());  // it was present, and then it left
  CHECK(!watch.quiet(200000));  // and it stays absent, saying so once
}

TEST(a_scanner_that_stopped_is_not_an_empty_room) {
  Watch watch = a_watch(1, 10000, 60000);
  CHECK(!watch.covered(true, 0));
  CHECK(watch.seen(1000, -50, true));
  CHECK(watch.state() == Presence::kPresent);

  // The radio goes away. That is unknown at once, and not absence however long it lasts.
  CHECK(watch.covered(false, 2000));
  CHECK(watch.state() == Presence::kUnknown);
  CHECK(!watch.quiet(500000));
  CHECK(watch.state() == Presence::kUnknown);
  CHECK(!watch.seen(300000, -50, true));  // nothing is heard while nothing is listening

  // And when it comes back, the wait for absence starts again from there.
  CHECK(!watch.covered(true, 600000));
  CHECK(!watch.quiet(650000));
  CHECK(watch.state() == Presence::kUnknown);
  CHECK(watch.quiet(661000));
  CHECK(watch.state() == Presence::kAbsent);
  CHECK(watch.is_baseline());  // reached from unknown: where things stand
}

TEST(a_watchfulness_nobody_could_survive_is_refused) {
  Watch watch;
  Watchfulness how;
  how.enter_sightings = 0;
  CHECK(!watch.configure(how));
  how.enter_sightings = 100;
  CHECK(!watch.configure(how));
  how.enter_sightings = 3;
  how.enter_window_ms = 500;
  CHECK(!watch.configure(how));
  how.enter_window_ms = 10000;
  how.absent_after_ms = 1000;
  CHECK(!watch.configure(how));
  how.absent_after_ms = 120000;
  how.rssi_min = 40;
  CHECK(!watch.configure(how));
  how.rssi_min = -100;
  CHECK(watch.configure(how));
}

TEST(a_device_is_watched_by_its_address_or_by_its_beacon_and_not_by_neither) {
  Watched nobody;
  CHECK(!nobody.named());
  const uint8_t address[6] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF};
  CHECK(!is_the_one(nobody, address, nullptr, 0));

  Watched both;
  both.by_address = true;
  both.by_beacon = true;
  CHECK(!both.named());  // two claims about what is being watched are not one device
}

TEST(an_address_is_read_and_written_the_way_it_is_printed) {
  uint8_t address[6] = {};
  CHECK(read_address("AA:BB:CC:DD:EE:FF", address));
  CHECK(address[0] == 0xAA && address[5] == 0xFF);
  char back[18] = {};
  CHECK(write_address(address, back, sizeof(back)));
  CHECK(std::strcmp(back, "AA:BB:CC:DD:EE:FF") == 0);

  const char* wrong[] = {"aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "AA:BB:CC:DD:EE",
                         "AA:BB:CC:DD:EE:FG", "", "AA:BB:CC:DD:EE:FF:00"};
  for (const char* one : wrong) CHECK(!read_address(one, address));
}

TEST(a_uuid_is_read_in_the_case_a_configuration_writes_it_in) {
  uint8_t uuid[16] = {};
  CHECK(read_uuid("f7826da6-4fa2-4e98-8024-bc5b71e0893e", uuid));
  CHECK(uuid[0] == 0xf7 && uuid[15] == 0x3e);
  const char* wrong[] = {"F7826DA6-4FA2-4E98-8024-BC5B71E0893E",
                         "f7826da64fa24e988024bc5b71e0893e", "f7826da6-4fa2-4e98-8024",
                         "f7826da6-4fa2-4e98-8024-bc5b71e0893z", ""};
  for (const char* one : wrong) CHECK(!read_uuid(one, uuid));
}

TEST(an_ibeacon_is_read_out_of_an_advertisement_that_has_one) {
  uint8_t uuid[16] = {};
  CHECK(read_uuid("f7826da6-4fa2-4e98-8024-bc5b71e0893e", uuid));
  uint8_t payload[40] = {};
  const size_t size = an_ibeacon(payload, uuid, 11, 4242);

  Beacon found;
  CHECK(read_beacon(payload, size, found));
  CHECK(std::memcmp(found.uuid, uuid, 16) == 0);
  CHECK(found.major == 11);
  CHECK(found.minor == 4242);

  Watched watched;
  watched.by_beacon = true;
  std::memcpy(watched.uuid, uuid, 16);
  CHECK(is_the_one(watched, nullptr, payload, size));
  watched.major = 11;
  CHECK(is_the_one(watched, nullptr, payload, size));
  watched.minor = 7;
  CHECK(!is_the_one(watched, nullptr, payload, size));  // the right beacon, the wrong room
}

TEST(an_advertisement_that_is_not_a_beacon_is_not_made_into_one) {
  Beacon found;
  // A name and nothing else.
  const uint8_t named[] = {3, 0x08, 'h', 'i'};
  CHECK(!read_beacon(named, sizeof(named), found));
  // Manufacturer data from somebody who is not Apple, of the right length.
  uint8_t other[30] = {};
  other[0] = 26;
  other[1] = 0xff;
  other[2] = 0x99;
  other[3] = 0x00;
  CHECK(!read_beacon(other, 27, found));
  // A length that runs off the end of what was received: truncated is not evidence.
  const uint8_t torn[] = {26, 0xff, 0x4c, 0x00, 0x02, 0x15};
  CHECK(!read_beacon(torn, sizeof(torn), found));
  // Nothing at all.
  CHECK(!read_beacon(nullptr, 0, found));
  const uint8_t empty[] = {0};
  CHECK(!read_beacon(empty, sizeof(empty), found));
}

TEST(an_address_that_is_not_the_one_being_watched_is_somebody_elses_phone) {
  Watched watched;
  watched.by_address = true;
  CHECK(read_address("AA:BB:CC:DD:EE:FF", watched.address));
  const uint8_t theirs[6] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01};
  const uint8_t ours[6] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF};
  CHECK(!is_the_one(watched, theirs, nullptr, 0));
  CHECK(is_the_one(watched, ours, nullptr, 0));
}

TEST(every_state_has_a_name) {
  CHECK(std::strcmp(name_of(Presence::kUnknown), "unknown") == 0);
  CHECK(std::strcmp(name_of(Presence::kPresent), "present") == 0);
  CHECK(std::strcmp(name_of(Presence::kAbsent), "absent") == 0);
}

TEST(a_window_too_short_for_the_sightings_it_asks_for_is_refused) {
  Watch watch;
  Watchfulness impossible;
  impossible.enter_sightings = 5;
  impossible.enter_window_ms = 4000;  // four gaps of a second fit in four seconds, just
  CHECK(!watch.configure(impossible));
  impossible.enter_window_ms = 4001;
  CHECK(watch.configure(impossible));

  // One sighting needs no gap at all, so the shortest window there is will do.
  Watchfulness eager;
  eager.enter_sightings = 1;
  eager.enter_window_ms = sentry::kMinEnterWindowMs;
  CHECK(watch.configure(eager));
}

TEST(a_uuid_goes_back_out_the_way_it_came_in) {
  uint8_t uuid[16] = {};
  CHECK(sentry::read_uuid("f7826da6-4fa2-4e98-8024-bc5b71e0893e", uuid));
  char text[40] = {};
  CHECK(sentry::write_uuid(uuid, text, sizeof(text)));
  CHECK(std::strcmp(text, "f7826da6-4fa2-4e98-8024-bc5b71e0893e") == 0);
  // Room for the hyphens and the ending, and not one byte less.
  char cramped[36] = {};
  CHECK(!sentry::write_uuid(uuid, cramped, sizeof(cramped)));
}

int main() { return harness::run_all("presence"); }
