// What a wire is allowed to mean.

#include "harness.h"
#include "sentry/input.h"

using sentry::DigitalInput;
using sentry::InputConfig;
using sentry::Quality;
using sentry::Report;

namespace {

DigitalInput a_contact(uint32_t debounce_ms = 50, bool active_high = true) {
  DigitalInput input;
  InputConfig config;
  config.debounce_ms = debounce_ms;
  config.active_high = active_high;
  input.configure(config);
  input.rearm(0);
  return input;
}

}  // namespace

TEST(a_contact_that_was_open_at_boot_did_not_just_open) {
  // The first reading is what the door is, not something that happened. A hub told
  // otherwise would raise an intrusion every time the node restarted.
  DigitalInput input = a_contact();
  CHECK(input.sample(true, 100) == Report::kBaseline);
  CHECK(input.state());
  CHECK(input.sample(true, 200) == Report::kNothing);
}

TEST(a_level_has_to_hold_still_before_it_is_believed) {
  DigitalInput input = a_contact(50);
  CHECK(input.sample(false, 0) == Report::kBaseline);
  CHECK(input.sample(true, 100) == Report::kNothing);   // it has only just changed
  CHECK(input.sample(false, 120) == Report::kNothing);  // and changed back: bounce
  CHECK(input.sample(true, 140) == Report::kNothing);
  CHECK(input.sample(true, 180) == Report::kNothing);   // 40 ms of the 50 it needs
  CHECK(input.sample(true, 200) == Report::kTransition);
  CHECK(input.state());
}

TEST(polarity_is_configured_because_software_cannot_guess_the_wiring) {
  // A contact to ground with a pull-up reads low when it is closed.
  DigitalInput input = a_contact(0, false);
  CHECK(input.sample(false, 0) == Report::kBaseline);
  CHECK(input.state());  // low means closed here
  CHECK(input.sample(true, 100) == Report::kTransition);
  CHECK(!input.state());
}

TEST(a_pir_that_is_still_warming_up_reports_nothing_it_would_have_to_take_back) {
  DigitalInput pir;
  InputConfig config;
  config.debounce_ms = 0;
  config.settle_ms = 60000;
  CHECK(pir.configure(config));
  pir.rearm(0);

  CHECK(pir.sample(false, 0) == Report::kBaseline);
  CHECK(pir.quality(0) == Quality::kUnknown);  // not "no motion": nothing is known yet
  CHECK(pir.sample(true, 1000) == Report::kNothing);
  CHECK(pir.sample(false, 2000) == Report::kNothing);
  CHECK(!pir.settled(59999));
  CHECK(pir.settled(60000));
  CHECK(pir.quality(60000) == Quality::kValid);
  CHECK(pir.sample(true, 61000) == Report::kTransition);
}

TEST(a_grant_or_a_reconfiguration_starts_the_story_again) {
  // The hub knows nothing after a grant, so what it is owed is a baseline, not silence
  // because the level happens not to have changed since before it was listening.
  DigitalInput input = a_contact();
  CHECK(input.sample(true, 0) == Report::kBaseline);
  CHECK(input.sample(true, 500) == Report::kNothing);
  input.rearm(1000);
  CHECK(input.sample(true, 1000) == Report::kBaseline);
}

TEST(a_configuration_outside_the_limits_is_refused_and_changes_nothing) {
  DigitalInput input;
  InputConfig sane;
  sane.debounce_ms = 40;
  CHECK(input.configure(sane));
  InputConfig absurd;
  absurd.debounce_ms = sentry::kMaxDebounceMs + 1;
  CHECK(!input.configure(absurd));
  InputConfig forever;
  forever.settle_ms = sentry::kMaxSettleMs + 1;
  CHECK(!input.configure(forever));

  input.rearm(0);
  CHECK(input.sample(false, 0) == Report::kBaseline);
  CHECK(input.sample(true, 10) == Report::kNothing);
  CHECK(input.sample(true, 50) == Report::kTransition);  // still the 40 ms that was accepted
}

int main() { return harness::run_all("input"); }
