// The vocabulary the contract is written in, in one place.
//
// A name, a uuid, a kind, a driver, a timestamp: each of these is spelled out in the
// hub's models and in the published schemas, and each was being checked in whichever file
// happened to need it first. Three copies of "what a name is" is three chances for one of
// them to be a character out, and the one that is wrong is the one that lets a node claim
// a source on another node.
//
// Everything here takes the caller's bytes and returns a verdict. Nothing allocates and
// nothing keeps a pointer.

#ifndef SENTRY_NAMES_H
#define SENTRY_NAMES_H

#include <cstddef>

namespace sentry {

// `sources/models.py`: lower case, digits and inner dashes, at most 40 characters.
bool is_name(const char* text, size_t length);
bool is_name(const char* text);

// A uuid as the contract spells it, because `format: uuid` is advice and a pattern is not.
bool is_uuid(const char* text);

// A family and a name: sensor.motion, board.temperature. The families are not a closed
// list, which is what lets a new kind of sensor exist without a new contract.
bool is_kind(const char* text);

// `control.py`'s DRIVER: gpio, bme280, board.
bool is_driver(const char* text);

// RFC 3339 with the offset spelled out. A reading whose time zone is a guess cannot be
// ordered against anything else, so one without an offset is refused.
bool is_timestamp(const char* text);

// The alphabet a stream id and a ticket are written in: nothing that could be a path, a
// host or a shell word, whatever the hub thinks it is sending.
bool is_url_safe(const char* text, size_t length);
bool is_stream_id(const char* text, size_t length);
bool is_token(const char* text, size_t length);

// Text this node may repeat: valid UTF-8, no control characters, and within the limit the
// contract states for that field.
bool is_text_within(const char* text, size_t limit);

}  // namespace sentry

#endif  // SENTRY_NAMES_H
