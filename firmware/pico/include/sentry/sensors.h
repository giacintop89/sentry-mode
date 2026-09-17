// Turning what a sensor returned into a reading, or into an admission that there is none.
//
// The rule these share: a sensor that did not answer produces no value, not zero. A bus
// with nothing on it reads as all ones; a DS18B20 that has never converted holds 85 °C
// from its own reset; an ADC channel with nothing wired to it returns noise that looks
// exactly like a measurement. Each of those is a way of publishing a number nobody
// measured, and each is refused here with a quality that says so.

#ifndef SENTRY_SENSORS_H
#define SENTRY_SENSORS_H

#include <cstddef>
#include <cstdint>

#include "sentry/event.h"

namespace sentry {

// What a reading came to, and how much it can be trusted. `has_value` false means there is
// nothing to publish but the quality: a reading with no value is not a reading of zero.
struct Measured {
  bool has_value = false;
  double value = 0.0;
  Quality quality = Quality::kUnavailable;
};

// The Maxim CRC-8 the 1-Wire devices use, over a scratchpad or a ROM code.
uint8_t onewire_crc(const uint8_t* bytes, size_t count);

// A DS18B20 scratchpad, as read: nine bytes, the last of which is the CRC of the other
// eight. Refuses a bad CRC, an absent bus, a value outside what the part can measure, and
// the 85 °C the part holds after a reset until the first conversion finishes.
Measured ds18b20_temperature(const uint8_t scratchpad[9]);

// The RP2040 and RP2350 ADC is 12 bits against a 3.3 V reference.
inline constexpr uint16_t kAdcMax = 4095;
inline constexpr double kAdcReference = 3.3;

// Volts on the pin. A raw count the converter cannot have produced is not a voltage.
Measured adc_volts(uint16_t raw);

// The chip's own temperature sensor, from the formula in the datasheet. It measures the
// die, which is warmer than the room and is not the room: whatever this is published as,
// it is `board.temperature` and never an ambient reading.
Measured board_temperature(uint16_t raw);

// A light-dependent resistor read against the ADC, as a fraction of full scale. It is not
// lux and must not be labelled as lux: nothing here has been calibrated against a light
// meter, and a number with a unit nobody earned is worse than a number with none.
Measured relative_brightness(uint16_t raw);

}  // namespace sentry

#endif  // SENTRY_SENSORS_H
