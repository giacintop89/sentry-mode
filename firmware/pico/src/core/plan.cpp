#include "sentry/plan.h"

#include <cstdarg>
#include <cstdio>
#include <cstring>

#include "sentry/names.h"

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

// The one pin a source is on, or none. Two drivers here take a pin and the map does not
// care which: what it is protecting is the wire.
int pin_of(const Planned& planned) {
  if (planned.driver == Driver::kGpio) return planned.gpio.pin;
  if (planned.driver == Driver::kAdc) return planned.adc.pin;
  return -1;
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

Plan& Plan::operator=(const Plan& other) {
  if (this == &other) return *this;
  board_ = other.board_;
  count_ = other.count_;
  for (size_t index = 0; index < kMaxPlanned; ++index) planned_[index] = other.planned_[index];
  pins_.clear();
  for (size_t index = 0; index < count_; ++index) {
    const int pin = pin_of(planned_[index]);
    if (pin < 0) continue;
    PinRefusal refusal = PinRefusal::kNone;
    // It was agreed to once, on the same board, so this cannot refuse; if it somehow did,
    // the plan would be running a pin its own map does not know about, so it is dropped.
    if (!pins_.claim(pin, planned_[index].source_id, refusal)) {
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

    const char* const* known = kGpioOptions;
    size_t how_many = sizeof(kGpioOptions) / sizeof(kGpioOptions[0]);
    if (planned.driver == Driver::kBoard) {
      known = kBoardOptions;
      how_many = sizeof(kBoardOptions) / sizeof(kBoardOptions[0]);
    } else if (planned.driver == Driver::kAdc) {
      known = kAdcOptions;
      how_many = sizeof(kAdcOptions) / sizeof(kAdcOptions[0]);
    }
    for (size_t at = 0; at < source.option_count; ++at) {
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
      if (!pins_.claim(planned.adc.pin, planned.source_id, refusal)) {
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
    if (!pins_.claim(planned.gpio.pin, planned.source_id, refusal)) {
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
