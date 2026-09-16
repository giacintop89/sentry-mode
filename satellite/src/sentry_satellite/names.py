"""The identifier grammar, copied here because the satellite shares no code with the hub.

The hub publishes the same rule in `contracts/satellite/v1/event.schema.json`, and a test
reads it back from that file to prove the two have not drifted apart.
"""

import re

NAME = r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$"
KIND = r"^[a-z][a-z0-9]*\.[a-z][a-z0-9_]*$"

_NAME = re.compile(NAME)
_KIND = re.compile(KIND)


class InvalidName(ValueError):
    """A name that would not survive a topic, a file name or a log line."""


def check_name(value: str, what: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise InvalidName(
            f"{what} must be 1 to 40 characters of lowercase letters, digits and hyphens,"
            f" starting and ending with a letter or a digit; got {value!r}"
        )
    return value


def check_kind(value: str) -> str:
    if not isinstance(value, str) or not _KIND.fullmatch(value):
        raise InvalidName(
            f"an event kind is a family and a name, as in sensor.motion; got {value!r}"
        )
    return value


def local_name(value: str, node_id: str) -> str:
    """Accept either `pir-1` or `zero-entrance.pir-1`, and return the local half.

    A configuration file may spell a source out in full, which is easier to read next to
    the hub's rules. The node half then has to be this node: a board cannot name a source
    that belongs to another one.
    """
    node, dot, name = value.partition(".")
    if not dot:
        return check_name(value, "a source id")
    if node != node_id:
        raise InvalidName(
            f"source {value!r} belongs to node {node!r}, but this node is {node_id!r}"
        )
    return check_name(name, "a source id")
