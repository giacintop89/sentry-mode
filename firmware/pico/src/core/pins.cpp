#include "sentry/pins.h"

namespace sentry {

bool has_radio(Board board) { return board == Board::kPicoW || board == Board::kPico2W; }

bool is_reserved(Board board, int gpio) {
  // On a board with radio these four belong to the wireless chip: its power, its data, its
  // chip select and its clock, which doubles as the VSYS divider. On a board without one
  // the same four numbers are the regulator's mode pin, the VBUS sense, the LED this
  // firmware blinks as its heartbeat, and VSYS through ADC3.
  //
  // The two lists coincide today. They are kept apart because the reasons do, and because
  // the next board is where copying one onto the other stops being harmless.
  static const int kWireless[] = {23, 24, 25, 29};
  static const int kWired[] = {23, 24, 25, 29};
  const bool wireless = has_radio(board);
  const int* reserved = wireless ? kWireless : kWired;
  size_t count = wireless ? sizeof(kWireless) / sizeof(int) : sizeof(kWired) / sizeof(int);
  for (size_t index = 0; index < count; ++index) {
    if (reserved[index] == gpio) return true;
  }
  return false;
}

const char* name_of(PinRefusal why) {
  switch (why) {
    case PinRefusal::kOutOfRange:
      return "not a pin on this board";
    case PinRefusal::kReserved:
      return "reserved by the board";
    case PinRefusal::kTaken:
      return "already taken";
    case PinRefusal::kNone:
    default:
      return "none";
  }
}

bool PinMap::claim(int gpio, const char* source_id, PinRefusal& why) {
  why = PinRefusal::kNone;
  if (gpio < 0 || gpio > kMaxGpio) {
    why = PinRefusal::kOutOfRange;
    return false;
  }
  if (is_reserved(board_, gpio)) {
    why = PinRefusal::kReserved;
    return false;
  }
  if (holder(gpio) != nullptr) {
    why = PinRefusal::kTaken;
    return false;
  }
  if (count_ >= kMaxClaims) {
    why = PinRefusal::kOutOfRange;
    return false;
  }
  claims_[count_].gpio = gpio;
  claims_[count_].source_id = source_id;
  ++count_;
  return true;
}

bool PinMap::allows(int gpio, PinRefusal& why) const {
  why = PinRefusal::kNone;
  if (gpio < 0 || gpio > kMaxGpio) {
    why = PinRefusal::kOutOfRange;
    return false;
  }
  if (is_reserved(board_, gpio)) {
    why = PinRefusal::kReserved;
    return false;
  }
  return true;
}

const char* PinMap::holder(int gpio) const {
  for (size_t index = 0; index < count_; ++index) {
    if (claims_[index].gpio == gpio) return claims_[index].source_id;
  }
  return nullptr;
}

void PinMap::clear() { count_ = 0; }

}  // namespace sentry
