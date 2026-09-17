#include "i2s.h"

#include "hardware/dma.h"
#include "hardware/pio.h"
#include "i2s.pio.h"
#include "pico/stdlib.h"

namespace i2s {
namespace {

// Both channels come off the wire whether or not anything wants them: the frame is what it
// is, and the microphone is wired to drive one half of it.
constexpr size_t kWordsPerBlock = kFramesPerBlock * 2;
constexpr size_t kBytesPerBlock = kWordsPerBlock * sizeof(uint32_t);
// Each buffer wraps on itself in hardware, which is what makes the pair safe: a channel
// that is started again, for any reason and at any moment, writes at the beginning of its
// own buffer and can never write past the end of it. The alignment is the ring's: the DMA
// wraps an address by masking bits, so a buffer of 4096 bytes has to begin at one.
static_assert(kBytesPerBlock == 4096, "the ring below is written for a 4096-byte block");
constexpr uint32_t kRingBits = 12;

alignas(4096) uint32_t frames[2][kWordsPerBlock];
int16_t block[kFramesPerBlock];

PIO pio_ = nullptr;
uint machine_ = 0;
uint program_ = 0;
int channel_[2] = {-1, -1};
uint data_pin_ = 0;
uint clock_pin_ = 0;
bool left_ = true;
bool running_ = false;
size_t reading_ = 0;  // the buffer the loop takes next
uint32_t missed_ = 0;
const char* why_not = "nothing is listening";

uint32_t finished(size_t which) { return 1u << static_cast<uint>(channel_[which]); }

void let_go() {
  for (size_t which = 0; which < 2; ++which) {
    if (channel_[which] < 0) continue;
    dma_channel_abort(static_cast<uint>(channel_[which]));
    dma_channel_unclaim(static_cast<uint>(channel_[which]));
    channel_[which] = -1;
  }
  if (pio_ != nullptr) {
    pio_sm_set_enabled(pio_, machine_, false);
    pio_remove_program_and_unclaim_sm(&i2s_rx_program, pio_, machine_, program_);
    pio_ = nullptr;
  }
}

}  // namespace

bool listen(uint data, uint clock, bool left) {
  if (running_) return true;
  // One state machine and one program, on whichever PIO block has room. Nothing else in
  // this firmware uses PIO, so this only ever fails on a board somebody has added to.
  if (!pio_claim_free_sm_and_add_program(&i2s_rx_program, &pio_, &machine_, &program_)) {
    pio_ = nullptr;
    why_not = "there is no state machine free for a microphone";
    return false;
  }
  for (size_t which = 0; which < 2; ++which) {
    channel_[which] = dma_claim_unused_channel(false);
    if (channel_[which] < 0) {
      let_go();
      why_not = "there is no DMA channel free for a microphone";
      return false;
    }
  }

  data_pin_ = data;
  clock_pin_ = clock;
  left_ = left;
  reading_ = 0;
  i2s_rx_program_init(pio_, machine_, program_, data, clock, kRate);

  // Two channels that start each other, so the microphone is never unattended: one fills a
  // block while the loop reads the other, and the handover is a hardware one. A single
  // channel would stop at the end of every block and wait to be started again, and the
  // sound of that wait — a millisecond here, a TLS handshake there — would be missing from
  // every block this board ever measured.
  for (size_t which = 0; which < 2; ++which) {
    dma_channel_config config = dma_channel_get_default_config(static_cast<uint>(channel_[which]));
    channel_config_set_transfer_data_size(&config, DMA_SIZE_32);
    channel_config_set_read_increment(&config, false);
    channel_config_set_write_increment(&config, true);
    channel_config_set_ring(&config, true, kRingBits);
    channel_config_set_dreq(&config, pio_get_dreq(pio_, machine_, false));
    channel_config_set_chain_to(&config, static_cast<uint>(channel_[1 - which]));
    dma_channel_configure(static_cast<uint>(channel_[which]), &config, frames[which],
                          &pio_->rxf[machine_], kWordsPerBlock, false);
  }

  // The debug bits say whether the state machine ever had to wait with a word in hand, and
  // the interrupt bits say which block has just been finished. Both are read rather than
  // hooked up: this firmware has one loop and no interrupt handlers, and a microphone is
  // not a reason to start.
  pio_->fdebug = 0xffffffffu;
  dma_hw->intr = finished(0) | finished(1);
  pio_sm_clear_fifos(pio_, machine_);
  pio_sm_set_enabled(pio_, machine_, true);
  dma_channel_start(static_cast<uint>(channel_[0]));
  running_ = true;
  why_not = nullptr;
  return true;
}

void stop() {
  if (!running_) return;
  let_go();
  // The pins are given back as plain inputs: a clock left running into a room nobody is
  // listening to is a microphone that looks switched off and is not.
  gpio_set_function(data_pin_, GPIO_FUNC_NULL);
  gpio_set_function(clock_pin_, GPIO_FUNC_NULL);
  gpio_set_function(clock_pin_ + 1, GPIO_FUNC_NULL);
  running_ = false;
  why_not = "nothing is listening";
}

bool running() { return running_; }

const char* trouble() { return running_ ? nullptr : why_not; }

bool next(const int16_t*& samples, size_t& count) {
  if (!running_) return false;
  if ((dma_hw->intr & finished(reading_)) == 0) return false;

  // One half of the frame, and the top sixteen bits of it. A MEMS microphone puts 24 bits
  // in a 32-bit word, most significant first; taking the top sixteen is the same
  // resolution the agent's capture has, and no arithmetic is done to the sound beyond it.
  const size_t half = left_ ? 0 : 1;
  const uint32_t* raw = frames[reading_];
  for (size_t frame = 0; frame < kFramesPerBlock; ++frame) {
    block[frame] = static_cast<int16_t>(raw[frame * 2 + half] >> 16);
  }

  // Whether the other channel finished while that was being copied. It takes a whole block
  // — thirty-two milliseconds — for that to happen, and if it did then this buffer's turn
  // came round again and what was copied is half one block and half the next. A level
  // measured from that is not a level of anything, so it is dropped and counted instead.
  const bool overtaken = (dma_hw->intr & finished(1 - reading_)) != 0;
  // And whether the state machine ever had a word in hand with nowhere to put it, which is
  // sound that never reached memory at all.
  const uint32_t stalled = pio_->fdebug & (1u << (PIO_FDEBUG_RXSTALL_LSB + machine_));
  if (stalled != 0) pio_->fdebug = stalled;

  dma_hw->intr = finished(reading_);
  reading_ = 1 - reading_;
  if (overtaken || stalled != 0) {
    ++missed_;
    return false;
  }
  samples = block;
  count = kFramesPerBlock;
  return true;
}

uint32_t missed() { return missed_; }

}  // namespace i2s
