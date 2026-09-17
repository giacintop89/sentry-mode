#include "sentry/sensors.h"

namespace sentry {
namespace {

// What a DS18B20 holds from its own power-on reset: 85.0 °C, in the raw encoding. A part
// that has not finished a conversion returns it, and it is a plausible-looking temperature
// on a summer day, which is exactly what makes it dangerous to publish.
constexpr int16_t kPowerOnValue = 0x0550;

bool all_the_same(const uint8_t* bytes, size_t count, uint8_t value) {
  for (size_t index = 0; index < count; ++index) {
    if (bytes[index] != value) return false;
  }
  return true;
}

}  // namespace

uint8_t onewire_crc(const uint8_t* bytes, size_t count) {
  uint8_t remainder = 0;
  for (size_t index = 0; index < count; ++index) {
    remainder = static_cast<uint8_t>(remainder ^ bytes[index]);
    for (int bit = 0; bit < 8; ++bit) {
      bool low = (remainder & 1u) != 0;
      remainder = static_cast<uint8_t>(remainder >> 1);
      if (low) remainder = static_cast<uint8_t>(remainder ^ 0x8C);
    }
  }
  return remainder;
}

Measured ds18b20_temperature(const uint8_t scratchpad[9]) {
  Measured measured;
  if (scratchpad == nullptr) return measured;
  // Nothing on the bus reads as all ones; a bus held down reads as all zeroes. Neither is
  // a sensor, and the CRC of either happens to check out, so this comes first.
  if (all_the_same(scratchpad, 9, 0xFF) || all_the_same(scratchpad, 9, 0x00)) return measured;
  if (onewire_crc(scratchpad, 8) != scratchpad[8]) return measured;

  int16_t raw = static_cast<int16_t>(static_cast<uint16_t>(scratchpad[0]) |
                                     (static_cast<uint16_t>(scratchpad[1]) << 8));
  if (raw == kPowerOnValue) {
    // It may genuinely be 85 °C, and this node cannot tell. It says so rather than
    // choosing: degraded is a reading the hub can weigh, 85.0 as valid is one it cannot.
    measured.has_value = true;
    measured.value = 85.0;
    measured.quality = Quality::kDegraded;
    return measured;
  }
  double celsius = static_cast<double>(raw) / 16.0;
  if (celsius < -55.0 || celsius > 125.0) return measured;  // not something the part measures
  measured.has_value = true;
  measured.value = celsius;
  measured.quality = Quality::kValid;
  return measured;
}

Measured adc_volts(uint16_t raw) {
  Measured measured;
  if (raw > kAdcMax) return measured;  // not a count this converter produces
  measured.has_value = true;
  measured.value = (static_cast<double>(raw) * kAdcReference) / static_cast<double>(kAdcMax);
  measured.quality = Quality::kValid;
  return measured;
}

Measured board_temperature(uint16_t raw) {
  Measured volts = adc_volts(raw);
  if (!volts.has_value) return Measured{};
  Measured measured;
  measured.has_value = true;
  measured.value = 27.0 - (volts.value - 0.706) / 0.001721;
  // Outside this the die is not at a temperature the formula describes, and the number
  // would be arithmetic rather than a measurement.
  measured.quality =
      (measured.value > -40.0 && measured.value < 105.0) ? Quality::kValid : Quality::kDegraded;
  return measured;
}

Measured relative_brightness(uint16_t raw) {
  Measured measured;
  if (raw > kAdcMax) return measured;
  measured.has_value = true;
  measured.value = static_cast<double>(raw) / static_cast<double>(kAdcMax);
  measured.quality = Quality::kValid;
  return measured;
}

}  // namespace sentry
