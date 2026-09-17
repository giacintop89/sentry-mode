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

bool known_option(const char* name, const char* const* known, size_t count) {
  for (size_t index = 0; index < count; ++index) {
    if (std::strcmp(name, known[index]) == 0) return true;
  }
  return false;
}

}  // namespace

const char* name_of(Driver driver) {
  switch (driver) {
    case Driver::kBoard:
      return "board";
    case Driver::kGpio:
      return "gpio";
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
    if (planned_[index].driver != Driver::kGpio) continue;
    PinRefusal refusal = PinRefusal::kNone;
    // It was agreed to once, on the same board, so this cannot refuse; if it somehow did,
    // the plan would be running a pin its own map does not know about, so it is dropped.
    if (!pins_.claim(planned_[index].gpio.pin, planned_[index].source_id, refusal)) {
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

    const char* const* known = planned.driver == Driver::kBoard ? kBoardOptions : kGpioOptions;
    const size_t how_many = planned.driver == Driver::kBoard
                                ? sizeof(kBoardOptions) / sizeof(kBoardOptions[0])
                                : sizeof(kGpioOptions) / sizeof(kGpioOptions[0]);
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
      planned.board.interval_ms = kDefaultBoardSeconds * 1000;
      const Option* interval = option_named(source, "interval_seconds");
      if (interval != nullptr) {
        int64_t seconds = 0;
        if (!whole_number(*interval, seconds)) {
          why = Unplanned::kWrongType;
          say(detail, capacity, "%s: interval_seconds is a whole number of seconds",
              source.id);
          clear();
          return false;
        }
        if (seconds < static_cast<int64_t>(kMinBoardSeconds) ||
            seconds > static_cast<int64_t>(kMaxBoardSeconds)) {
          why = Unplanned::kOutOfRange;
          say(detail, capacity, "%s: interval_seconds is between %u and %u", source.id,
              static_cast<unsigned>(kMinBoardSeconds), static_cast<unsigned>(kMaxBoardSeconds));
          clear();
          return false;
        }
        planned.board.interval_ms = static_cast<uint32_t>(seconds) * 1000u;
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

    const Option* kind = option_named(source, "event_kind");
    if (kind != nullptr) {
      if (kind->type != Option::Type::kString || !is_kind(kind->text)) {
        why = Unplanned::kWrongType;
        say(detail, capacity, "%s: event_kind is a family and a name, like sensor.contact",
            source.id);
        clear();
        return false;
      }
      if (!copy_into(planned.gpio.event_kind, sizeof(planned.gpio.event_kind), kind->text)) {
        why = Unplanned::kOutOfRange;
        say(detail, capacity, "%s: that event_kind is longer than this node can carry",
            source.id);
        clear();
        return false;
      }
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
