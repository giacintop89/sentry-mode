#include "reset_reason.h"

#include "hardware/watchdog.h"
#include "pico/platform.h"

#if PICO_RP2040
#include "hardware/structs/vreg_and_chip_reset.h"
#else
#include "hardware/structs/powman.h"
#endif

namespace device {
namespace {

// The bits that say a person or a probe did it, kept apart from the ones that say the
// supply did, because those two lead to different places when a board keeps restarting.
uint32_t chip_reset_bits() {
#if PICO_RP2040
  return vreg_and_chip_reset_hw->chip_reset;
#else
  return powman_hw->chip_reset;
#endif
}

}  // namespace

sentry::Woke woke_because() {
  // The watchdog first, and in two halves: the timer running out is a fault, and a reboot
  // asked for through the watchdog — which is how this chip reboots at all — is not.
  if (watchdog_enable_caused_reboot()) return sentry::Woke::kWatchdog;
  if (watchdog_caused_reboot()) return sentry::Woke::kSoftware;

  const uint32_t had = chip_reset_bits();
#if PICO_RP2040
  if ((had & VREG_AND_CHIP_RESET_CHIP_RESET_HAD_POR_BITS) != 0) return sentry::Woke::kPower;
  if ((had & VREG_AND_CHIP_RESET_CHIP_RESET_HAD_RUN_BITS) != 0) return sentry::Woke::kButton;
  if ((had & VREG_AND_CHIP_RESET_CHIP_RESET_HAD_PSM_RESTART_BITS) != 0) {
    return sentry::Woke::kDebugger;
  }
#else
  if ((had & POWMAN_CHIP_RESET_HAD_POR_BITS) != 0) return sentry::Woke::kPower;
  // A brown-out is its own answer on this chip: the supply sagged, nobody pressed
  // anything, and a board that keeps doing it needs a power supply rather than a patch.
  if ((had & (POWMAN_CHIP_RESET_HAD_BOR_BITS | POWMAN_CHIP_RESET_HAD_GLITCH_DETECT_BITS)) != 0) {
    return sentry::Woke::kBrownout;
  }
  if ((had & POWMAN_CHIP_RESET_HAD_RUN_LOW_BITS) != 0) return sentry::Woke::kButton;
  if ((had & (POWMAN_CHIP_RESET_HAD_DP_RESET_REQ_BITS | POWMAN_CHIP_RESET_HAD_RESCUE_BITS |
              POWMAN_CHIP_RESET_HAD_HZD_SYS_RESET_REQ_BITS)) != 0) {
    return sentry::Woke::kDebugger;
  }
#endif
  return sentry::Woke::kUnknown;
}

}  // namespace device
