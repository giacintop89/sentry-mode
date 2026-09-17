// The 1-Wire bus, bit-banged on one pin.
//
// This is the only part of a DS18B20 that has to be hardware: what a scratchpad means, and
// what the 85 °C in it means, is decided in `sensors.cpp` and tested on a host. What is
// here is the timing, which cannot be tested anywhere but on a board.
//
// The bus is open drain. Nothing here ever drives the line high: a device answers by
// pulling it down, and two devices answering at once would be a short if anything drove
// it. The line is held up by a resistor — 4.7 kΩ to 3.3 V is what the part's datasheet
// asks for. The chip's own pull-up is around fifty times weaker than that and is enabled
// here only so that a bus with nothing on it reads as absent rather than as noise; it is
// not a substitute for the resistor, and a probe on a long wire will not work without one.
//
// Every call here blocks: the reset is about a millisecond, a byte is about half of one.
// Nothing waits for a conversion — that is 750 ms, and it belongs to the caller's clock.

#ifndef SENTRY_DEVICE_ONEWIRE_H
#define SENTRY_DEVICE_ONEWIRE_H

#include <cstdint>

#include "hardware/gpio.h"

namespace onewire {

// The pin as this bus wants it: an input with a weak pull-up, and a zero waiting in the
// output register for whenever the line is pulled down.
void take(uint pin);
void give_back(uint pin);

// The reset pulse, and whether anything answered it.
bool present(uint pin);

// SKIP ROM, or MATCH ROM with `rom` — the one probe on the bus, or that one of several.
void address(uint pin, const uint8_t* rom);

// CONVERT T. False if nothing answered the reset: there is no probe to convert anything.
bool start_a_conversion(uint pin, const uint8_t* rom);

// READ SCRATCHPAD, nine bytes as they came off the bus. False if nothing answered; the
// bytes are still written, because all ones is what an empty bus reads as and `sensors.cpp`
// knows what to do with that.
bool read_the_scratchpad(uint pin, const uint8_t* rom, uint8_t out[9]);

// What a conversion takes at twelve bits, which is what a DS18B20 powers up at.
inline constexpr uint32_t kConversionMs = 750;

}  // namespace onewire

#endif  // SENTRY_DEVICE_ONEWIRE_H
