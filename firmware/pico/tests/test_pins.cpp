// A configuration that would start two drivers on one wire.

#include "harness.h"
#include "sentry/pins.h"

using sentry::Board;
using sentry::PinMap;
using sentry::PinRefusal;

TEST(two_sources_cannot_have_the_same_pin) {
  PinMap pins(Board::kPicoW);
  PinRefusal why = PinRefusal::kNone;
  CHECK(pins.claim(17, "door-1", why));
  CHECK(!pins.claim(17, "pir-1", why));
  CHECK(why == PinRefusal::kTaken);
  CHECK_TEXT(pins.holder(17), "door-1");  // and the answer can say who has it
  CHECK(pins.size() == 1);
}

TEST(a_pin_the_board_needs_for_itself_is_not_offered_to_a_sensor) {
  // Taking one of these does not fail at configuration time on a Pico W. It fails as a
  // node that stops being able to reach the broker, which is much harder to work out.
  PinMap pins(Board::kPicoW);
  PinRefusal why = PinRefusal::kNone;
  for (int gpio : {23, 24, 25, 29}) {
    if (pins.claim(gpio, "pir-1", why)) {
      std::printf("    GPIO %d was handed out\n", gpio);
      CHECK(false);
    }
    CHECK(why == PinRefusal::kReserved);
  }
  CHECK(pins.claim(22, "pir-1", why));
}

TEST(a_number_that_is_not_a_pin_on_this_board_is_refused_as_such) {
  PinMap pins(Board::kPico);
  PinRefusal why = PinRefusal::kNone;
  CHECK(!pins.claim(-1, "pir-1", why));
  CHECK(why == PinRefusal::kOutOfRange);
  CHECK(!pins.claim(30, "pir-1", why));
  CHECK(why == PinRefusal::kOutOfRange);
  CHECK(pins.size() == 0);
}

TEST(a_refused_configuration_leaves_the_map_as_it_was) {
  PinMap pins(Board::kPicoW);
  PinRefusal why = PinRefusal::kNone;
  CHECK(pins.claim(16, "door-1", why));
  CHECK(!pins.claim(23, "pir-1", why));
  CHECK(pins.size() == 1);
  CHECK(pins.holder(23) == nullptr);
  pins.clear();
  CHECK(pins.size() == 0);
  CHECK(pins.holder(16) == nullptr);
}

int main() { return harness::run_all("pins"); }
