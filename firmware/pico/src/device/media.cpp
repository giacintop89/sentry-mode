#include "media.h"

#include <cstdio>
#include <cstring>

#include "net.h"
#include "pico/stdlib.h"
#include "sentry/audio.h"
#include "sentry/command.h"
#include "sentry/names.h"

namespace media {
namespace {

// Four finished blocks, which is four hundred milliseconds of catching up. More would be
// more sound to throw away later: a connection that is half a second behind is not going
// to recover by being given a second of backlog, and what the hub wants is what is
// happening now.
constexpr size_t kQueued = 4;
constexpr size_t kBlockBytes = sentry::kAudioHeaderBytes + sentry::kAudioBlockSamples * 2;
// The gateway's answer is one short line. Anything longer than this is not an answer.
constexpr size_t kMostAnswer = 200;
constexpr uint32_t kGreetingPatienceMs = 8000;
constexpr uint32_t kBeforeTryingAgainMs = 2000;

enum class Stage { kIdle, kDialling, kGreeting, kLive };

struct Block {
  uint8_t bytes[kBlockBytes] = {};
  size_t size = 0;   // how much of it is a block at all
  size_t sent = 0;   // and how much of that has gone
};

Stage stage = Stage::kIdle;
char stream_id[sentry::kMaxIdText] = {};
char source_id[sentry::kMaxNameText] = {};
char token[sentry::kMaxOptionText] = {};
char host[64] = {};
uint16_t port = 0;
uint64_t until_ms = 0;
uint64_t try_again_ms = 0;
uint64_t greeted_at_ms = 0;
const char* trouble = nullptr;

Block queue[kQueued];
size_t oldest = 0;
size_t held = 0;
uint32_t sequence = 0;
uint32_t blocks_sent = 0;
uint32_t dropped = 0;
uint32_t connections = 0;

// The one being filled, and where the capture has got to. `samples_seen` counts every
// sample this node was handed while a stream was wanted, including the ones in blocks that
// were thrown away: that is what makes a dropped block a hole the hub can see and fill
// rather than sound that quietly never existed.
int16_t filling[sentry::kAudioBlockSamples];
size_t filled = 0;
uint64_t samples_seen = 0;
bool lost_before_the_next_one = false;

char answer[kMostAnswer + 1] = {};
size_t answer_size = 0;

void forget_the_queue() {
  oldest = 0;
  held = 0;
  filled = 0;
  answer_size = 0;
  lost_before_the_next_one = false;
}

void give_up(const char* why) {
  if (stage != Stage::kIdle) net::hang_up(net::Line::kMedia);
  trouble = why;
  stage = Stage::kIdle;
  forget_the_queue();
}

// A finished block, at the back of the queue. When there is no room the oldest one that
// has not started going out is thrown away — never the one half-sent, which would leave
// the hub reading the second half of a block as the first half of another.
void queue_the_block() {
  if (filled == 0) return;
  if (held == kQueued) {
    const bool head_is_going = queue[oldest].sent > 0;
    if (held == 1 && head_is_going) {
      // Nowhere to put it: the only block here is on its way out. This one is the loss.
      ++dropped;
      lost_before_the_next_one = true;
      samples_seen += filled;
      filled = 0;
      return;
    }
    const size_t victim = head_is_going ? (oldest + 1) % kQueued : oldest;
    if (victim == oldest) oldest = (oldest + 1) % kQueued;
    else {
      // Close the hole by moving the ones behind it forward, so the queue stays in order.
      for (size_t step = 1; step < held - 1; ++step) {
        const size_t here = (oldest + step) % kQueued;
        const size_t next = (oldest + step + 1) % kQueued;
        queue[here] = queue[next];
      }
    }
    --held;
    ++dropped;
    lost_before_the_next_one = true;
  }

  sentry::AudioBlock header;
  header.sequence = sequence;
  header.first_sample = samples_seen;
  header.captured_ns = time_us_64() * 1000ull;
  header.gap = lost_before_the_next_one;
  header.samples = filled;

  Block& block = queue[(oldest + held) % kQueued];
  block.sent = 0;
  block.size = 0;
  if (!sentry::write_audio_block(header, filling, block.bytes, sizeof(block.bytes),
                                 block.size)) {
    // Nothing the caller did can cause this — the size is this file's own — so there is
    // nothing to say to the hub about it. The samples are dropped rather than sent wrong.
    ++dropped;
    samples_seen += filled;
    filled = 0;
    return;
  }
  ++held;
  ++sequence;
  samples_seen += filled;
  filled = 0;
  lost_before_the_next_one = false;
}

// The line the gateway wants first, and then its answer.
void say_hello() {
  char hello[320] = {};
  size_t size = 0;
  if (!sentry::write_audio_hello(stream_id, source_id, token, hello, sizeof(hello), size)) {
    give_up("this node would not put that stream on a socket");
    return;
  }
  if (net::send(reinterpret_cast<const uint8_t*>(hello), size, net::Line::kMedia) != size) {
    give_up("the hello would not go out");
    return;
  }
  // The hello goes off the stack straight away. The token itself is kept for as long as
  // the hub is asking: a connection that drops is one this node reconnects, with the
  // ticket it was given, and a node that had wiped it could only sit there granted and
  // silent until somebody noticed.
  std::memset(hello, 0, sizeof(hello));
  stage = Stage::kGreeting;
}

void listen_for_the_answer(uint64_t now_ms) {
  uint8_t taken[64];
  size_t got = 0;
  while ((got = net::receive(taken, sizeof(taken), net::Line::kMedia)) > 0) {
    for (size_t index = 0; index < got; ++index) {
      if (answer_size < kMostAnswer) answer[answer_size++] = static_cast<char>(taken[index]);
      if (taken[index] != '\n') continue;
      char detail[96] = {};
      if (sentry::read_gateway_answer(answer, answer_size - 1, detail, sizeof(detail))) {
        stage = Stage::kLive;
        trouble = nullptr;
        ++connections;
        std::printf("# the hub is taking sound from %s\n", source_id);
        return;
      }
      static char refused[128];
      std::snprintf(refused, sizeof(refused), "the hub refused the stream: %s", detail);
      give_up(refused);
      return;
    }
    if (answer_size >= kMostAnswer) {
      give_up("the hub's answer is longer than an answer");
      return;
    }
  }
  if (now_ms - greeted_at_ms > kGreetingPatienceMs) {
    give_up("the hub did not answer the hello");
  }
}

void send_what_there_is() {
  // Strictly in order, and one block at a time: a write that only half went is finished
  // before anything else is offered, because the hub reads a byte stream and cannot tell
  // two halves of different blocks from one block.
  while (held > 0) {
    Block& block = queue[oldest];
    const size_t left = block.size - block.sent;
    const size_t taken =
        net::send_some(block.bytes + block.sent, left, net::Line::kMedia);
    block.sent += taken;
    if (block.sent < block.size) return;  // the rest next turn
    oldest = (oldest + 1) % kQueued;
    --held;
    ++blocks_sent;
  }
}

}  // namespace

bool start(const char* the_host, uint16_t the_port, const char* the_stream,
           const char* the_source, const char* the_token, uint64_t until, 
           const sentry::Credentials& credentials, const char*& why_not) {
  if (stage != Stage::kIdle) {
    why_not = "this node is already streaming, and it streams one thing at a time";
    return false;
  }
  if (the_host == nullptr || the_host[0] == '\0') {
    why_not = "this node has no hub to send sound to";
    return false;
  }
  if (!sentry::is_stream_id(the_stream, std::strlen(the_stream)) || !sentry::is_name(the_source)) {
    why_not = "that is not a stream this node could have been granted";
    return false;
  }
  if (the_token == nullptr || the_token[0] == '\0') {
    why_not = "a stream without a token is one the hub would refuse anyway";
    return false;
  }

  std::snprintf(host, sizeof(host), "%s", the_host);
  port = the_port;
  std::snprintf(stream_id, sizeof(stream_id), "%s", the_stream);
  std::snprintf(source_id, sizeof(source_id), "%s", the_source);
  std::snprintf(token, sizeof(token), "%s", the_token);
  until_ms = until;
  trouble = nullptr;
  forget_the_queue();
  samples_seen = 0;
  sequence = 0;
  blocks_sent = 0;
  dropped = 0;
  try_again_ms = 0;

  const net::Dialled dialled =
      net::dial(host, port, credentials, net::Line::kMedia);
  if (dialled != net::Dialled::kOpening) {
    std::memset(token, 0, sizeof(token));
    why_not = net::name_of(dialled);
    return false;
  }
  stage = Stage::kDialling;
  return true;
}

bool renew(const char* the_stream, uint64_t until, const char*& why_not) {
  if (stage == Stage::kIdle || the_stream == nullptr ||
      std::strcmp(stream_id, the_stream) != 0) {
    why_not = "there is no such stream to renew";
    return false;
  }
  until_ms = until;
  return true;
}

void stop(const char* the_stream, const char* why) {
  if (stage == Stage::kIdle) return;
  if (the_stream != nullptr && std::strcmp(stream_id, the_stream) != 0) return;
  give_up(why);
  std::memset(token, 0, sizeof(token));
  std::memset(stream_id, 0, sizeof(stream_id));
  std::memset(source_id, 0, sizeof(source_id));
}

bool wants(const char* asking) {
  return stage != Stage::kIdle && asking != nullptr && std::strcmp(source_id, asking) == 0;
}

void offer(const int16_t* samples, size_t count) {
  if (stage == Stage::kIdle || samples == nullptr) return;
  size_t at = 0;
  while (at < count) {
    const size_t room = sentry::kAudioBlockSamples - filled;
    const size_t taking = (count - at) < room ? (count - at) : room;
    std::memcpy(filling + filled, samples + at, taking * sizeof(int16_t));
    filled += taking;
    at += taking;
    if (filled == sentry::kAudioBlockSamples) queue_the_block();
  }
}

void serve(uint64_t now_ms, const sentry::Credentials& credentials) {
  if (stage == Stage::kIdle) return;
  // The end of the grant is the end of the stream, measured here on this node's own clock:
  // a hub that stopped asking, or that cannot be reached to stop asking, is the same thing.
  if (now_ms >= until_ms) {
    stop(nullptr, "the stream ran out");
    return;
  }

  const net::Socket socket = net::socket(net::Line::kMedia);
  if (socket == net::Socket::kClosed) {
    // The hub is still asking, so this is worth another try — with a pause, so that a
    // gateway that is refusing cannot be asked sixty times a second.
    static char ended[128];
    std::snprintf(ended, sizeof(ended), "%s", net::why_closed(net::Line::kMedia));
    net::hang_up(net::Line::kMedia);
    trouble = ended;
    stage = Stage::kDialling;
    forget_the_queue();
    try_again_ms = now_ms + kBeforeTryingAgainMs;
    return;
  }
  if (stage == Stage::kDialling && socket == net::Socket::kIdle) {
    if (now_ms < try_again_ms) return;
    if (net::dial(host, port, credentials, net::Line::kMedia) != net::Dialled::kOpening) {
      try_again_ms = now_ms + kBeforeTryingAgainMs;
    }
    return;
  }
  if (socket != net::Socket::kOpen) return;

  if (stage == Stage::kDialling) {
    greeted_at_ms = now_ms;
    say_hello();
    return;
  }
  if (stage == Stage::kGreeting) {
    listen_for_the_answer(now_ms);
    return;
  }
  send_what_there_is();
}

Numbers how_it_is_going() {
  Numbers numbers;
  switch (stage) {
    case Stage::kIdle:
      numbers.state = "idle";
      break;
    case Stage::kDialling:
      numbers.state = "dialling";
      break;
    case Stage::kGreeting:
      numbers.state = "greeting";
      break;
    case Stage::kLive:
      numbers.state = "live";
      break;
  }
  numbers.stream_id = stream_id[0] != '\0' ? stream_id : nullptr;
  numbers.source_id = source_id[0] != '\0' ? source_id : nullptr;
  numbers.blocks = blocks_sent;
  numbers.dropped = dropped;
  numbers.connections = connections;
  numbers.until_ms = until_ms;
  numbers.error = trouble;
  return numbers;
}

}  // namespace media
