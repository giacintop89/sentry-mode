// A microphone on three pins, and the only thing this board does with it.
//
// The state machine clocks the microphone and reads its data; two DMA channels start each
// other, so one is always filling a block while the loop reads the other and nothing is
// missing from between them. What comes back is a block of signed 16-bit samples — one
// channel of the two, the one the microphone was wired to drive — and what is made of them
// is in `acoustic.cpp`, which has never seen a pin.
//
// A loop that is late by a whole block gets nothing rather than something torn: each
// buffer wraps on itself in hardware, so a channel whose turn came round again overwrites
// its own block from the beginning and can never write past it, and a block that was
// caught being overwritten is dropped and counted. A level detector can miss a block. It
// cannot survive being told a number that was half one block and half the next.
//
// Nothing here keeps audio. A block is looked at once, turned into a level, and the buffer
// it came in is handed straight back to the DMA. There is no second copy, no queue of
// sound and no way to ask this firmware for any: `audio.activity` says that something was
// loud, and that is the whole of what leaves the board.

#ifndef SENTRY_DEVICE_I2S_H
#define SENTRY_DEVICE_I2S_H

#include <cstddef>
#include <cstdint>

#include "pico/types.h"  // uint, which is what the SDK calls a pin

namespace i2s {

// What the microphone is sampled at. Sixteen kilohertz is what the agent captures and what
// the hub's audio is written against; nothing here resamples, so this is the one rate.
inline constexpr uint32_t kRate = 16000;

// How many frames a block holds, and so how long one is: 512 frames is 32 ms, which is
// short enough that a level follows a door closing and long enough that the loop is not
// counting samples all day.
inline constexpr size_t kFramesPerBlock = 512;

// Take the pins and start the clock. `data` is the microphone's output, `clock` is the bit
// clock and `clock + 1` is the word select. False if there is no state machine or no DMA
// channel left, which is a board asked for more than it has rather than a fault.
bool listen(uint data, uint clock, bool left);

// Give the pins back and stop the clock. Idempotent.
void stop();

// Whether the microphone is being clocked right now.
bool running();

// Why it is not, or null.
const char* trouble();

// The next block nobody has looked at, or none. The samples belong to this module and are
// valid until the next call: the caller measures them and does not keep them.
bool next(const int16_t*& samples, size_t& count);

// How many blocks were filled before the loop came back for them and had to be overwritten.
uint32_t missed();

}  // namespace i2s

#endif  // SENTRY_DEVICE_I2S_H
