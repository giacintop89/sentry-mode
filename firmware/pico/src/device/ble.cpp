#include "ble.h"

#include <cstring>

#include "btstack.h"
#include "pico/async_context.h"
#include "pico/btstack_hci_transport_cyw43.h"
#include "pico/btstack_run_loop_async_context.h"
#include "pico/cyw43_arch.h"
#include "pico/stdlib.h"

namespace ble {
namespace {

// A ring the radio writes and the loop reads. A busy building puts out a few hundred
// advertisements a minute and the loop comes round many times a second, so this is deep
// enough for the times it does not: a TLS handshake, a flash write, a reconnection. What
// overflows is counted rather than silently dropped — the count is the difference between
// a quiet room and a busy one nobody drained.
constexpr size_t kQueued = 32;

Sighting queue[kQueued];
volatile size_t written = 0;
volatile size_t read = 0;
volatile uint32_t missed_ = 0;

btstack_packet_callback_registration_t registration;
bool started = false;   // the stack has been brought up once
bool wanted = false;    // somebody has asked for a scanner
bool scanning = false;  // the controller says it is listening
const char* why_not = nullptr;

// Written from the background context, where every advertisement arrives.
void remember(const uint8_t* packet) {
  bd_addr_t address;
  gap_event_advertising_report_get_address(packet, address);
  const uint8_t size = gap_event_advertising_report_get_data_length(packet);
  const uint8_t* data = gap_event_advertising_report_get_data(packet);

  const size_t at = written % kQueued;
  if (written - read >= kQueued) {
    // The loop has not come back. The oldest is the one given up: an advertisement from a
    // second ago is worth less than the one that just arrived, and presence is counted in
    // sightings rather than in any one of them.
    ++missed_;
    read = read + 1;
  }
  Sighting& slot = queue[at];
  std::memcpy(slot.address, address, sizeof(slot.address));
  // The controller reports it as a signed number of dBm, and 127 means it did not measure.
  const int8_t rssi = static_cast<int8_t>(gap_event_advertising_report_get_rssi(packet));
  slot.has_rssi = rssi != 127;
  slot.rssi = rssi;
  slot.size = size > kMaxAdvertisement ? static_cast<uint8_t>(kMaxAdvertisement) : size;
  std::memcpy(slot.data, data, slot.size);
  slot.at_ms = to_ms_since_boot(get_absolute_time());
  written = written + 1;
}

// Passive, and listening all the time. Passive because a scan request is this node
// transmitting, and there is nothing it wants to ask anybody; all the time because a
// window narrower than the interval is a beacon heard every other minute. The unit is
// 0.625 ms, so 48 is 30 ms of every 30 ms.
void begin_scan() {
  gap_set_scan_parameters(0, 48, 48);
  gap_start_scan();
  scanning = true;
  why_not = nullptr;
}

void packet_handler(uint8_t packet_type, uint16_t channel, uint8_t* packet, uint16_t size) {
  (void)channel;
  (void)size;
  if (packet_type != HCI_EVENT_PACKET) return;
  switch (hci_event_packet_get_type(packet)) {
    case BTSTACK_EVENT_STATE:
      if (btstack_event_state_get_state(packet) == HCI_STATE_WORKING) {
        if (wanted) begin_scan();
      } else if (btstack_event_state_get_state(packet) == HCI_STATE_OFF) {
        scanning = false;
        // Said as a state rather than as a failure when it was asked for: a node with
        // nothing to watch is not a node with a broken radio.
        if (wanted) why_not = "the radio stopped";
      }
      break;
    case GAP_EVENT_ADVERTISING_REPORT:
      if (scanning) remember(packet);
      break;
    default:
      break;
  }
}

}  // namespace

bool listen() {
  wanted = true;
  if (scanning) return true;
  // The controller lives on the wireless chip, which the network half brings up. A board
  // that has not joined anything yet has no chip to talk to, and this is called again —
  // it is the loop that asks, every few seconds, for as long as something is watched.
  async_context_t* context = cyw43_arch_async_context();
  if (context == nullptr) {
    why_not = "the wireless chip is not running";
    return false;
  }
  if (!started) {
    // BTstack's own initialisation, without the part of it that keeps bonded devices in
    // flash: this node never bonds, and the sectors that code would take are the vault's.
    btstack_memory_init();
    btstack_run_loop_init(btstack_run_loop_async_context_get_instance(context));
    hci_init(hci_transport_cyw43_instance(), nullptr);
    registration.callback = &packet_handler;
    hci_add_event_handler(&registration);
    started = true;
  }
  async_context_acquire_lock_blocking(context);
  const int trouble_now = hci_power_control(HCI_POWER_ON);
  // Powering on something already on is not an error and raises no event, so the scan is
  // started here as well: coming back from `deaf()` must not wait for something that has
  // already happened.
  if (trouble_now == 0 && hci_get_state() == HCI_STATE_WORKING) begin_scan();
  async_context_release_lock(context);
  if (trouble_now != 0) {
    why_not = "the radio would not start";
    return false;
  }
  if (!scanning) why_not = "the radio is starting";
  return true;
}

void deaf() {
  wanted = false;
  if (!started) return;
  async_context_t* context = cyw43_arch_async_context();
  if (context != nullptr) async_context_acquire_lock_blocking(context);
  if (scanning) gap_stop_scan();
  hci_power_control(HCI_POWER_OFF);
  if (context != nullptr) async_context_release_lock(context);
  scanning = false;
  why_not = "nothing is being watched";
}

bool listening() { return scanning; }

const char* trouble() { return scanning ? nullptr : why_not; }

bool next(Sighting& out) {
  async_context_t* context = cyw43_arch_async_context();
  if (context != nullptr) async_context_acquire_lock_blocking(context);
  const bool any = written != read;
  if (any) {
    out = queue[read % kQueued];
    read = read + 1;
  }
  if (context != nullptr) async_context_release_lock(context);
  return any;
}

uint32_t missed() { return missed_; }

}  // namespace ble
