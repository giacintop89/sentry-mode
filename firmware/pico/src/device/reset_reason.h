// Why this board is running again, read from the chip before anything can overwrite it.
//
// The watchdog answers for itself; the rest is one register, in a different place and with
// different bits on RP2040 and RP2350. What neither chip has is a way to say "I do not
// know", so that is this file's job: a cause nothing matched is reported as unknown rather
// than as the most likely guess, because a guess is what somebody would then go and debug.

#ifndef SENTRY_DEVICE_RESET_REASON_H
#define SENTRY_DEVICE_RESET_REASON_H

#include "sentry/control.h"

namespace device {

// Read it once, at the top of main(), before the watchdog is turned on again.
sentry::Woke woke_because();

}  // namespace device

#endif  // SENTRY_DEVICE_RESET_REASON_H
