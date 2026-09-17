// What this firmware knows about the boards it builds for, said by the firmware itself.
//
// A release manifest that lists the pins a board reserves, or the number of sources it
// takes, is making a claim about the code; written by hand it is a claim that can drift
// from the code the day either one changes. So it is not written by hand: the manifest
// asks this, and this asks `pins.h`, `plan.h`, `command.h` and `vault.h`.
//
// One line per board, then one for the limits that are the same on all of them. Tab
// separated, because `tools/release_manifest.py` reads it and nobody else does.

#include <cstdio>

#include "sentry/command.h"
#include "sentry/pins.h"
#include "sentry/plan.h"
#include "sentry/vault.h"

namespace {

struct Known {
  const char* name;  // what the SDK calls it, so that PICO_BOARD is the key
  sentry::Board board;
};

void one_board(const Known& known) {
  std::printf("board\t%s\tradio=%s\tgpio=0-%d\tclaims=%zu\treserved=", known.name,
              sentry::has_radio(known.board) ? "yes" : "no", sentry::kMaxGpio,
              sentry::kMaxClaims);
  bool first = true;
  for (int gpio = 0; gpio <= sentry::kMaxGpio; ++gpio) {
    if (!sentry::is_reserved(known.board, gpio)) continue;
    std::printf("%s%d", first ? "" : ",", gpio);
    first = false;
  }
  std::putchar('\n');
}

}  // namespace

int main() {
  const Known boards[] = {{"pico", sentry::Board::kPico},
                          {"pico_w", sentry::Board::kPicoW},
                          {"pico2", sentry::Board::kPico2},
                          {"pico2_w", sentry::Board::kPico2W}};
  for (const Known& known : boards) one_board(known);
  // The limits a hub has to agree with. `sources` is what this firmware will plan, which
  // is smaller than the `command.h` grammar allows on the wire; `configuration` is what
  // one vault slot holds after the record framing, which is what a hub may send.
  std::printf("limits\tsources=%zu\toptions=%zu\tconfiguration=%zu\tadc=%d-%d\n",
              sentry::kMaxPlanned, sentry::kMaxOptions,
              sentry::most_of(sentry::Held::kConfiguration), sentry::kFirstAdcPin,
              sentry::kLastAdcPin);
  return 0;
}
