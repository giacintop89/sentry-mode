// Where a message goes, and why the node cannot choose freely.
//
// `sentry/v1/nodes/<node-id>/<channel>`. The broker's ACL lets a node write only under its
// own name, which is what makes the topic the hub reads the sender from trustworthy — it
// is the one claim in the whole protocol that a node cannot make up. Building the topic in
// one place is what keeps that true: a node that could put a `+` or a `#` or a `../` in it
// would be trying to be somebody else, and the broker would refuse it, which is the right
// outcome arrived at by the wrong route.

#ifndef SENTRY_TOPICS_H
#define SENTRY_TOPICS_H

#include <cstddef>

namespace sentry {

// The prefix both existing implementations use.
inline constexpr const char* kTopicPrefix = "sentry/v1";
// Long enough for the prefix, a 40-character name and the longest channel.
inline constexpr size_t kMaxTopicText = 96;

enum class Channel { kEvents, kState, kHealth, kAcks, kCommands };

// Write the topic for one channel. False, and nothing written, if the prefix or the name
// is not one this node may use.
bool topic(const char* prefix, const char* node_id, Channel channel, char* out,
           size_t capacity);

const char* name_of(Channel channel);

}  // namespace sentry

#endif  // SENTRY_TOPICS_H
