// What the reader must refuse, and what the writer must never produce.

#include <cstring>

#include "harness.h"
#include "sentry/json.h"

using sentry::Kind;
using sentry::Reader;
using sentry::Span;
using sentry::Writer;

namespace {

bool walks(const char* document) {
  Reader reader(document, std::strlen(document));
  if (!reader.begin_object()) return false;
  Span key;
  Kind kind = Kind::kEnd;
  while (reader.member(key, kind)) {
    if (kind == Kind::kEnd) return true;
    if (!reader.skip()) return false;
  }
  return false;
}

}  // namespace

TEST(an_object_of_plain_members_is_read) {
  const char* document = R"({"a":1,"b":"two","c":true,"d":null,"e":-3.5})";
  Reader reader(document, std::strlen(document));
  CHECK(reader.begin_object());
  Span key;
  Kind kind = Kind::kEnd;
  int64_t whole = 0;
  char text[8] = {};
  size_t length = 0;
  bool flag = false;
  double fraction = 0;

  CHECK(reader.member(key, kind) && key.is("a") && kind == Kind::kNumber);
  CHECK(reader.integer(whole) && whole == 1);
  CHECK(reader.member(key, kind) && key.is("b") && kind == Kind::kString);
  CHECK(reader.string(text, sizeof(text), length) && length == 3);
  CHECK(reader.member(key, kind) && key.is("c") && kind == Kind::kTrue);
  CHECK(reader.boolean(flag) && flag);
  CHECK(reader.member(key, kind) && key.is("d") && kind == Kind::kNull);
  CHECK(reader.null());
  CHECK(reader.member(key, kind) && key.is("e") && kind == Kind::kNumber);
  CHECK(reader.number(fraction) && fraction < -3.4 && fraction > -3.6);
  CHECK(reader.member(key, kind) && kind == Kind::kEnd);
}

TEST(the_same_key_twice_is_refused) {
  // Whoever sent the second one would otherwise decide what this node does.
  CHECK(!walks(R"({"port":8555,"port":22})"));
}

TEST(a_key_repeated_in_a_nested_object_is_refused_too) {
  CHECK(!walks(R"({"sources":[{"id":"pir-1","id":"door-1"}]})"));
}

TEST(the_same_key_in_two_different_objects_is_not_a_repeat) {
  CHECK(walks(R"({"a":{"id":"one"},"b":{"id":"two"},"id":"three"})"));
}

TEST(nesting_deeper_than_the_contract_is_refused) {
  CHECK(!walks(R"({"a":{"b":{"c":{"d":{"e":1}}}}})"));
}

TEST(a_document_that_stops_in_the_middle_is_refused) {
  CHECK(!walks(R"({"a":1,"b":)"));
  CHECK(!walks(R"({"a":"unterminated)"));
  CHECK(!walks("{"));
  CHECK(!walks(""));
}

TEST(a_string_longer_than_its_field_is_refused_rather_than_cut) {
  const char* document = R"({"zone":"entrance"})";
  Reader reader(document, std::strlen(document));
  Span key;
  Kind kind = Kind::kEnd;
  CHECK(reader.begin_object());
  CHECK(reader.member(key, kind));
  char small[4] = {};
  size_t length = 0;
  CHECK(!reader.string(small, sizeof(small), length));
}

TEST(an_escape_is_read_and_an_invalid_one_is_not) {
  // The hub writes JSON with ensure_ascii, so anything that is not plain ASCII arrives as
  // \uXXXX. A node that refused those would be refusing the name of a room with an accent.
  const char* document = "{\"a\":\"line\\nbreak \\u00b0C \\ud83d\\ude00\"}";
  Reader reader(document, std::strlen(document));
  Span key;
  Kind kind = Kind::kEnd;
  char text[32] = {};
  size_t length = 0;
  CHECK(reader.begin_object());
  CHECK(reader.member(key, kind));
  CHECK(reader.string(text, sizeof(text), length));
  CHECK(std::strstr(text, "line\nbreak") != nullptr);
  CHECK(std::strstr(text, "\xc2\xb0" "C") != nullptr);
  CHECK(std::strstr(text, "\xf0\x9f\x98\x80") != nullptr);

  const char* bad[] = {
      "{\"a\":\"\\q\"}",        // an escape nobody agreed on
      "{\"a\":\"\\u00\"}",      // half of a code point
      "{\"a\":\"\\ud83d\"}",    // a high surrogate with nothing after it
      "{\"a\":\"\\udc00x\"}",   // a low surrogate on its own
  };
  for (const char* one : bad) {
    Reader again(one, std::strlen(one));
    Span ignored;
    Kind found = Kind::kEnd;
    char out[16] = {};
    size_t got = 0;
    CHECK(again.begin_object());
    CHECK(again.member(ignored, found));
    CHECK(!again.string(out, sizeof(out), got));
  }
}

TEST(a_raw_control_byte_in_a_string_is_refused) {
  // In a key it is refused on the way past, because a key is read to know what follows.
  const char in_key[] = {'{', '"', 0x01, '"', ':', '1', '}'};
  Reader keyed(in_key, sizeof(in_key));
  Span key;
  Kind kind = Kind::kEnd;
  CHECK(keyed.begin_object());
  CHECK(!keyed.member(key, kind));

  // In a value it is refused when the value is read, which is the first time anyone asks.
  const char in_value[] = {'{', '"', 'a', '"', ':', '"', 0x01, '"', '}'};
  Reader valued(in_value, sizeof(in_value));
  char out[8] = {};
  size_t length = 0;
  CHECK(valued.begin_object());
  CHECK(valued.member(key, kind) && kind == Kind::kString);
  CHECK(!valued.string(out, sizeof(out), length));
}

TEST(the_whitespace_a_hub_may_send_survives_but_a_nul_does_not) {
  // \n arrives escaped and means a line break in someone's name; \u0000 arrives escaped
  // and would end the text early for everything downstream that reads it as a C string.
  const char* kept = "{\"a\":\"two\\nlines\"}";
  Reader reader(kept, std::strlen(kept));
  Span key;
  Kind kind = Kind::kEnd;
  char text[16] = {};
  size_t length = 0;
  CHECK(reader.begin_object());
  CHECK(reader.member(key, kind));
  CHECK(reader.string(text, sizeof(text), length) && length == 9);

  const char* cut = "{\"a\":\"two\\u0000halves\"}";
  Reader again(cut, std::strlen(cut));
  CHECK(again.begin_object());
  CHECK(again.member(key, kind));
  CHECK(!again.string(text, sizeof(text), length));
}

TEST(a_number_that_is_not_one_is_refused) {
  for (const char* bad : {R"({"a":01})", R"({"a":+1})", R"({"a":1.})", R"({"a":.5})",
                          R"({"a":1e})", R"({"a":NaN})", R"({"a":Infinity})"}) {
    CHECK(!walks(bad));
  }
}

TEST(an_integer_is_a_whole_number_and_fits) {
  const char* document = R"({"a":17,"b":17.0,"c":9223372036854775808})";
  Reader reader(document, std::strlen(document));
  Span key;
  Kind kind = Kind::kEnd;
  int64_t value = 0;
  CHECK(reader.begin_object());
  CHECK(reader.member(key, kind));
  CHECK(reader.integer(value) && value == 17);
  CHECK(reader.member(key, kind));
  CHECK(!reader.integer(value));  // 17.0 is a number that happens to be whole, not an int

  Reader overflowing(document, std::strlen(document));
  CHECK(overflowing.begin_object());
  CHECK(overflowing.member(key, kind) && overflowing.skip());
  CHECK(overflowing.member(key, kind) && overflowing.skip());
  CHECK(overflowing.member(key, kind));
  CHECK(!overflowing.integer(value));  // one past the largest, refused rather than wrapped
}

TEST(what_is_not_valid_text_is_not_text) {
  CHECK(sentry::is_clean_text("plain", 5));
  CHECK(sentry::is_clean_text("\xc2\xb0", 2));
  CHECK(!sentry::is_clean_text("\xc0\xaf", 2));          // overlong '/'
  CHECK(!sentry::is_clean_text("\xed\xa0\x80", 3));      // half a surrogate pair
  CHECK(!sentry::is_clean_text("\xe2\x82", 2));          // cut short
  const char nul[] = {'a', '\0', 'b'};
  CHECK(!sentry::is_clean_text(nul, sizeof(nul)));
}

TEST(the_writer_produces_what_the_contract_says) {
  char buffer[128];
  Writer writer(buffer, sizeof(buffer));
  writer.object_open();
  writer.key("name");
  writer.string("pir-1");
  writer.key("count");
  writer.integer(-42);
  writer.key("temperature_c");
  writer.fixed(42.25, 1);
  writer.key("live");
  writer.boolean(true);
  writer.key("unit");
  writer.null();
  writer.object_close();
  CHECK(writer.ok());
  std::string written(writer.data(), writer.size());
  CHECK_TEXT(written.c_str(),
             R"({"name":"pir-1","count":-42,"temperature_c":42.3,"live":true,"unit":null})");
}

TEST(a_writer_that_runs_out_of_room_produces_nothing_to_send) {
  char buffer[8];
  Writer writer(buffer, sizeof(buffer));
  writer.object_open();
  writer.key("a_rather_long_key");
  writer.string("and a longer value");
  writer.object_close();
  CHECK(!writer.ok());
}

TEST(a_quote_or_a_backslash_is_escaped_on_the_way_out) {
  char buffer[64];
  Writer writer(buffer, sizeof(buffer));
  writer.string("say \"hello\\\"");
  CHECK(writer.ok());
  std::string written(writer.data(), writer.size());
  CHECK_TEXT(written.c_str(), R"("say \"hello\\\"")");
}

TEST(a_string_that_is_not_text_is_never_written) {
  char buffer[32];
  Writer writer(buffer, sizeof(buffer));
  writer.string("\xc0\xaf", 2);
  CHECK(!writer.ok());
}

TEST(negative_and_small_fixed_numbers_are_written_whole) {
  // A number too small to show its own sign is written without one: "-0.0" is a reading
  // nobody took, and it looks like a measurement of something.
  struct Case {
    double value;
    int decimals;
    const char* text;
  };
  const Case cases[] = {
      {0.0, 1, "0.0"},   {-0.04, 1, "0.0"},  {-1.25, 1, "-1.3"},
      {1.0 / 3, 3, "0.333"}, {99.95, 1, "100.0"}, {7.0, 0, "7"},
  };
  for (const Case& one : cases) {
    char buffer[32];
    Writer writer(buffer, sizeof(buffer));
    writer.fixed(one.value, one.decimals);
    CHECK(writer.ok());
    std::string written(writer.data(), writer.size());
    CHECK_TEXT(written.c_str(), one.text);
  }
}

int main() { return harness::run_all("json"); }
