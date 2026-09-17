// The radio, listening.
//
// This is the only part of BLE presence that has to be hardware. What an advertisement
// means — whether it is the device being watched, whether enough of them close enough
// together amount to somebody arriving — is decided in `presence.cpp`, which has never
// seen a radio and is tested on a host.
//
// What is here is the scanner: turning the controller on, asking it to listen passively,
// and handing every advertisement it hears to the loop. Nothing is matched here and
// nothing is decided here.
//
// Two rules shape it. The first is that the loop must not be blocked: the stack runs in
// the same background context lwIP does, and every sighting is left in a queue the loop
// drains when it gets to it. The second is that a scanner that is not running must be
// visible as such — a node that stopped hearing has not learned that the room is empty,
// and `listening()` is what the presence code is told so that it can say unknown instead
// of absent.

#ifndef SENTRY_DEVICE_BLE_H
#define SENTRY_DEVICE_BLE_H

#include <cstddef>
#include <cstdint>

namespace ble {

// The most an advertisement carries in the payload this firmware looks at. Extended
// advertising can be longer; nothing this node watches uses it, and a beacon that did
// would be heard and not understood rather than misunderstood.
inline constexpr size_t kMaxAdvertisement = 31;

// One advertisement, as it came off the air: who sent it, how loud, and the bytes.
struct Sighting {
  uint8_t address[6] = {};
  int32_t rssi = 0;
  bool has_rssi = false;
  uint8_t data[kMaxAdvertisement] = {};
  uint8_t size = 0;
  uint64_t at_ms = 0;
};

// Start listening. Idempotent: the second call is not a second radio. False if the
// controller could not be brought up at all, which is said out loud rather than retried
// forever — a board whose Bluetooth does not start is a board somebody has to look at.
bool listen();

// Stop listening and power the controller down. The Wi-Fi side is untouched: they share a
// chip, not a switch.
void deaf();

// Whether the controller is scanning right now. Between `listen()` and the controller
// coming up this is false, which is the truth: nothing is being heard yet.
bool listening();

// Why it is not listening, or null. Text for a person, and for the health message.
const char* trouble();

// The next advertisement nobody has looked at, oldest first. False when there are none.
bool next(Sighting& out);

// How many were thrown away because the loop did not come back for them. A number that
// grows is a loop spending too long somewhere else, and it belongs in health rather than
// in a silence that looks like an empty room.
uint32_t missed();

}  // namespace ble

#endif  // SENTRY_DEVICE_BLE_H
