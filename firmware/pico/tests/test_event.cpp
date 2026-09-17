// What this node may say happened, and what it must refuse to say.

#include <cstring>
#include <string>

#include "harness.h"
#include "sentry/event.h"

using sentry::Clock;
using sentry::Delivery;
using sentry::Quality;
using sentry::Reading;
using sentry::Value;
using sentry::write_event;

namespace {

Reading a_reading() {
  Reading reading;
  reading.event_id = "0f6c4a1e-9a5b-4c2d-8e11-5b7c9d0a1f23";
  reading.node_id = "pico-ingresso";
  reading.source_id = "pir-1";
  reading.boot_id = "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611";
  reading.sequence = 41;
  reading.kind = "sensor.motion";
  reading.occurred_at = "2026-09-16T21:04:07.512Z";
  reading.clock = Clock::kSynced;
  reading.value = Value::of(true);
  reading.quality = Quality::kValid;
  return reading;
}

Delivery a_delivery() {
  Delivery delivery;
  delivery.connection_id = "9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb";
  delivery.hub_epoch = 7;
  return delivery;
}

}  // namespace

TEST(an_event_is_written_exactly_as_the_contract_spells_it) {
  char buffer[sentry::kMaxEventBytes];
  size_t size = write_event(a_reading(), a_delivery(), buffer, sizeof(buffer));
  CHECK(size > 0);
  std::string written(buffer, size);
  CHECK_TEXT(
      written.c_str(),
      R"({"schema_version":1,"event":{"event_id":"0f6c4a1e-9a5b-4c2d-8e11-5b7c9d0a1f23",)"
      R"("node_id":"pico-ingresso","source_id":"pir-1",)"
      R"("boot_id":"2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611","sequence":41,)"
      R"("kind":"sensor.motion","occurred_at":"2026-09-16T21:04:07.512Z",)"
      R"("clock_status":"synced","value":true,"unit":null,"quality":"valid"},)"
      R"("delivery":{"connection_id":"9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb","hub_epoch":7,)"
      R"("grant_id":null,"queued_ms":0,"replayed":false,"initial_state":false}})");
}

TEST(a_measurement_carries_its_unit_and_its_decimals) {
  Reading reading = a_reading();
  reading.kind = "sensor.temperature";
  reading.value = Value::of(42.25, 1);
  reading.unit = "\xc2\xb0"
                 "C";
  char buffer[sentry::kMaxEventBytes];
  size_t size = write_event(reading, a_delivery(), buffer, sizeof(buffer));
  CHECK(size > 0);
  std::string written(buffer, size);
  CHECK(written.find(R"("value":42.3)") != std::string::npos);
  CHECK(written.find("\"unit\":\"\xc2\xb0"
                     "C\"") != std::string::npos);
}

TEST(a_reading_with_no_value_is_not_a_reading_of_zero) {
  Reading reading = a_reading();
  reading.value = Value{};
  reading.quality = Quality::kUnavailable;
  char buffer[sentry::kMaxEventBytes];
  size_t size = write_event(reading, a_delivery(), buffer, sizeof(buffer));
  CHECK(size > 0);
  std::string written(buffer, size);
  CHECK(written.find(R"("value":null)") != std::string::npos);
  CHECK(written.find(R"("quality":"unavailable")") != std::string::npos);
}

TEST(how_it_was_delivered_is_written_beside_what_happened_not_inside_it) {
  Delivery delivery = a_delivery();
  delivery.grant_id = "025a1b1f-2969-48c2-b14e-079c8b955f7c";
  delivery.queued_ms = 4200;
  delivery.replayed = true;
  delivery.initial_state = false;
  char buffer[sentry::kMaxEventBytes];
  size_t size = write_event(a_reading(), delivery, buffer, sizeof(buffer));
  CHECK(size > 0);
  std::string written(buffer, size);
  CHECK(written.find(R"("queued_ms":4200,"replayed":true)") != std::string::npos);
  CHECK(written.find(R"("sequence":41)") != std::string::npos);
}

TEST(an_event_the_hub_would_refuse_is_never_written_at_all) {
  struct Case {
    const char* why;
    Reading reading;
  };
  Reading not_a_uuid = a_reading();
  not_a_uuid.event_id = "41";
  Reading joined = a_reading();
  joined.source_id = "pico-ingresso.pir-1";  // the hub joins the names, a node never does
  Reading no_family = a_reading();
  no_family.kind = "motion";
  Reading no_zone = a_reading();
  no_zone.occurred_at = "2026-09-16T21:04:07";  // a time zone that is a guess
  Reading backwards = a_reading();
  backwards.sequence = -1;
  const Case cases[] = {
      {"an identifier that is not a uuid", not_a_uuid},
      {"a source claimed on another node", joined},
      {"a kind without a family", no_family},
      {"a timestamp without an offset", no_zone},
      {"a sequence that counts backwards", backwards},
  };
  for (const Case& one : cases) {
    char buffer[sentry::kMaxEventBytes];
    std::memset(buffer, 'x', sizeof(buffer));
    if (write_event(one.reading, a_delivery(), buffer, sizeof(buffer)) != 0) {
      std::printf("    %s was written\n", one.why);
      CHECK(false);
    }
  }
}

TEST(an_event_that_does_not_fit_is_refused_rather_than_cut_in_half) {
  char buffer[64];
  CHECK(write_event(a_reading(), a_delivery(), buffer, sizeof(buffer)) == 0);
}

TEST(the_time_is_written_the_way_the_contract_reads_it) {
  struct Case {
    int64_t unix_ms;
    const char* text;
  };
  const Case cases[] = {
      {0, "1970-01-01T00:00:00.000Z"},
      {1789592647512, "2026-09-16T21:04:07.512Z"},
      {1709164800000, "2024-02-29T00:00:00.000Z"},  // a leap day, which is where these break
  };
  for (const Case& one : cases) {
    char text[32] = {};
    CHECK(sentry::write_timestamp(one.unix_ms, text, sizeof(text)));
    CHECK_TEXT(text, one.text);
  }
  char small[8] = {};
  CHECK(!sentry::write_timestamp(0, small, sizeof(small)));
  char text[32] = {};
  CHECK(!sentry::write_timestamp(-1, text, sizeof(text)));
}

TEST(every_word_this_node_can_say_is_a_word_the_published_schema_knows) {
  // The vocabulary lives in the hub's models and is copied into an enum here. A word that
  // was invented on this side would pass every test above and be refused on arrival.
  std::string published = harness::slurp(SENTRY_CONTRACTS_DIR "/event.schema.json");
  CHECK(!published.empty());
  const Quality qualities[] = {Quality::kValid, Quality::kDegraded, Quality::kUnavailable,
                               Quality::kUnknown};
  for (Quality quality : qualities) {
    std::string quoted = std::string("\"") + sentry::name_of(quality) + "\"";
    if (published.find(quoted) == std::string::npos) {
      std::printf("    %s is not in the schema\n", sentry::name_of(quality));
      CHECK(false);
    }
  }
  const Clock clocks[] = {Clock::kSynced, Clock::kUnsynced, Clock::kUnknown};
  for (Clock clock : clocks) {
    std::string quoted = std::string("\"") + sentry::name_of(clock) + "\"";
    if (published.find(quoted) == std::string::npos) {
      std::printf("    %s is not in the schema\n", sentry::name_of(clock));
      CHECK(false);
    }
  }
}

int main() { return harness::run_all("event"); }
