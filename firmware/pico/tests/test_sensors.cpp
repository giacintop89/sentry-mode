// A sensor that did not answer produces no value, and never a zero.

#include <cstring>

#include "harness.h"
#include "sentry/sensors.h"

using sentry::adc_volts;
using sentry::board_temperature;
using sentry::ds18b20_temperature;
using sentry::Measured;
using sentry::onewire_crc;
using sentry::Quality;
using sentry::relative_brightness;

namespace {

// A scratchpad with a correct CRC for whatever temperature is put in it.
void scratchpad(int16_t raw, uint8_t out[9]) {
  out[0] = static_cast<uint8_t>(raw & 0xFF);
  out[1] = static_cast<uint8_t>((raw >> 8) & 0xFF);
  out[2] = 0x4B;
  out[3] = 0x46;
  out[4] = 0x7F;
  out[5] = 0xFF;
  out[6] = 0x0C;
  out[7] = 0x10;
  out[8] = onewire_crc(out, 8);
}

}  // namespace

TEST(a_temperature_is_read_when_the_scratchpad_checks_out) {
  uint8_t bytes[9];
  scratchpad(0x0191, bytes);  // 25.0625 °C
  Measured measured = ds18b20_temperature(bytes);
  CHECK(measured.has_value);
  CHECK(measured.quality == Quality::kValid);
  CHECK(measured.value > 25.0 && measured.value < 25.1);

  scratchpad(static_cast<int16_t>(0xFF5E), bytes);  // -10.125 °C
  measured = ds18b20_temperature(bytes);
  CHECK(measured.has_value);
  CHECK(measured.value < -10.0 && measured.value > -10.2);
}

TEST(a_scratchpad_whose_crc_does_not_check_out_is_not_a_temperature) {
  uint8_t bytes[9];
  scratchpad(0x0191, bytes);
  bytes[2] = static_cast<uint8_t>(bytes[2] ^ 0x01);
  Measured measured = ds18b20_temperature(bytes);
  CHECK(!measured.has_value);
  CHECK(measured.quality == Quality::kUnavailable);
}

TEST(a_bus_with_nothing_on_it_is_not_a_sensor_reading_zero) {
  uint8_t absent[9];
  std::memset(absent, 0xFF, sizeof(absent));
  CHECK(!ds18b20_temperature(absent).has_value);
  uint8_t shorted[9];
  std::memset(shorted, 0x00, sizeof(shorted));
  Measured measured = ds18b20_temperature(shorted);
  CHECK(!measured.has_value);
  CHECK(measured.quality == Quality::kUnavailable);
}

TEST(the_85_degrees_a_part_holds_after_a_reset_is_not_published_as_a_measurement) {
  // It is a plausible summer temperature and it is what the part says before it has
  // converted anything. The node cannot tell the two apart, so it says it cannot.
  uint8_t bytes[9];
  scratchpad(0x0550, bytes);
  Measured measured = ds18b20_temperature(bytes);
  CHECK(measured.has_value);
  CHECK(measured.value > 84.9 && measured.value < 85.1);
  CHECK(measured.quality == Quality::kDegraded);
}

TEST(a_count_the_converter_cannot_have_produced_is_not_a_voltage) {
  CHECK(!adc_volts(4096).has_value);
  CHECK(!relative_brightness(65535).has_value);
  Measured measured = adc_volts(0);
  CHECK(measured.has_value && measured.value < 0.001);
  measured = adc_volts(sentry::kAdcMax);
  CHECK(measured.has_value && measured.value > 3.29 && measured.value < 3.31);
}

TEST(the_chips_own_temperature_is_the_die_and_is_labelled_as_such) {
  // 0.706 V is 27 °C by the datasheet's formula. Whatever this is worth, it is the board's
  // temperature and never the room's.
  uint16_t raw = static_cast<uint16_t>((0.706 / sentry::kAdcReference) * sentry::kAdcMax);
  Measured measured = board_temperature(raw);
  CHECK(measured.has_value);
  CHECK(measured.value > 26.0 && measured.value < 28.0);
  CHECK(measured.quality == Quality::kValid);

  Measured impossible = board_temperature(0);  // 0 V: arithmetic, not a measurement
  CHECK(impossible.quality == Quality::kDegraded);
}

TEST(a_light_reading_is_a_fraction_of_full_scale_and_not_lux) {
  Measured measured = relative_brightness(2048);
  CHECK(measured.has_value);
  CHECK(measured.value > 0.49 && measured.value < 0.51);
}

int main() { return harness::run_all("sensors"); }
