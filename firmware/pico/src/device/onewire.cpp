#include "onewire.h"

#include "hardware/sync.h"
#include "pico/time.h"

namespace onewire {
namespace {

// The line is pulled down by making the pin an output; the zero it drives was put in the
// output register once, when the pin was taken.
void pull_down(uint pin) { gpio_set_dir(pin, GPIO_OUT); }
void let_go(uint pin) { gpio_set_dir(pin, GPIO_IN); }

// One bit out. The slot is 60 µs either way; what differs is how much of it the line is
// held down for. Interrupts are off for the part that is measured in microseconds, because
// lwIP servicing the radio in the middle of a slot would turn a one into a zero.
void write_bit(uint pin, bool one) {
  const uint32_t interrupts = save_and_disable_interrupts();
  pull_down(pin);
  busy_wait_us_32(one ? 6 : 60);
  let_go(pin);
  busy_wait_us_32(one ? 64 : 10);
  restore_interrupts(interrupts);
}

// One bit in. The probe answers inside the first fifteen microseconds of the slot, so the
// line is sampled at nine and the rest of the slot is waited out afterwards.
bool read_bit(uint pin) {
  const uint32_t interrupts = save_and_disable_interrupts();
  pull_down(pin);
  busy_wait_us_32(6);
  let_go(pin);
  busy_wait_us_32(9);
  const bool high = gpio_get(pin) != 0;
  restore_interrupts(interrupts);
  busy_wait_us_32(55);
  return high;
}

void write_byte(uint pin, uint8_t value) {
  for (int bit = 0; bit < 8; ++bit) {
    write_bit(pin, (value & (1u << bit)) != 0);  // least significant bit first
  }
}

uint8_t read_byte(uint pin) {
  uint8_t value = 0;
  for (int bit = 0; bit < 8; ++bit) {
    if (read_bit(pin)) value = static_cast<uint8_t>(value | (1u << bit));
  }
  return value;
}

constexpr uint8_t kSkipRom = 0xCC;
constexpr uint8_t kMatchRom = 0x55;
constexpr uint8_t kConvertT = 0x44;
constexpr uint8_t kReadScratchpad = 0xBE;

}  // namespace

void take(uint pin) {
  gpio_init(pin);
  gpio_put(pin, false);
  gpio_set_dir(pin, GPIO_IN);
  gpio_pull_up(pin);
}

void give_back(uint pin) {
  gpio_set_pulls(pin, false, false);
  gpio_deinit(pin);
}

bool present(uint pin) {
  const uint32_t interrupts = save_and_disable_interrupts();
  pull_down(pin);
  busy_wait_us_32(480);
  let_go(pin);
  busy_wait_us_32(70);
  const bool answered = gpio_get(pin) == 0;
  restore_interrupts(interrupts);
  busy_wait_us_32(410);
  return answered;
}

void address(uint pin, const uint8_t* rom) {
  if (rom == nullptr) {
    write_byte(pin, kSkipRom);
    return;
  }
  write_byte(pin, kMatchRom);
  for (size_t index = 0; index < 8; ++index) write_byte(pin, rom[index]);
}

bool start_a_conversion(uint pin, const uint8_t* rom) {
  if (!present(pin)) return false;
  address(pin, rom);
  write_byte(pin, kConvertT);
  return true;
}

bool read_the_scratchpad(uint pin, const uint8_t* rom, uint8_t out[9]) {
  for (size_t index = 0; index < 9; ++index) out[index] = 0xFF;
  if (!present(pin)) return false;
  address(pin, rom);
  write_byte(pin, kReadScratchpad);
  for (size_t index = 0; index < 9; ++index) out[index] = read_byte(pin);
  return true;
}

}  // namespace onewire
