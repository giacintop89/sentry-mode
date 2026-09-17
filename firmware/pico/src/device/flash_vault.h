// The vault, on the flash this board actually has.
//
// `sentry/vault.h` says where each thing goes and how big it may be; this is the part that
// erases and programs, and it is the only file in the firmware that writes to flash.
//
// The region is taken out of the end of the part, past anything the program itself
// occupies, and nothing here is compiled into the image: a node is provisioned over the
// cable and writes what it was told. That is what keeps the UF2 free of secrets — it is
// the same image on every board, and what makes a board this node lives on the flash.

#ifndef SENTRY_DEVICE_FLASH_VAULT_H
#define SENTRY_DEVICE_FLASH_VAULT_H

#include <cstddef>
#include <cstdint>

#include "sentry/vault.h"

namespace vault {

// Where the vault begins, as an offset into the flash part. Constant for a given build.
uint32_t begins_at();

// What is kept, copied into the caller's buffer, or 0 if neither slot checks out — which
// is what a board that was never provisioned reads, and also what one interrupted in the
// middle of its first write reads.
size_t load(sentry::Held held, char* out, size_t capacity);

// Frame it and write it to whichever slot is not in use. The one in use is untouched until
// the new one is written and readable, so power lost anywhere in here leaves the old
// record where it was.
bool save(sentry::Held held, const char* bytes, size_t size);

// Erase both slots of one thing, or of all of them. This is the recovery verb: a board
// whose identity is wrong, or whose key must not stay on it, is erased over the cable.
bool forget(sentry::Held held);
bool forget_everything();

// Write half a record into the slot that is not in use, on purpose. This is the only way
// to see on the board itself what an interrupted write leaves behind: the framing says how
// long the record is, the flash holds the first page of it and nothing else, and the next
// boot has to notice that and fall back on the slot that was already there. It is a
// diagnostic, reachable only from the cable, and it never touches the slot in use.
bool tear(sentry::Held held);

// Whether there is a readable record, and what its sequence number is, without giving back
// what is in it. This is what `status` may say about a private key.
bool holds(sentry::Held held, uint32_t& sequence);

}  // namespace vault

#endif  // SENTRY_DEVICE_FLASH_VAULT_H
