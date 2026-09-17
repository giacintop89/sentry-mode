// What a configuration is allowed to ask this board for, and what it is told when it is not.
//
// Every refusal here is a configuration that would otherwise have started something: a
// driver this firmware does not have, two sources on one wire, a pin that belongs to the
// radio, a debounce nobody could wait for. They are refused whole, before a pin is touched,
// and each one says which source it was.

#include <cstring>
#include <string>

#include "harness.h"
#include "sentry/plan.h"
#include "sentry/sensors.h"

using sentry::Bias;
using sentry::Board;
using sentry::Driver;
using sentry::Option;
using sentry::Plan;
using sentry::Source;
using sentry::Unplanned;

namespace {

Source a_source(const char* id, const char* kind) {
  Source source;
  std::strncpy(source.id, id, sizeof(source.id) - 1);
  std::strncpy(source.kind, kind, sizeof(source.kind) - 1);
  return source;
}

void with_integer(Source& source, const char* name, int64_t value) {
  Option& option = source.options[source.option_count++];
  std::strncpy(option.name, name, sizeof(option.name) - 1);
  option.type = Option::Type::kInteger;
  option.integer = value;
}

void with_number(Source& source, const char* name, double value) {
  Option& option = source.options[source.option_count++];
  std::strncpy(option.name, name, sizeof(option.name) - 1);
  option.type = Option::Type::kNumber;
  option.number = value;
}

void with_text(Source& source, const char* name, const char* value) {
  Option& option = source.options[source.option_count++];
  std::strncpy(option.name, name, sizeof(option.name) - 1);
  option.type = Option::Type::kString;
  std::strncpy(option.text, value, sizeof(option.text) - 1);
}

void with_boolean(Source& source, const char* name, bool value) {
  Option& option = source.options[source.option_count++];
  std::strncpy(option.name, name, sizeof(option.name) - 1);
  option.type = Option::Type::kBoolean;
  option.boolean = value;
}

struct Attempt {
  Unplanned why = Unplanned::kNone;
  char detail[sentry::kMaxPlanDetail] = {};

  bool of(Plan& plan, const Source* sources, size_t count) {
    return plan.take(sources, count, why, detail, sizeof(detail));
  }

  bool said(const char* fragment) const { return std::strstr(detail, fragment) != nullptr; }
};

}  // namespace

TEST(a_pir_and_a_contact_on_two_pins_are_a_plan) {
  Source sources[2];
  sources[0] = a_source("pir-1", "gpio");
  with_integer(sources[0], "pin", 15);
  with_text(sources[0], "bias", "pull_down");
  with_number(sources[0], "settle_seconds", 30.0);
  with_text(sources[0], "event_kind", "sensor.motion");

  sources[1] = a_source("door-1", "gpio");
  with_integer(sources[1], "pin", 16);
  with_boolean(sources[1], "active_high", false);
  with_text(sources[1], "bias", "pull_up");
  with_integer(sources[1], "debounce_ms", 20);
  with_text(sources[1], "event_kind", "sensor.contact");

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(attempt.of(plan, sources, 2));
  CHECK(plan.size() == 2);

  const sentry::Planned& pir = plan.at(0);
  CHECK(std::strcmp(pir.source_id, "pir-1") == 0);
  CHECK(pir.driver == Driver::kGpio);
  CHECK(pir.gpio.pin == 15);
  CHECK(pir.gpio.bias == Bias::kPullDown);
  CHECK(pir.gpio.input.active_high);
  CHECK(pir.gpio.input.settle_ms == 30000);
  CHECK(pir.gpio.input.debounce_ms == 50);  // the default, which nothing said otherwise
  CHECK(std::strcmp(pir.gpio.event_kind, "sensor.motion") == 0);

  const sentry::Planned& door = plan.at(1);
  CHECK(door.gpio.pin == 16);
  CHECK(!door.gpio.input.active_high);  // wired to ground: closed reads low
  CHECK(door.gpio.bias == Bias::kPullUp);
  CHECK(door.gpio.input.debounce_ms == 20);
  CHECK(std::strcmp(door.gpio.event_kind, "sensor.contact") == 0);

  CHECK(std::strcmp(plan.holder(15), "pir-1") == 0);
  CHECK(plan.holder(17) == nullptr);
}

TEST(the_board_measures_its_own_temperature_and_says_how_often) {
  Source source = a_source("board-temperature", "board");
  with_text(source, "measure", "temperature");
  with_integer(source, "interval_seconds", 60);

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(attempt.of(plan, &source, 1));
  CHECK(plan.at(0).driver == Driver::kBoard);
  CHECK(plan.at(0).board.interval_ms == 60000);
  CHECK(std::strcmp(plan.at(0).board.measure, "temperature") == 0);
}

TEST(a_board_with_no_options_is_the_default_one_rather_than_a_refusal) {
  Source source = a_source("board-temperature", "board");
  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(attempt.of(plan, &source, 1));
  CHECK(plan.at(0).board.interval_ms == sentry::kDefaultBoardSeconds * 1000);
  CHECK(std::strcmp(plan.at(0).board.measure, "temperature") == 0);
}

TEST(two_sources_on_one_wire_are_refused_whole) {
  Source sources[2];
  sources[0] = a_source("pir-1", "gpio");
  with_integer(sources[0], "pin", 15);
  sources[1] = a_source("door-1", "gpio");
  with_integer(sources[1], "pin", 15);

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, sources, 2));
  CHECK(attempt.why == Unplanned::kPinRefused);
  CHECK(attempt.said("door-1"));
  CHECK(attempt.said("pir-1"));
  CHECK(attempt.said("15"));
  // Nothing of it survives: the first source was fine and is still not running.
  CHECK(plan.size() == 0);
  CHECK(plan.holder(15) == nullptr);
}

TEST(a_pin_the_radio_is_on_is_not_a_pin_this_configuration_may_have) {
  // GPIO 23 on a Pico W is wired to the wireless chip. Taking it does not fail here; it
  // fails as a node that stops being able to reach the broker, which is worse.
  Source source = a_source("pir-1", "gpio");
  with_integer(source, "pin", 23);

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, &source, 1));
  CHECK(attempt.why == Unplanned::kPinRefused);
  CHECK(attempt.said("belongs to the board"));
}

TEST(a_driver_this_firmware_does_not_have_is_said_so_by_name) {
  Source source = a_source("weather-1", "bme280");
  with_integer(source, "bus", 1);

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, &source, 1));
  CHECK(attempt.why == Unplanned::kNoSuchDriver);
  CHECK(attempt.said("bme280"));
}

TEST(a_probe_named_the_way_the_other_satellite_names_it_is_the_same_probe) {
  // The kernel calls it 28-0123456789ab and prints the serial the other way round from the
  // order the bus wants it in. A MATCH ROM built from the printed order addresses nobody.
  Source source = a_source("thermometer-1", "onewire");
  with_integer(source, "pin", 2);
  with_text(source, "device", "28-0123456789ab");
  with_integer(source, "interval_seconds", 60);

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(attempt.of(plan, &source, 1));
  const sentry::Planned& probe = plan.at(0);
  CHECK(probe.driver == Driver::kOneWire);
  CHECK(probe.onewire.pin == 2);
  CHECK(probe.onewire.has_rom);
  CHECK(probe.onewire.rom[0] == 0x28);
  CHECK(probe.onewire.rom[1] == 0xab);
  CHECK(probe.onewire.rom[6] == 0x01);
  CHECK(probe.onewire.rom[7] == sentry::onewire_crc(probe.onewire.rom, 7));
  CHECK(probe.onewire.interval_ms == 60000);
  CHECK(std::strcmp(probe.onewire.event_kind, "climate.temperature") == 0);
  CHECK(std::strcmp(plan.holder(2), "thermometer-1") == 0);
}

TEST(a_bus_with_one_probe_on_it_does_not_have_to_name_it) {
  Source source = a_source("thermometer-1", "onewire");
  with_integer(source, "pin", 2);
  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(attempt.of(plan, &source, 1));
  CHECK(!plan.at(0).onewire.has_rom);
}

TEST(a_probe_id_that_is_not_one_is_refused_with_what_one_looks_like) {
  Source source = a_source("thermometer-1", "onewire");
  with_integer(source, "pin", 2);
  with_text(source, "device", "10-0123456789ab");  // a DS18S20, which this does not read

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, &source, 1));
  CHECK(attempt.why == Unplanned::kOutOfRange);
  CHECK(attempt.said("28-0123456789ab"));
}

TEST(an_option_no_driver_here_has_is_refused_rather_than_ignored) {
  // The Linux agent's gpio driver takes a chip and a line. A configuration written for it
  // and sent here is not half applicable: the pin it means is not the pin this would use.
  Source source = a_source("pir-1", "gpio");
  with_integer(source, "pin", 15);
  with_text(source, "chip", "/dev/gpiochip0");

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, &source, 1));
  CHECK(attempt.why == Unplanned::kNoSuchOption);
  CHECK(attempt.said("chip"));
}

TEST(a_gpio_source_without_a_pin_is_not_a_gpio_source) {
  Source source = a_source("pir-1", "gpio");
  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, &source, 1));
  CHECK(attempt.why == Unplanned::kMissingOption);
  CHECK(attempt.said("pir-1"));
}

TEST(numbers_outside_what_this_node_keeps_are_refused_with_the_limit) {
  Source source = a_source("door-1", "gpio");
  with_integer(source, "pin", 16);
  with_integer(source, "debounce_ms", 60000);

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, &source, 1));
  CHECK(attempt.why == Unplanned::kOutOfRange);
  CHECK(attempt.said("5000"));

  Source interval = a_source("board-temperature", "board");
  with_integer(interval, "interval_seconds", 0);
  CHECK(!attempt.of(plan, &interval, 1));
  CHECK(attempt.why == Unplanned::kOutOfRange);
}

TEST(a_number_written_as_a_decimal_is_still_that_number_and_a_half_is_not) {
  Source source = a_source("board-temperature", "board");
  with_number(source, "interval_seconds", 45.0);
  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(attempt.of(plan, &source, 1));
  CHECK(plan.at(0).board.interval_ms == 45000);

  Source half = a_source("board-temperature", "board");
  with_number(half, "interval_seconds", 45.5);
  CHECK(!attempt.of(plan, &half, 1));
  CHECK(attempt.why == Unplanned::kWrongType);
}

TEST(the_same_name_twice_is_a_configuration_nobody_could_read_back) {
  Source sources[2];
  sources[0] = a_source("pir-1", "gpio");
  with_integer(sources[0], "pin", 15);
  sources[1] = a_source("pir-1", "gpio");
  with_integer(sources[1], "pin", 16);

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, sources, 2));
  CHECK(attempt.why == Unplanned::kRepeatedId);
  CHECK(attempt.said("twice"));
}

TEST(more_sources_than_this_board_will_run_are_refused_before_any_of_them) {
  Source sources[sentry::kMaxPlanned + 1];
  for (size_t index = 0; index < sentry::kMaxPlanned + 1; ++index) {
    const std::string name = "pir-" + std::to_string(index);
    sources[index] = a_source(name.c_str(), "gpio");
    with_integer(sources[index], "pin", static_cast<int64_t>(index));
  }
  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, sources, sentry::kMaxPlanned + 1));
  CHECK(attempt.why == Unplanned::kTooMany);
  CHECK(plan.size() == 0);
}

TEST(an_empty_configuration_is_a_node_with_nothing_on_it_and_that_is_allowed) {
  // A node may be told to stop reporting from everything without being revoked: it is
  // still there, still answering, and has nothing to say.
  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(attempt.of(plan, nullptr, 0));
  CHECK(plan.size() == 0);
  CHECK(attempt.why == Unplanned::kNone);
}

TEST(a_bias_nobody_can_apply_is_refused_by_name) {
  Source source = a_source("door-1", "gpio");
  with_integer(source, "pin", 16);
  with_text(source, "bias", "pull_sideways");

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, &source, 1));
  CHECK(attempt.why == Unplanned::kOutOfRange);
  CHECK(attempt.said("pull_sideways"));
}

TEST(an_event_kind_that_is_not_one_is_refused) {
  Source source = a_source("door-1", "gpio");
  with_integer(source, "pin", 16);
  with_text(source, "event_kind", "Door Opened");

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, &source, 1));
  CHECK(attempt.why == Unplanned::kWrongType);
}

TEST(a_light_sensor_is_a_pin_with_a_converter_behind_it) {
  Source source = a_source("light-1", "adc");
  with_integer(source, "pin", 26);
  with_text(source, "output", "volts");
  with_integer(source, "interval_seconds", 5);
  with_text(source, "event_kind", "light.level");

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(attempt.of(plan, &source, 1));
  CHECK(plan.at(0).driver == Driver::kAdc);
  CHECK(plan.at(0).adc.pin == 26);
  CHECK(plan.at(0).adc.volts);
  CHECK(plan.at(0).adc.interval_ms == 5000);
  CHECK(std::strcmp(plan.at(0).adc.event_kind, "light.level") == 0);
  CHECK(std::strcmp(plan.holder(26), "light-1") == 0);
}

TEST(a_pin_with_no_converter_behind_it_is_refused_as_that_and_not_as_a_missing_pin) {
  // GPIO 15 is a pin on this board and a perfectly good one; it just has no converter on
  // it. Saying "not a pin" would send somebody looking at the wrong thing.
  Source source = a_source("light-1", "adc");
  with_integer(source, "pin", 15);

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, &source, 1));
  CHECK(attempt.why == Unplanned::kOutOfRange);
  CHECK(attempt.said("converter"));
}

TEST(an_adc_source_written_for_the_other_satellite_is_refused_by_name) {
  // The Linux agent reads its analogue values through an ADS1115 on an I2C bus. This chip
  // has its own converter and no bus: the channel that configuration means is not a
  // channel here, and taking the rest of it would start something else entirely.
  Source source = a_source("light-1", "adc");
  with_integer(source, "pin", 26);
  with_text(source, "chip", "ads1115");

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, &source, 1));
  CHECK(attempt.why == Unplanned::kNoSuchOption);
  CHECK(attempt.said("chip"));
}

TEST(a_converter_and_a_wire_may_not_be_the_same_pin) {
  Source sources[2];
  sources[0] = a_source("light-1", "adc");
  with_integer(sources[0], "pin", 27);
  sources[1] = a_source("door-1", "gpio");
  with_integer(sources[1], "pin", 27);

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, sources, 2));
  CHECK(attempt.why == Unplanned::kPinRefused);
  CHECK(attempt.said("door-1"));
  CHECK(attempt.said("light-1"));
  CHECK(plan.size() == 0);
}

TEST(a_plan_that_is_adopted_owns_its_own_pins) {
  // The device judges a configuration on one plan and runs it on another. What the running
  // plan says about a pin has to survive the next configuration being judged, or a node
  // would be reporting the name of a source somebody merely proposed.
  Source first = a_source("pir-1", "gpio");
  with_integer(first, "pin", 15);
  Plan candidate(Board::kPico2W);
  Attempt attempt;
  CHECK(attempt.of(candidate, &first, 1));

  Plan running(Board::kPico2W);
  running = candidate;
  CHECK(running.size() == 1);
  CHECK(std::strcmp(running.holder(15), "pir-1") == 0);

  // The candidate is now judging something else entirely, on the same pin.
  Source second = a_source("door-1", "gpio");
  with_integer(second, "pin", 15);
  CHECK(attempt.of(candidate, &second, 1));
  CHECK(std::strcmp(running.holder(15), "pir-1") == 0);
  CHECK(std::strcmp(running.at(0).source_id, "pir-1") == 0);
}

TEST(a_source_can_be_kept_in_the_configuration_without_being_read) {
  // The hub's page offers this, in those words. A node that refused it would be refusing
  // the only way a configuration has of keeping a source written down while it is unwired.
  Source sources[2];
  sources[0] = a_source("pir-1", "gpio");
  with_integer(sources[0], "pin", 15);
  with_boolean(sources[0], "enabled", false);
  sources[1] = a_source("board-temperature", "board");

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(attempt.of(plan, sources, 2));
  CHECK(plan.size() == 2);
  CHECK(!plan.at(0).enabled);
  CHECK(plan.at(0).gpio.pin == 15);  // still judged, so that switching it on cannot fail
  CHECK(plan.at(1).enabled);
}

TEST(a_source_nobody_reads_is_not_in_the_way_of_one_somebody_does) {
  Source sources[2];
  sources[0] = a_source("pir-1", "gpio");
  with_integer(sources[0], "pin", 15);
  with_boolean(sources[0], "enabled", false);
  sources[1] = a_source("door-1", "gpio");
  with_integer(sources[1], "pin", 15);

  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(attempt.of(plan, sources, 2));
  CHECK(std::strcmp(plan.holder(15), "door-1") == 0);

  // Both switched on, and it is the conflict it always was.
  Source both[2];
  both[0] = a_source("pir-1", "gpio");
  with_integer(both[0], "pin", 15);
  both[1] = a_source("door-1", "gpio");
  with_integer(both[1], "pin", 15);
  CHECK(!attempt.of(plan, both, 2));
  CHECK(attempt.why == Unplanned::kPinRefused);
  CHECK(attempt.said("both use GPIO 15"));
}

TEST(a_pin_that_is_not_a_pin_is_refused_even_for_a_source_nobody_reads) {
  Source off = a_source("pir-1", "gpio");
  with_integer(off, "pin", 25);  // the LED, on every one of these boards
  with_boolean(off, "enabled", false);
  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, &off, 1));
  CHECK(attempt.why == Unplanned::kPinRefused);
  CHECK(attempt.said("belongs to the board itself"));
}

TEST(enabled_is_true_or_false_and_not_the_word) {
  Source sources[1];
  sources[0] = a_source("pir-1", "gpio");
  with_integer(sources[0], "pin", 15);
  with_text(sources[0], "enabled", "no");
  Plan plan(Board::kPico2W);
  Attempt attempt;
  CHECK(!attempt.of(plan, sources, 1));
  CHECK(attempt.why == Unplanned::kWrongType);
  CHECK(attempt.said("enabled is true or false"));
}

int main() { return harness::run_all("plan"); }
