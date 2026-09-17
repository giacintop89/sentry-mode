#include "ble.h"

// A board without the wireless chip has no radio to listen with. This is the whole of BLE
// on such a board: a scanner that says it is not running, so that a source watching for a
// device reports unknown rather than an empty room — and no BTstack in the image at all.
//
// A `ble` source is refused at configuration time on these boards, so nothing here should
// ever be reached. It exists because "should never" is not a linker guarantee.

namespace ble {

bool listen() { return false; }

void deaf() {}

bool listening() { return false; }

const char* trouble() { return "this board has no radio"; }

bool next(Sighting& out) {
  (void)out;
  return false;
}

uint32_t missed() { return 0; }

}  // namespace ble
