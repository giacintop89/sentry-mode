#include "flash_vault.h"

#include <cstring>
#include <initializer_list>

#include "hardware/flash.h"
#include "hardware/sync.h"
#include "hardware/watchdog.h"
#include "pico/stdlib.h"
#include "sentry/store.h"

namespace vault {
namespace {

// A slot is one erase sector, and that is not this file's opinion: it is what the part can
// be told to forget. If a future part disagrees, this stops the build rather than erasing
// somebody else's sector.
static_assert(sentry::kVaultSlotBytes == FLASH_SECTOR_SIZE, "a slot is one erase sector");
static_assert(sentry::kVaultSlotBytes % FLASH_PAGE_SIZE == 0, "a slot is whole pages");

// One sector, built in RAM before any of it is written. It is static because the main loop
// runs on a four kilobyte stack and this is four kilobytes.
char page[sentry::kVaultSlotBytes];

const char* slot_at(sentry::Held held, sentry::Slot slot) {
  const size_t offset = sentry::offset_of(held, slot);
  if (offset >= sentry::kVaultBytes) return nullptr;
  return reinterpret_cast<const char*>(XIP_BASE + begins_at() + offset);
}

// Which slot this node is booting from, and what is in it.
sentry::Slot in_use(sentry::Held held, sentry::Record& record) {
  return sentry::choose(slot_at(held, sentry::Slot::kFirst), sentry::kVaultSlotBytes,
                        slot_at(held, sentry::Slot::kSecond), sentry::kVaultSlotBytes, record);
}

// Erase and program, with nothing else running. The flash cannot be read while it is being
// written, and on this chip the code doing the writing is itself in flash: anything that
// interrupts it — an alarm, the radio's own callback — would return into a part that is
// not answering. The SDK's functions are written to run from RAM; what this has to do is
// make sure nothing else runs at all.
void write_one_sector(uint32_t offset, const char* bytes) {
  const uint32_t interrupts = save_and_disable_interrupts();
  flash_range_erase(offset, sentry::kVaultSlotBytes);
  flash_range_program(offset, reinterpret_cast<const uint8_t*>(bytes), sentry::kVaultSlotBytes);
  restore_interrupts(interrupts);
}

void erase_one_sector(uint32_t offset) {
  const uint32_t interrupts = save_and_disable_interrupts();
  flash_range_erase(offset, sentry::kVaultSlotBytes);
  restore_interrupts(interrupts);
}

}  // namespace

uint32_t begins_at() {
  // The end of the part. Whatever the program grew to, it is not here: the linker lays the
  // image out from the beginning, and a firmware that reached this far would be a firmware
  // more than a megabyte larger than this one.
  return static_cast<uint32_t>(PICO_FLASH_SIZE_BYTES - sentry::kVaultBytes);
}

size_t load(sentry::Held held, char* out, size_t capacity) {
  sentry::Record record;
  if (in_use(held, record) == sentry::Slot::kNeither) return 0;
  if (record.size > capacity) return 0;
  std::memcpy(out, record.payload, record.size);
  return record.size;
}

bool holds(sentry::Held held, uint32_t& sequence) {
  sentry::Record record;
  if (in_use(held, record) == sentry::Slot::kNeither) return false;
  sequence = record.sequence;
  return true;
}

bool save(sentry::Held held, const char* bytes, size_t size) {
  if (size == 0 || size > sentry::most_of(held)) return false;
  sentry::Record record;
  const sentry::Slot current = in_use(held, record);
  const sentry::Slot next = sentry::next_write(current);
  const uint32_t sequence = current == sentry::Slot::kNeither ? 1 : record.sequence + 1;

  // Everything outside the record is left as an erased sector reads, so what follows a
  // short record is not the tail of whatever was there before.
  std::memset(page, 0xFF, sizeof(page));
  if (sentry::write_record(bytes, size, sequence, page, sizeof(page)) == 0) return false;

  watchdog_update();
  write_one_sector(begins_at() + static_cast<uint32_t>(sentry::offset_of(held, next)), page);
  watchdog_update();

  // Read it back through the same path the boot will: a write that the part accepted and
  // did not keep is a write that has not happened.
  sentry::Record written;
  return sentry::read_record(slot_at(held, next), sentry::kVaultSlotBytes, written) &&
         written.sequence == sequence && written.size == size;
}

bool tear(sentry::Held held) {
  sentry::Record record;
  const sentry::Slot current = in_use(held, record);
  const sentry::Slot next = sentry::next_write(current);
  // Something long enough to be cut in half. What it says does not matter: what is being
  // written is a record whose header promises more than the flash ends up holding.
  char pretend[FLASH_PAGE_SIZE * 3];
  std::memset(pretend, 'x', sizeof(pretend));
  std::memset(page, 0xFF, sizeof(page));
  const uint32_t sequence = (current == sentry::Slot::kNeither ? 1 : record.sequence + 1) + 1000;
  if (sentry::write_record(pretend, sizeof(pretend), sequence, page, sizeof(page)) == 0) {
    return false;
  }
  const uint32_t offset = begins_at() + static_cast<uint32_t>(sentry::offset_of(held, next));
  watchdog_update();
  {
    // Erased, and then one page of it programmed: exactly as far as a write gets when the
    // power goes off in the middle of one, and with a sequence number high enough that a
    // node comparing them without checking them would choose this one.
    const uint32_t interrupts = save_and_disable_interrupts();
    flash_range_erase(offset, sentry::kVaultSlotBytes);
    flash_range_program(offset, reinterpret_cast<const uint8_t*>(page), FLASH_PAGE_SIZE);
    restore_interrupts(interrupts);
  }
  watchdog_update();
  sentry::Record torn;
  // It has to be unreadable, or this proves nothing.
  return !sentry::read_record(slot_at(held, next), sentry::kVaultSlotBytes, torn);
}

bool forget(sentry::Held held) {
  for (sentry::Slot slot : {sentry::Slot::kFirst, sentry::Slot::kSecond}) {
    watchdog_update();
    erase_one_sector(begins_at() + static_cast<uint32_t>(sentry::offset_of(held, slot)));
  }
  uint32_t sequence = 0;
  return !holds(held, sequence);
}

bool forget_everything() {
  bool all_gone = true;
  for (sentry::Held held : {sentry::Held::kIdentity, sentry::Held::kAuthority,
                            sentry::Held::kCertificate, sentry::Held::kPrivateKey,
                            sentry::Held::kConfiguration}) {
    if (!forget(held)) all_gone = false;
  }
  return all_gone;
}

}  // namespace vault
