#include "sentry/plan.h"

#include <cstdarg>
#include <cstdio>
#include <cstring>

#include "sentry/names.h"
#include "sentry/sensors.h"

namespace sentry {
namespace {

// Written into the caller's buffer, never anywhere else. The detail is for a person, so it
// names the source first: that is the word they will look for.
void say(char* detail, size_t capacity, const char* format, ...) __attribute__((format(printf, 3, 4)));

void say(char* detail, size_t capacity, const char* format, ...) {
  if (detail == nullptr || capacity == 0) return;
  va_list arguments;
  va_start(arguments, format);
  const int written = std::vsnprintf(detail, capacity, format, arguments);
  va_end(arguments);
  if (written < 0) detail[0] = '\0';
}

const Option* option_named(const Source& source, const char* name) {
  for (size_t index = 0; index < source.option_count; ++index) {
    if (std::strcmp(source.options[index].name, name) == 0) return &source.options[index];
  }
  return nullptr;
}

bool copy_into(char* out, size_t capacity, const char* text) {
  const size_t length = std::strlen(text);
  if (length + 1 > capacity) return false;
  std::memcpy(out, text, length + 1);
  return true;
}

// An integer written as 15 and one written as 15.0 are the same number to whoever wrote
// the file. One written as 15.5 is not an integer and is not rounded into one here.
bool whole_number(const Option& option, int64_t& out) {
  if (option.type == Option::Type::kInteger) {
    out = option.integer;
    return true;
  }
  if (option.type == Option::Type::kNumber) {
    const double value = option.number;
    if (value != static_cast<double>(static_cast<int64_t>(value))) return false;
    out = static_cast<int64_t>(value);
    return true;
  }
  return false;
}

bool any_number(const Option& option, double& out) {
  if (option.type == Option::Type::kInteger) {
    out = static_cast<double>(option.integer);
    return true;
  }
  if (option.type == Option::Type::kNumber) {
    out = option.number;
    return true;
  }
  return false;
}

const char* kBoardOptions[] = {"measure", "interval_seconds"};
const char* kGpioOptions[] = {"pin",          "active_high",    "bias",
                              "debounce_ms",  "settle_seconds", "event_kind"};
const char* kAdcOptions[] = {"pin", "output", "event_kind", "interval_seconds"};
const char* kOneWireOptions[] = {"pin", "device", "event_kind", "interval_seconds"};
// The same words the Linux agent takes for the same source, minus the adapter: a board
// with one radio has nothing to choose between.
// A microphone on this board reports that something was loud and nothing else. There is no
// `activity` among these, because there is nothing else it could be doing: the agent's
// microphone waits for the hub to ask for a stream, and this one has nowhere to put sound.
const char* kMicrophoneOptions[] = {"pin",
                                    "clock_pin",
                                    "channel",
                                    "activity_threshold_dbfs",
                                    "activity_min_seconds",
                                    "activity_hold_seconds",
                                    "event_kind"};
const char* kBleOptions[] = {"address",         "ibeacon_uuid",         "ibeacon_major",
                             "ibeacon_minor",   "rssi_min",             "enter_sightings",
                             "enter_window_seconds", "absent_after_seconds", "event_kind"};

bool hex_digit(char letter, uint8_t& value) {
  if (letter >= '0' && letter <= '9') {
    value = static_cast<uint8_t>(letter - '0');
    return true;
  }
  if (letter >= 'a' && letter <= 'f') {
    value = static_cast<uint8_t>(letter - 'a' + 10);
    return true;
  }
  return false;
}

bool known_option(const char* name, const char* const* known, size_t count) {
  for (size_t index = 0; index < count; ++index) {
    if (std::strcmp(name, known[index]) == 0) return true;
  }
  return false;
}

// How often a periodic source is read. Two drivers take it and it means the same thing in
// both: a reading taken every so often, whether the sensor is on the die or on a pin.
bool interval_of(const Source& source, const char* id, uint32_t& out, Unplanned& why,
                 char* detail, size_t capacity) {
  out = kDefaultBoardSeconds * 1000;
  const Option* interval = option_named(source, "interval_seconds");
  if (interval == nullptr) return true;
  int64_t seconds = 0;
  if (!whole_number(*interval, seconds)) {
    why = Unplanned::kWrongType;
    say(detail, capacity, "%s: interval_seconds is a whole number of seconds", id);
    return false;
  }
  if (seconds < static_cast<int64_t>(kMinBoardSeconds) ||
      seconds > static_cast<int64_t>(kMaxBoardSeconds)) {
    why = Unplanned::kOutOfRange;
    say(detail, capacity, "%s: interval_seconds is between %u and %u", id,
        static_cast<unsigned>(kMinBoardSeconds), static_cast<unsigned>(kMaxBoardSeconds));
    return false;
  }
  out = static_cast<uint32_t>(seconds) * 1000u;
  return true;
}

// What a source calls what it publishes, checked as a kind and copied where it belongs.
bool event_kind_of(const Source& source, const char* id, char* out, size_t capacity,
                   Unplanned& why, char* detail, size_t detail_capacity) {
  const Option* kind = option_named(source, "event_kind");
  if (kind == nullptr) return true;
  if (kind->type != Option::Type::kString || !is_kind(kind->text)) {
    why = Unplanned::kWrongType;
    say(detail, detail_capacity, "%s: event_kind is a family and a name, like sensor.contact",
        id);
    return false;
  }
  if (!copy_into(out, capacity, kind->text)) {
    why = Unplanned::kOutOfRange;
    say(detail, detail_capacity, "%s: that event_kind is longer than this node can carry", id);
    return false;
  }
  return true;
}

// How patient this source is: how many sightings make an arrival, in how long a window,
// how long a quiet is absence, and how weak a signal is still evidence. Every one of them
// has a default that works, so a source that names a device and nothing else is a source.
bool watchfulness_of(const Source& source, const char* id, Watchfulness& out, Unplanned& why,
                     char* detail, size_t capacity) {
  struct Number {
    const char* name;
    int64_t least;
    int64_t most;
    bool seconds;  // written in seconds, kept in milliseconds
    int64_t* into;
  };
  int64_t sightings = out.enter_sightings;
  int64_t window = out.enter_window_ms;
  int64_t absent = out.absent_after_ms;
  int64_t weakest = out.rssi_min;
  const Number numbers[] = {
      {"enter_sightings", 1, static_cast<int64_t>(kMaxEnterSightings), false, &sightings},
      {"enter_window_seconds", kMinEnterWindowMs / 1000, kMaxEnterWindowMs / 1000, true,
       &window},
      {"absent_after_seconds", kMinAbsentAfterMs / 1000, kMaxAbsentAfterMs / 1000, true,
       &absent},
      {"rssi_min", -127, 0, false, &weakest},
  };
  for (const Number& number : numbers) {
    const Option* option = option_named(source, number.name);
    if (option == nullptr) continue;
    int64_t value = 0;
    if (!whole_number(*option, value)) {
      why = Unplanned::kWrongType;
      say(detail, capacity, "%s: %s is a whole number", id, number.name);
      return false;
    }
    if (value < number.least || value > number.most) {
      why = Unplanned::kOutOfRange;
      say(detail, capacity, "%s: %s is between %lld and %lld", id, number.name,
          static_cast<long long>(number.least), static_cast<long long>(number.most));
      return false;
    }
    *number.into = number.seconds ? value * 1000 : value;
  }
  Watchfulness wanted;
  wanted.enter_sightings = static_cast<uint32_t>(sightings);
  wanted.enter_window_ms = static_cast<uint32_t>(window);
  wanted.absent_after_ms = static_cast<uint32_t>(absent);
  wanted.rssi_min = static_cast<int32_t>(weakest);
  // The watch is the authority on what it can survive; this only refuses earlier, with the
  // name of the source on it.
  Watch measuring;
  if (!measuring.configure(wanted)) {
    why = Unplanned::kOutOfRange;
    say(detail, capacity, "%s: those sightings and windows do not go together", id);
    return false;
  }
  out = wanted;
  return true;
}

// How loud, for how long, and how long a quiet ends it. Every one of them has the same
// default the agent's microphone has, so a source file written for a Pi and sent to a board
// behaves the same way for the same sound.
bool loudness_of(const Source& source, const char* id, Loudness& out, Unplanned& why,
                 char* detail, size_t capacity) {
  struct Number {
    const char* name;
    double least;
    double most;
    double* into;
  };
  Loudness wanted;
  const Number numbers[] = {
      {"activity_threshold_dbfs", kLeastThresholdDbfs, kMostThresholdDbfs,
       &wanted.threshold_dbfs},
      {"activity_min_seconds", kLeastMinSeconds, kMostMinSeconds, &wanted.min_seconds},
      {"activity_hold_seconds", kLeastHoldSeconds, kMostHoldSeconds, &wanted.hold_seconds},
  };
  for (const Number& number : numbers) {
    const Option* option = option_named(source, number.name);
    if (option == nullptr) continue;
    double value = 0.0;
    if (!any_number(*option, value)) {
      why = Unplanned::kWrongType;
      say(detail, capacity, "%s: %s is a number", id, number.name);
      return false;
    }
    if (!(value >= number.least) || !(value <= number.most)) {
      why = Unplanned::kOutOfRange;
      say(detail, capacity, "%s: %s is between %.1f and %.1f", id, number.name, number.least,
          number.most);
      return false;
    }
    *number.into = value;
  }
  // The detector is the authority on what it can survive; this refuses earlier, with the
  // name of the source on it.
  Activity listening;
  if (!listening.configure(wanted)) {
    why = Unplanned::kOutOfRange;
    say(detail, capacity, "%s: those levels and durations do not go together", id);
    return false;
  }
  out = wanted;
  return true;
}

// The one pin a source is on, or none. Two drivers here take a pin and the map does not
// care which: what it is protecting is the wire.
// The most pins one source holds: a microphone's data, clock and word select.
constexpr size_t kMostPins = 3;

// The pins a source holds, into `out`. Most hold one; a microphone holds three and a watch
// holds none.
size_t pins_of(const Planned& planned, int out[kMostPins]) {
  // A source that is in the list without being read holds no wire. That is what makes
  // `"enabled": false` a way to keep a source written down rather than a way to reserve a
  // pin nobody is using.
  if (!planned.enabled) return 0;
  if (planned.driver == Driver::kGpio) {
    out[0] = planned.gpio.pin;
    return 1;
  }
  if (planned.driver == Driver::kAdc) {
    out[0] = planned.adc.pin;
    return 1;
  }
  if (planned.driver == Driver::kOneWire) {
    out[0] = planned.onewire.pin;
    return 1;
  }
  if (planned.driver == Driver::kMicrophone) {
    out[0] = planned.microphone.pin;
    out[1] = planned.microphone.clock_pin;
    out[2] = planned.microphone.clock_pin + 1;
    return 3;
  }
  return 0;
}

}  // namespace

const char* name_of(Driver driver) {
  switch (driver) {
    case Driver::kBoard:
      return "board";
    case Driver::kGpio:
      return "gpio";
    case Driver::kAdc:
      return "adc";
    case Driver::kOneWire:
      return "onewire";
    case Driver::kBle:
      return "ble";
    case Driver::kMicrophone:
      return "microphone";
    case Driver::kNone:
      break;
  }
  return "none";
}

const char* name_of(Bias bias) {
  switch (bias) {
    case Bias::kPullUp:
      return "pull_up";
    case Bias::kPullDown:
      return "pull_down";
    case Bias::kNone:
      break;
  }
  return "disabled";
}

bool rom_code_of(const char* device, uint8_t rom[8]) {
  if (device == nullptr || std::strlen(device) != 15) return false;
  if (device[0] != '2' || device[1] != '8' || device[2] != '-') return false;
  uint8_t serial[6] = {};
  for (size_t index = 0; index < 6; ++index) {
    uint8_t high = 0;
    uint8_t low = 0;
    if (!hex_digit(device[3 + index * 2], high) || !hex_digit(device[4 + index * 2], low)) {
      return false;
    }
    serial[index] = static_cast<uint8_t>((high << 4) | low);
  }
  // The kernel prints the serial most significant byte first; the bus sends it the other
  // way round, and a MATCH ROM with the bytes in the printed order addresses nobody.
  rom[0] = 0x28;
  for (size_t index = 0; index < 6; ++index) rom[1 + index] = serial[5 - index];
  rom[7] = onewire_crc(rom, 7);
  return true;
}

Plan& Plan::operator=(const Plan& other) {
  if (this == &other) return *this;
  board_ = other.board_;
  count_ = other.count_;
  for (size_t index = 0; index < kMaxPlanned; ++index) planned_[index] = other.planned_[index];
  pins_.clear();
  for (size_t index = 0; index < count_; ++index) {
    int held[kMostPins] = {};
    const size_t how_many = pins_of(planned_[index], held);
    PinRefusal refusal = PinRefusal::kNone;
    bool all_of_them = true;
    // They were agreed to once, on the same board, so this cannot refuse; if it somehow
    // did, the plan would be running a pin its own map does not know about, so it is
    // dropped rather than kept.
    for (size_t at = 0; at < how_many && all_of_them; ++at) {
      all_of_them = pins_.claim(held[at], planned_[index].source_id, refusal);
    }
    if (!all_of_them) {
      count_ = index;
      break;
    }
  }
  return *this;
}

const char* name_of(Unplanned why) {
  switch (why) {
    case Unplanned::kNone:
      return "none";
    case Unplanned::kTooMany:
      return "too_many_sources";
    case Unplanned::kNotAName:
      return "not_a_name";
    case Unplanned::kRepeatedId:
      return "repeated_id";
    case Unplanned::kNoSuchDriver:
      return "no_such_driver";
    case Unplanned::kNoSuchOption:
      return "no_such_option";
    case Unplanned::kMissingOption:
      return "missing_option";
    case Unplanned::kWrongType:
      return "wrong_type";
    case Unplanned::kOutOfRange:
      return "out_of_range";
    case Unplanned::kPinRefused:
      return "pin_refused";
  }
  return "unknown";
}

Driver driver_of(const char* kind) {
  if (std::strcmp(kind, "board") == 0) return Driver::kBoard;
  if (std::strcmp(kind, "gpio") == 0) return Driver::kGpio;
  if (std::strcmp(kind, "adc") == 0) return Driver::kAdc;
  if (std::strcmp(kind, "onewire") == 0) return Driver::kOneWire;
  if (std::strcmp(kind, "ble") == 0) return Driver::kBle;
  if (std::strcmp(kind, "microphone") == 0) return Driver::kMicrophone;
  return Driver::kNone;
}

void Plan::clear() {
  pins_.clear();
  count_ = 0;
  for (Planned& one : planned_) one = Planned{};
}

bool Plan::take(const Source* sources, size_t count, Unplanned& why, char* detail,
                size_t capacity) {
  clear();
  why = Unplanned::kNone;
  if (detail != nullptr && capacity > 0) detail[0] = '\0';
  if (count > kMaxPlanned) {
    why = Unplanned::kTooMany;
    say(detail, capacity, "this board runs %u sources at once, not %u",
        static_cast<unsigned>(kMaxPlanned), static_cast<unsigned>(count));
    return false;
  }
  if (count > 0 && sources == nullptr) {
    why = Unplanned::kTooMany;
    return false;
  }

  for (size_t index = 0; index < count; ++index) {
    const Source& source = sources[index];
    Planned& planned = planned_[index];

    if (!is_name(source.id)) {
      why = Unplanned::kNotAName;
      say(detail, capacity, "a source needs a name this hub would recognise");
      clear();
      return false;
    }
    for (size_t before = 0; before < index; ++before) {
      if (std::strcmp(planned_[before].source_id, source.id) == 0) {
        why = Unplanned::kRepeatedId;
        say(detail, capacity, "%s is in this configuration twice", source.id);
        clear();
        return false;
      }
    }
    copy_into(planned.source_id, sizeof(planned.source_id), source.id);
    planned.driver = driver_of(source.kind);
    planned.enabled = true;

    if (planned.driver == Driver::kNone) {
      why = Unplanned::kNoSuchDriver;
      say(detail, capacity, "%s: this firmware has no %s driver", source.id, source.kind);
      clear();
      return false;
    }

    // `enabled` is a field of every source rather than an option of any driver: the hub's
    // own file spells it beside the driver's options, and a node that refused it would be
    // refusing the one way a configuration has of keeping a source without reading it.
    const Option* switched = option_named(source, "enabled");
    if (switched != nullptr) {
      if (switched->type != Option::Type::kBoolean) {
        why = Unplanned::kWrongType;
        say(detail, capacity, "%s: enabled is true or false", source.id);
        clear();
        return false;
      }
      planned.enabled = switched->boolean;
    }

    const char* const* known = kGpioOptions;
    size_t how_many = sizeof(kGpioOptions) / sizeof(kGpioOptions[0]);
    if (planned.driver == Driver::kBoard) {
      known = kBoardOptions;
      how_many = sizeof(kBoardOptions) / sizeof(kBoardOptions[0]);
    } else if (planned.driver == Driver::kAdc) {
      known = kAdcOptions;
      how_many = sizeof(kAdcOptions) / sizeof(kAdcOptions[0]);
    } else if (planned.driver == Driver::kOneWire) {
      known = kOneWireOptions;
      how_many = sizeof(kOneWireOptions) / sizeof(kOneWireOptions[0]);
    } else if (planned.driver == Driver::kBle) {
      known = kBleOptions;
      how_many = sizeof(kBleOptions) / sizeof(kBleOptions[0]);
    } else if (planned.driver == Driver::kMicrophone) {
      known = kMicrophoneOptions;
      how_many = sizeof(kMicrophoneOptions) / sizeof(kMicrophoneOptions[0]);
    }
    for (size_t at = 0; at < source.option_count; ++at) {
      if (std::strcmp(source.options[at].name, "enabled") == 0) continue;
      if (!known_option(source.options[at].name, known, how_many)) {
        why = Unplanned::kNoSuchOption;
        say(detail, capacity, "%s: a %s source has no %s", source.id, source.kind,
            source.options[at].name);
        clear();
        return false;
      }
    }

    if (planned.driver == Driver::kBoard) {
      copy_into(planned.board.measure, sizeof(planned.board.measure), "temperature");
      const Option* measure = option_named(source, "measure");
      if (measure != nullptr) {
        if (measure->type != Option::Type::kString) {
          why = Unplanned::kWrongType;
          say(detail, capacity, "%s: measure is the name of a measurement", source.id);
          clear();
          return false;
        }
        if (std::strcmp(measure->text, "temperature") != 0) {
          why = Unplanned::kOutOfRange;
          say(detail, capacity, "%s: this chip can measure its temperature, not its %s",
              source.id, measure->text);
          clear();
          return false;
        }
      }
      if (!interval_of(source, source.id, planned.board.interval_ms, why, detail, capacity)) {
        clear();
        return false;
      }
      ++count_;
      continue;
    }

    // microphone, which is three pins and a promise about what leaves the board.
    if (planned.driver == Driver::kMicrophone) {
      // One microphone. There is one state machine clocking I²S on this board and one
      // block of samples behind it; a second source would quietly share the first one's
      // clock and read the first one's pin, and report a room it is not listening to.
      for (size_t before = 0; planned.enabled && before < index; ++before) {
        if (planned_[before].driver != Driver::kMicrophone || !planned_[before].enabled) {
          continue;
        }
        why = Unplanned::kTooMany;
        say(detail, capacity, "%s: this board listens to one microphone, and %s is already it",
            source.id, planned_[before].source_id);
        clear();
        return false;
      }
      copy_into(planned.microphone.event_kind, sizeof(planned.microphone.event_kind),
                "audio.activity");
      struct Wire {
        const char* name;
        int* into;
      };
      const Wire wires[] = {{"pin", &planned.microphone.pin},
                            {"clock_pin", &planned.microphone.clock_pin}};
      for (const Wire& wire : wires) {
        const Option* named = option_named(source, wire.name);
        if (named == nullptr) {
          why = Unplanned::kMissingOption;
          say(detail, capacity, "%s: a microphone source is %s, and this one names none",
              source.id, wire.name);
          clear();
          return false;
        }
        int64_t number = 0;
        if (!whole_number(*named, number)) {
          why = Unplanned::kWrongType;
          say(detail, capacity, "%s: %s is a GPIO number", source.id, wire.name);
          clear();
          return false;
        }
        *wire.into = static_cast<int>(number);
      }
      // The word select is the pin above the clock, which is what PIO's side-set can drive
      // in one write. Said here rather than left to the pin map, which would refuse the
      // number without saying where it came from.
      if (planned.microphone.clock_pin + 1 > kMaxGpio) {
        why = Unplanned::kOutOfRange;
        say(detail, capacity, "%s: the word select is GPIO %d, which is not a pin", source.id,
            planned.microphone.clock_pin + 1);
        clear();
        return false;
      }

      const Option* channel = option_named(source, "channel");
      if (channel != nullptr) {
        if (channel->type != Option::Type::kString) {
          why = Unplanned::kWrongType;
          say(detail, capacity, "%s: channel is left or right", source.id);
          clear();
          return false;
        }
        if (std::strcmp(channel->text, "right") == 0) {
          planned.microphone.left = false;
        } else if (std::strcmp(channel->text, "left") != 0) {
          why = Unplanned::kOutOfRange;
          say(detail, capacity, "%s: channel is left or right, not %s", source.id,
              channel->text);
          clear();
          return false;
        }
      }

      if (!loudness_of(source, source.id, planned.microphone.how, why, detail, capacity) ||
          !event_kind_of(source, source.id, planned.microphone.event_kind,
                         sizeof(planned.microphone.event_kind), why, detail, capacity)) {
        clear();
        return false;
      }

      const int held[] = {planned.microphone.pin, planned.microphone.clock_pin,
                          planned.microphone.clock_pin + 1};
      for (const int wire : held) {
        PinRefusal refusal = PinRefusal::kNone;
        if (planned.enabled ? pins_.claim(wire, planned.source_id, refusal)
                            : pins_.allows(wire, refusal)) {
          continue;
        }
        why = Unplanned::kPinRefused;
        switch (refusal) {
          case PinRefusal::kOutOfRange:
            say(detail, capacity, "%s: GPIO %d is not a pin on this board", source.id, wire);
            break;
          case PinRefusal::kReserved:
            say(detail, capacity, "%s: GPIO %d belongs to the board itself", source.id, wire);
            break;
          case PinRefusal::kTaken:
            say(detail, capacity, "%s and %s both use GPIO %d", source.id, pins_.holder(wire),
                wire);
            break;
          case PinRefusal::kNone:
            break;
        }
        clear();
        return false;
      }
      ++count_;
      continue;
    }

    // ble, which is one named device and a long argument about what "here" means.
    if (planned.driver == Driver::kBle) {
      // The driver exists in this build; the antenna does not. Said as "no such driver"
      // because on this board there is none, and the hub's catalogue says the same.
      if (!has_radio(board_)) {
        why = Unplanned::kNoSuchDriver;
        say(detail, capacity, "%s: ble needs a board with a radio, and this one has none",
            source.id);
        clear();
        return false;
      }
      copy_into(planned.ble.event_kind, sizeof(planned.ble.event_kind), "presence.state");
      const Option* address = option_named(source, "address");
      const Option* beacon = option_named(source, "ibeacon_uuid");
      if ((address == nullptr) == (beacon == nullptr)) {
        why = Unplanned::kMissingOption;
        say(detail, capacity, "%s: a device is watched by its address or by its iBeacon",
            source.id);
        clear();
        return false;
      }
      if (address != nullptr) {
        if (address->type != Option::Type::kString ||
            !read_address(address->text, planned.ble.watched.address)) {
          why = Unplanned::kWrongType;
          say(detail, capacity, "%s: address is AA:BB:CC:DD:EE:FF, in capitals", source.id);
          clear();
          return false;
        }
        planned.ble.watched.by_address = true;
      } else {
        if (beacon->type != Option::Type::kString ||
            !read_uuid(beacon->text, planned.ble.watched.uuid)) {
          why = Unplanned::kWrongType;
          say(detail, capacity, "%s: ibeacon_uuid is a lowercase uuid with its hyphens",
              source.id);
          clear();
          return false;
        }
        planned.ble.watched.by_beacon = true;
      }
      // A major or a minor is a part of a beacon. On a source watching an address they
      // would be two claims about different things, and the second would be ignored.
      static const char* kBeaconOnly[] = {"ibeacon_major", "ibeacon_minor"};
      for (const char* only : kBeaconOnly) {
        const Option* part = option_named(source, only);
        if (part == nullptr) continue;
        if (planned.ble.watched.by_address) {
          why = Unplanned::kNoSuchOption;
          say(detail, capacity, "%s: %s is part of an iBeacon, and this one watches an address",
              source.id, only);
          clear();
          return false;
        }
        int64_t number = 0;
        if (!whole_number(*part, number) || number < 0 || number > 65535) {
          why = Unplanned::kOutOfRange;
          say(detail, capacity, "%s: %s is between 0 and 65535", source.id, only);
          clear();
          return false;
        }
        if (std::strcmp(only, "ibeacon_major") == 0) {
          planned.ble.watched.major = static_cast<int32_t>(number);
        } else {
          planned.ble.watched.minor = static_cast<int32_t>(number);
        }
      }
      if (!watchfulness_of(source, source.id, planned.ble.how, why, detail, capacity) ||
          !event_kind_of(source, source.id, planned.ble.event_kind,
                         sizeof(planned.ble.event_kind), why, detail, capacity)) {
        clear();
        return false;
      }
      ++count_;
      continue;
    }

    // adc, which is a pin with a converter behind it and a number that is not lux.
    if (planned.driver == Driver::kAdc) {
      copy_into(planned.adc.event_kind, sizeof(planned.adc.event_kind), "light.level");
      const Option* pin = option_named(source, "pin");
      if (pin == nullptr) {
        why = Unplanned::kMissingOption;
        say(detail, capacity, "%s: an adc source is a pin, and this one names none", source.id);
        clear();
        return false;
      }
      int64_t number = 0;
      if (!whole_number(*pin, number)) {
        why = Unplanned::kWrongType;
        say(detail, capacity, "%s: pin is a GPIO number", source.id);
        clear();
        return false;
      }
      if (number < kFirstAdcPin || number > kLastAdcPin) {
        // Refused here rather than by the pin map: GPIO 15 is a pin on this board, it just
        // has no converter on it, and "not a pin" would send somebody looking at the wrong
        // thing.
        why = Unplanned::kOutOfRange;
        say(detail, capacity, "%s: the converter is on GPIO %d to %d, not on GPIO %lld",
            source.id, kFirstAdcPin, kLastAdcPin, static_cast<long long>(number));
        clear();
        return false;
      }
      planned.adc.pin = static_cast<int>(number);

      const Option* output = option_named(source, "output");
      if (output != nullptr) {
        if (output->type != Option::Type::kString) {
          why = Unplanned::kWrongType;
          say(detail, capacity, "%s: output is ratio or volts", source.id);
          clear();
          return false;
        }
        if (std::strcmp(output->text, "volts") == 0) {
          planned.adc.volts = true;
        } else if (std::strcmp(output->text, "ratio") != 0) {
          why = Unplanned::kOutOfRange;
          say(detail, capacity, "%s: output is ratio or volts, not %s", source.id,
              output->text);
          clear();
          return false;
        }
      }

      if (!interval_of(source, source.id, planned.adc.interval_ms, why, detail, capacity) ||
          !event_kind_of(source, source.id, planned.adc.event_kind,
                         sizeof(planned.adc.event_kind), why, detail, capacity)) {
        clear();
        return false;
      }

      PinRefusal refusal = PinRefusal::kNone;
      if (!(planned.enabled ? pins_.claim(planned.adc.pin, planned.source_id, refusal)
                            : pins_.allows(planned.adc.pin, refusal))) {
        why = Unplanned::kPinRefused;
        if (refusal == PinRefusal::kTaken) {
          say(detail, capacity, "%s and %s both use GPIO %d", source.id,
              pins_.holder(planned.adc.pin), planned.adc.pin);
        } else {
          say(detail, capacity, "%s: GPIO %d belongs to the board itself", source.id,
              planned.adc.pin);
        }
        clear();
        return false;
      }
      ++count_;
      continue;
    }

    // onewire, which is a probe on a pin and possibly one of several on it.
    if (planned.driver == Driver::kOneWire) {
      copy_into(planned.onewire.event_kind, sizeof(planned.onewire.event_kind),
                "climate.temperature");
      const Option* pin = option_named(source, "pin");
      if (pin == nullptr) {
        why = Unplanned::kMissingOption;
        say(detail, capacity, "%s: a onewire source is a pin, and this one names none",
            source.id);
        clear();
        return false;
      }
      int64_t number = 0;
      if (!whole_number(*pin, number)) {
        why = Unplanned::kWrongType;
        say(detail, capacity, "%s: pin is a GPIO number", source.id);
        clear();
        return false;
      }
      planned.onewire.pin = static_cast<int>(number);

      const Option* device = option_named(source, "device");
      if (device != nullptr) {
        if (device->type != Option::Type::kString) {
          why = Unplanned::kWrongType;
          say(detail, capacity, "%s: device is a probe id like 28-0123456789ab", source.id);
          clear();
          return false;
        }
        if (!rom_code_of(device->text, planned.onewire.rom)) {
          why = Unplanned::kOutOfRange;
          say(detail, capacity, "%s: %s is not a DS18B20 id (28-0123456789ab)", source.id,
              device->text);
          clear();
          return false;
        }
        planned.onewire.has_rom = true;
        copy_into(planned.onewire.device, sizeof(planned.onewire.device), device->text);
      }

      if (!interval_of(source, source.id, planned.onewire.interval_ms, why, detail, capacity) ||
          !event_kind_of(source, source.id, planned.onewire.event_kind,
                         sizeof(planned.onewire.event_kind), why, detail, capacity)) {
        clear();
        return false;
      }

      PinRefusal refusal = PinRefusal::kNone;
      if (!(planned.enabled ? pins_.claim(planned.onewire.pin, planned.source_id, refusal)
                            : pins_.allows(planned.onewire.pin, refusal))) {
        why = Unplanned::kPinRefused;
        switch (refusal) {
          case PinRefusal::kOutOfRange:
            say(detail, capacity, "%s: GPIO %d is not a pin on this board", source.id,
                planned.onewire.pin);
            break;
          case PinRefusal::kReserved:
            say(detail, capacity, "%s: GPIO %d belongs to the board itself", source.id,
                planned.onewire.pin);
            break;
          case PinRefusal::kTaken:
            say(detail, capacity, "%s and %s both use GPIO %d", source.id,
                pins_.holder(planned.onewire.pin), planned.onewire.pin);
            break;
          case PinRefusal::kNone:
            break;
        }
        clear();
        return false;
      }
      ++count_;
      continue;
    }

    // gpio, which is a wire and everything anybody could get wrong about one.
    copy_into(planned.gpio.event_kind, sizeof(planned.gpio.event_kind), "sensor.motion");
    InputConfig input;

    const Option* pin = option_named(source, "pin");
    if (pin == nullptr) {
      why = Unplanned::kMissingOption;
      say(detail, capacity, "%s: a gpio source is a pin, and this one names none", source.id);
      clear();
      return false;
    }
    int64_t number = 0;
    if (!whole_number(*pin, number)) {
      why = Unplanned::kWrongType;
      say(detail, capacity, "%s: pin is a GPIO number", source.id);
      clear();
      return false;
    }
    planned.gpio.pin = static_cast<int>(number);

    const Option* polarity = option_named(source, "active_high");
    if (polarity != nullptr) {
      if (polarity->type != Option::Type::kBoolean) {
        why = Unplanned::kWrongType;
        say(detail, capacity, "%s: active_high is true or false", source.id);
        clear();
        return false;
      }
      input.active_high = polarity->boolean;
    }

    const Option* bias = option_named(source, "bias");
    planned.gpio.bias = Bias::kNone;
    if (bias != nullptr) {
      if (bias->type != Option::Type::kString) {
        why = Unplanned::kWrongType;
        say(detail, capacity, "%s: bias is pull_up, pull_down or disabled", source.id);
        clear();
        return false;
      }
      if (std::strcmp(bias->text, "pull_up") == 0) {
        planned.gpio.bias = Bias::kPullUp;
      } else if (std::strcmp(bias->text, "pull_down") == 0) {
        planned.gpio.bias = Bias::kPullDown;
      } else if (std::strcmp(bias->text, "disabled") != 0) {
        why = Unplanned::kOutOfRange;
        say(detail, capacity, "%s: bias is pull_up, pull_down or disabled, not %s", source.id,
            bias->text);
        clear();
        return false;
      }
    }

    const Option* debounce = option_named(source, "debounce_ms");
    if (debounce != nullptr) {
      int64_t milliseconds = 0;
      if (!whole_number(*debounce, milliseconds)) {
        why = Unplanned::kWrongType;
        say(detail, capacity, "%s: debounce_ms is a whole number of milliseconds", source.id);
        clear();
        return false;
      }
      if (milliseconds < 0 || milliseconds > static_cast<int64_t>(kMaxDebounceMs)) {
        why = Unplanned::kOutOfRange;
        say(detail, capacity, "%s: debounce_ms is between 0 and %u", source.id,
            static_cast<unsigned>(kMaxDebounceMs));
        clear();
        return false;
      }
      input.debounce_ms = static_cast<uint32_t>(milliseconds);
    }

    const Option* settle = option_named(source, "settle_seconds");
    if (settle != nullptr) {
      double seconds = 0.0;
      if (!any_number(*settle, seconds)) {
        why = Unplanned::kWrongType;
        say(detail, capacity, "%s: settle_seconds is a number of seconds", source.id);
        clear();
        return false;
      }
      if (!(seconds >= 0.0) || seconds * 1000.0 > static_cast<double>(kMaxSettleMs)) {
        why = Unplanned::kOutOfRange;
        say(detail, capacity, "%s: settle_seconds is between 0 and %u", source.id,
            static_cast<unsigned>(kMaxSettleMs / 1000));
        clear();
        return false;
      }
      input.settle_ms = static_cast<uint32_t>(seconds * 1000.0);
    }

    if (!event_kind_of(source, source.id, planned.gpio.event_kind,
                       sizeof(planned.gpio.event_kind), why, detail, capacity)) {
      clear();
      return false;
    }

    planned.gpio.input = input;

    PinRefusal refusal = PinRefusal::kNone;
    if (!(planned.enabled ? pins_.claim(planned.gpio.pin, planned.source_id, refusal)
                          : pins_.allows(planned.gpio.pin, refusal))) {
      why = Unplanned::kPinRefused;
      switch (refusal) {
        case PinRefusal::kOutOfRange:
          say(detail, capacity, "%s: GPIO %d is not a pin on this board", source.id,
              planned.gpio.pin);
          break;
        case PinRefusal::kReserved:
          say(detail, capacity, "%s: GPIO %d belongs to the board itself", source.id,
              planned.gpio.pin);
          break;
        case PinRefusal::kTaken:
          say(detail, capacity, "%s and %s both use GPIO %d", source.id,
              pins_.holder(planned.gpio.pin), planned.gpio.pin);
          break;
        case PinRefusal::kNone:
          break;
      }
      clear();
      return false;
    }
    ++count_;
  }
  return true;
}

}  // namespace sentry
