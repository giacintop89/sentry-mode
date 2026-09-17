// The frames that cross the cable to a board with no radio.
//
// The layout is shared with the bridge, so `tools/link_check.py` reads what this writes
// with the hub's own reader and against the published contract. What this file adds is the
// behaviour on a cable that is not perfect: a byte lost, a byte invented, a frame from the
// side that has no business sending it, and a reader that has to find its place again
// without being restarted.

#include <cstdint>
#include <cstring>
#include <vector>

#include "harness.h"
#include "sentry/link.h"

using sentry::Carries;
using sentry::Frame;
using sentry::kLinkHeaderBytes;
using sentry::kLinkTrailerBytes;
using sentry::kMaxLinkPayload;
using sentry::link_checksum;
using sentry::LinkReader;
using sentry::write_frame;

namespace {

uint32_t at32(const uint8_t* bytes) {
  uint32_t value = 0;
  for (size_t byte = 0; byte < 4; ++byte) value |= static_cast<uint32_t>(bytes[byte]) << (8 * byte);
  return value;
}

uint16_t at16(const uint8_t* bytes) {
  return static_cast<uint16_t>(bytes[0] | (bytes[1] << 8));
}

std::vector<uint8_t> framed(Carries what, uint32_t counter, const std::string& payload) {
  std::vector<uint8_t> out(kLinkHeaderBytes + payload.size() + kLinkTrailerBytes);
  size_t written = 0;
  if (!write_frame(what, counter, reinterpret_cast<const uint8_t*>(payload.data()),
                   payload.size(), out.data(), out.size(), written)) {
    return {};
  }
  out.resize(written);
  return out;
}

// Everything a reader makes of some bytes, in the order it made it.
struct Heard {
  std::vector<Carries> what;
  std::vector<std::string> payloads;
};

Heard feed(LinkReader& reader, const std::vector<uint8_t>& bytes) {
  Heard heard;
  Frame frame;
  for (uint8_t byte : bytes) {
    if (!reader.eat(byte, frame)) continue;
    heard.what.push_back(frame.what);
    heard.payloads.emplace_back(reinterpret_cast<const char*>(frame.payload), frame.size);
  }
  return heard;
}

}  // namespace

TEST(a_frame_is_a_header_a_payload_and_a_checksum_over_both) {
  const std::vector<uint8_t> frame = framed(Carries::kEvents, 0x01020304, "{\"a\":1}");
  CHECK(frame.size() == kLinkHeaderBytes + 7 + kLinkTrailerBytes);
  CHECK(std::memcmp(frame.data(), "SMB1", 4) == 0);
  CHECK(frame[4] == 1);                                   // version
  CHECK(frame[5] == static_cast<uint8_t>(Carries::kEvents));
  CHECK(frame[6] == 0);                                   // flags, none of which exist yet
  CHECK(at32(frame.data() + 7) == 0x01020304u);
  CHECK(at16(frame.data() + 11) == 7);
  CHECK(std::memcmp(frame.data() + kLinkHeaderBytes, "{\"a\":1}", 7) == 0);
  CHECK(at32(frame.data() + kLinkHeaderBytes + 7) ==
        link_checksum(frame.data(), kLinkHeaderBytes + 7));
}

TEST(the_checksum_is_the_one_the_other_end_computes) {
  // `zlib.crc32(b"123456789")`, which is the check value every CRC-32 implementation
  // publishes. If this line ever changes, the cable has stopped speaking Python.
  const char* nine = "123456789";
  CHECK(link_checksum(reinterpret_cast<const uint8_t*>(nine), 9) == 0xCBF43926u);
  CHECK(link_checksum(reinterpret_cast<const uint8_t*>(""), 0) == 0u);
}

TEST(a_frame_that_would_not_fit_is_never_half_written) {
  uint8_t out[8] = {};
  size_t written = 99;
  CHECK(!write_frame(Carries::kEvents, 0, nullptr, 0, out, sizeof(out), written));
  CHECK(written == 0);

  std::vector<uint8_t> room(kLinkHeaderBytes + kMaxLinkPayload + kLinkTrailerBytes + 8);
  const std::vector<uint8_t> payload(kMaxLinkPayload + 1, 'x');
  CHECK(!write_frame(Carries::kEvents, 0, payload.data(), payload.size(), room.data(),
                     room.size(), written));
  CHECK(written == 0);
  // And the largest one that does fit, written whole.
  CHECK(write_frame(Carries::kEvents, 0, payload.data(), kMaxLinkPayload, room.data(),
                    room.size(), written));
  CHECK(written == kLinkHeaderBytes + kMaxLinkPayload + kLinkTrailerBytes);
}

TEST(a_kind_that_is_neither_sides_is_not_a_frame_anybody_writes) {
  uint8_t out[64] = {};
  size_t written = 99;
  CHECK(!write_frame(Carries::kNothing, 0, nullptr, 0, out, sizeof(out), written));
  CHECK(!write_frame(static_cast<Carries>(200), 0, nullptr, 0, out, sizeof(out), written));
  CHECK(written == 0);
}

TEST(frames_come_back_out_in_the_order_they_went_in) {
  LinkReader reader(true);
  std::vector<uint8_t> stream;
  for (const auto& one : {framed(Carries::kState, 1, "{\"online\":true}"),
                          framed(Carries::kEvents, 2, "{\"e\":1}"),
                          framed(Carries::kAcks, 3, "{\"ack\":1}")}) {
    stream.insert(stream.end(), one.begin(), one.end());
  }
  const Heard heard = feed(reader, stream);
  CHECK(heard.what.size() == 3);
  CHECK(heard.what[0] == Carries::kState);
  CHECK(heard.what[1] == Carries::kEvents);
  CHECK(heard.what[2] == Carries::kAcks);
  CHECK(heard.payloads[1] == "{\"e\":1}");
  CHECK(reader.frames() == 3);
  CHECK(reader.discarded() == 0);
  CHECK(reader.missed() == 0);
}

TEST(a_payload_survives_until_the_next_byte_and_no_longer) {
  LinkReader reader(true);
  Frame frame;
  const std::vector<uint8_t> one = framed(Carries::kEvents, 1, "first");
  for (uint8_t byte : one) reader.eat(byte, frame);
  CHECK(frame.size == 5);
  CHECK(std::memcmp(frame.payload, "first", 5) == 0);
  // Still there while nothing else has arrived, which is what lets the caller read it
  // after the call that produced it rather than inside it.
  CHECK(std::memcmp(frame.payload, "first", 5) == 0);
}

TEST(rubbish_in_front_of_a_frame_is_thrown_away_and_counted) {
  LinkReader reader(true);
  std::vector<uint8_t> stream = {'h', 'e', 'l', 'l', 'o', '\n'};
  const std::vector<uint8_t> one = framed(Carries::kEvents, 7, "{}");
  stream.insert(stream.end(), one.begin(), one.end());
  const Heard heard = feed(reader, stream);
  CHECK(heard.what.size() == 1);
  CHECK(heard.payloads[0] == "{}");
  CHECK(reader.discarded() == 6);
}

TEST(a_byte_lost_in_the_middle_costs_one_frame_and_not_the_next) {
  LinkReader reader(true);
  const std::vector<uint8_t> first = framed(Carries::kEvents, 1, "{\"a\":1}");
  const std::vector<uint8_t> second = framed(Carries::kEvents, 2, "{\"b\":2}");
  std::vector<uint8_t> stream(first.begin(), first.end() - 1);  // the last byte never came
  stream.insert(stream.end(), second.begin(), second.end());
  const Heard heard = feed(reader, stream);
  CHECK(heard.what.size() == 1);
  CHECK(heard.payloads[0] == "{\"b\":2}");
  CHECK(reader.frames() == 1);
  CHECK(reader.discarded() > 0);
}

TEST(a_payload_that_arrived_wrong_is_refused_rather_than_handed_on) {
  LinkReader reader(true);
  std::vector<uint8_t> frame = framed(Carries::kEvents, 1, "{\"a\":1}");
  frame[kLinkHeaderBytes + 3] = 'Z';  // one byte of the payload, changed on the wire
  const std::vector<uint8_t> good = framed(Carries::kEvents, 2, "{\"b\":2}");
  frame.insert(frame.end(), good.begin(), good.end());
  const Heard heard = feed(reader, frame);
  CHECK(heard.what.size() == 1);
  CHECK(heard.payloads[0] == "{\"b\":2}");
}

TEST(a_frame_the_other_side_had_no_business_sending_is_refused) {
  // A reader on the bridge's side of the cable, being sent a command: only a bridge sends
  // those, so this is a board that is confused or something that is not a board.
  LinkReader reader(true);
  std::vector<uint8_t> stream = framed(Carries::kCommands, 1, "{\"configure\":1}");
  const std::vector<uint8_t> real = framed(Carries::kEvents, 2, "{\"e\":1}");
  stream.insert(stream.end(), real.begin(), real.end());
  const Heard heard = feed(reader, stream);
  CHECK(heard.what.size() == 1);
  CHECK(heard.what[0] == Carries::kEvents);
  CHECK(reader.refused() == 1);
  CHECK(reader.frames() == 1);
}

TEST(the_other_reader_takes_the_other_three_and_nothing_else) {
  LinkReader reader(false);  // a board, reading what the bridge sends it
  std::vector<uint8_t> stream;
  for (const auto& one : {framed(Carries::kHello, 1, ""),
                          framed(Carries::kTime, 2, "{\"unix_ms\":1}"),
                          framed(Carries::kCommands, 3, "{\"c\":1}"),
                          framed(Carries::kEvents, 4, "{\"e\":1}")}) {
    stream.insert(stream.end(), one.begin(), one.end());
  }
  const Heard heard = feed(reader, stream);
  CHECK(heard.what.size() == 3);
  CHECK(heard.what[0] == Carries::kHello);
  CHECK(heard.what[2] == Carries::kCommands);
  CHECK(reader.refused() == 1);
}

TEST(a_frame_that_never_arrived_is_a_number_rather_than_a_silence) {
  LinkReader reader(true);
  std::vector<uint8_t> stream;
  for (const auto& one : {framed(Carries::kEvents, 10, "a"), framed(Carries::kEvents, 14, "b")}) {
    stream.insert(stream.end(), one.begin(), one.end());
  }
  feed(reader, stream);
  CHECK(reader.missed() == 3);
  CHECK(reader.frames() == 2);
}

TEST(a_counter_that_wrapped_is_not_four_billion_lost_frames) {
  LinkReader reader(true);
  std::vector<uint8_t> stream;
  for (const auto& one : {framed(Carries::kEvents, 0xFFFFFFFFu, "a"),
                          framed(Carries::kEvents, 0, "b"),
                          framed(Carries::kEvents, 1, "c")}) {
    stream.insert(stream.end(), one.begin(), one.end());
  }
  feed(reader, stream);
  CHECK(reader.missed() == 0);
  CHECK(reader.frames() == 3);
}

TEST(a_cable_that_was_unplugged_leaves_nothing_half_read) {
  LinkReader reader(true);
  Frame frame;
  const std::vector<uint8_t> one = framed(Carries::kEvents, 1, "{\"a\":1}");
  for (size_t index = 0; index + 1 < one.size(); ++index) reader.eat(one[index], frame);
  reader.forget();
  // The tail of the frame that was interrupted must not join the next one.
  const Heard heard = feed(reader, framed(Carries::kEvents, 2, "{\"b\":2}"));
  CHECK(heard.what.size() == 1);
  CHECK(heard.payloads[0] == "{\"b\":2}");
}

TEST(a_frame_with_a_version_or_a_flag_this_firmware_does_not_know_is_not_read) {
  LinkReader reader(true);
  std::vector<uint8_t> newer = framed(Carries::kEvents, 1, "{\"a\":1}");
  newer[4] = 2;  // a version from a bridge written after this board
  std::vector<uint8_t> flagged = framed(Carries::kEvents, 2, "{\"b\":2}");
  flagged[6] = 0x01;  // a flag nobody has agreed on
  const std::vector<uint8_t> good = framed(Carries::kEvents, 3, "{\"c\":3}");
  std::vector<uint8_t> stream = newer;
  stream.insert(stream.end(), flagged.begin(), flagged.end());
  stream.insert(stream.end(), good.begin(), good.end());
  const Heard heard = feed(reader, stream);
  CHECK(heard.what.size() == 1);
  CHECK(heard.payloads[0] == "{\"c\":3}");
}

TEST(the_largest_payload_goes_through_whole) {
  LinkReader reader(true);
  const std::string big(kMaxLinkPayload, 'x');
  const Heard heard = feed(reader, framed(Carries::kEvents, 1, big));
  CHECK(heard.what.size() == 1);
  CHECK(heard.payloads[0].size() == kMaxLinkPayload);
  CHECK(heard.payloads[0] == big);
}

int main() { return harness::run_all("link"); }
