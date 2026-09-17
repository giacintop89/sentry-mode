#include "sentry/session.h"

#include <cstring>

#include "sentry/names.h"

namespace sentry {

bool Session::connected(const char* connection_id) {
  if (link_ != Link::kOffline) return false;
  if (!is_uuid(connection_id)) return false;
  // The same identifier twice would make two connections indistinguishable, and the whole
  // point of the identifier is telling a goodbye from the old one apart from the new one.
  if (std::strcmp(previous_, connection_id) == 0) return false;
  std::memcpy(connection_id_, connection_id, std::strlen(connection_id) + 1);
  link_ = Link::kSubscribing;
  ++connections_;
  return true;
}

bool Session::subscribed() {
  if (link_ != Link::kSubscribing) return false;
  link_ = Link::kListening;
  return true;
}

bool Session::announced() {
  if (link_ != Link::kListening) return false;
  link_ = Link::kOnline;
  return true;
}

void Session::dropped() {
  if (link_ != Link::kOffline) std::memcpy(previous_, connection_id_, sizeof(previous_));
  connection_id_[0] = '\0';
  link_ = Link::kOffline;
}

}  // namespace sentry
