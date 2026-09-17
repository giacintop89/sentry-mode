// The order things have to happen in for a connection to mean anything.
//
// A node that announced itself online before its subscription was confirmed would be
// telling the hub to send commands to a topic it is not yet listening on, and the first
// grant would arrive in the gap and be gone: the MCU client connects with a clean session,
// so the broker keeps nothing for it while it is away. So `online` waits for the SUBACK.
//
// Every connection gets its own identifier, and it must be a new one. The will the broker
// holds names the connection it was registered for, which is how a goodbye from an old
// connection, published late, can be told apart from the node being gone now. Reusing an
// identifier would take that apart, so it is refused here rather than left to a convention.

#ifndef SENTRY_SESSION_H
#define SENTRY_SESSION_H

#include <cstddef>
#include <cstdint>

#include "sentry/identity.h"

namespace sentry {

enum class Link {
  kOffline,      // nothing is connected
  kSubscribing,  // the broker accepted the connection; the subscription is not confirmed
  kListening,    // the subscription is confirmed; the hub has not been told we are here
  kOnline,       // the retained state went out
};

class Session {
 public:
  // The broker accepted a CONNECT that carried a will for this same connection id.
  bool connected(const char* connection_id);

  // The SUBACK for this node's commands topic arrived.
  bool subscribed();

  // The retained `state` with `online: true` was published and acknowledged.
  bool announced();

  // The link dropped, however it dropped. Nothing may be published until it is back, and
  // the identifier of the connection that ended is not used again.
  void dropped();

  Link link() const { return link_; }
  bool may_publish_events() const { return link_ == Link::kOnline; }
  // The hub is owed a baseline of every source when a connection becomes usable, not when
  // the socket opens.
  bool ready_to_announce() const { return link_ == Link::kListening; }
  const char* connection_id() const { return link_ == Link::kOffline ? nullptr : connection_id_; }
  uint32_t connections() const { return connections_; }

 private:
  Link link_ = Link::kOffline;
  char connection_id_[kUuidText] = {};
  char previous_[kUuidText] = {};
  uint32_t connections_ = 0;
};

}  // namespace sentry

#endif  // SENTRY_SESSION_H
