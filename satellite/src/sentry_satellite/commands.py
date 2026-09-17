"""What the hub is allowed to tell this node to do.

The list is short and it is closed. A command can grant a capability for a while, renew
that grant, or take it away, and it can replace the list of sources with one made only of
drivers this agent already has. It can ask a camera for video, or a microphone for sound,
for a while, to a port on the hub this node already talks to. It cannot name a program to
run, a file to read, a rule to apply, another host to send anything to, or touch the
certificates or the installed packages. That is the difference between a peripheral and a
second Sentry, and it is enforced here rather than trusted to the sender.
"""

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

from sentry_satellite.names import InvalidName, check_name

CAPABILITIES = ("events", "video", "audio")
ACTIONS = (
    "grant",
    "renew",
    "revoke",
    "stop",
    "configure",
    "video_start",
    "video_renew",
    "video_stop",
    "audio_start",
    "audio_renew",
    "audio_stop",
)
VIDEO_ACTIONS = ("video_start", "video_renew", "video_stop")
AUDIO_ACTIONS = ("audio_start", "audio_renew", "audio_stop")
MEDIA_ACTIONS = VIDEO_ACTIONS + AUDIO_ACTIONS
MAX_SOURCES = 32
MAX_VIDEO_SECONDS = 600
STREAM_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
VIDEO_FIELDS = {"stream_id", "source_id", "port", "token"}

Outcome = Literal["received", "applied", "failed"]


class CommandError(ValueError):
    """The message on the commands topic is not a command this node will act on."""


@dataclass(frozen=True)
class Command:
    command_id: str
    action: str
    node_id: str
    hub_epoch: int
    capability: str | None = None
    grant_id: str | None = None
    duration_seconds: float = 0.0
    sequence: int = 0
    revision: int = 0
    sources: tuple[dict, ...] = ()
    stream_id: str | None = None
    source_id: str | None = None
    port: int = 0
    token: str | None = None


def parse(message: dict, *, node_id: str) -> Command:
    if not isinstance(message, dict):
        raise CommandError("a command is an object")
    unknown = set(message) - {
        "command_id",
        "action",
        "node_id",
        "hub_epoch",
        "capability",
        "grant_id",
        "duration_seconds",
        "sequence",
        "revision",
        "sources",
        *VIDEO_FIELDS,
    }
    if unknown:
        raise CommandError(f"a command does not take {', '.join(sorted(unknown))}")
    command_id = message.get("command_id")
    if not isinstance(command_id, str) or not command_id:
        raise CommandError("a command has an id")
    action = message.get("action")
    if action not in ACTIONS:
        raise CommandError(f"action must be one of {', '.join(ACTIONS)}")
    try:
        addressed_to = check_name(message.get("node_id", ""), "a node id")
    except InvalidName as error:
        raise CommandError(str(error)) from error
    if addressed_to != node_id:
        raise CommandError(f"this command is addressed to {addressed_to}, not to {node_id}")
    epoch = message.get("hub_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise CommandError("a command carries the hub epoch it was issued in")
    capability = message.get("capability")
    if action in {"grant", "renew", "revoke"} and capability not in CAPABILITIES:
        raise CommandError(f"capability must be one of {', '.join(CAPABILITIES)}")
    duration = message.get("duration_seconds", 0)
    if isinstance(duration, bool) or not isinstance(duration, int | float) or duration < 0:
        raise CommandError("a duration is a number of seconds, and it is not negative")
    sequence = message.get("sequence", 0)
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise CommandError("a renewal is numbered")
    revision = message.get("revision", 0)
    sources = message.get("sources", [])
    if action == "configure":
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise CommandError("a configuration is numbered from 1")
        if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
            raise CommandError(f"a configuration is a list of at most {MAX_SOURCES} sources")
        if not all(isinstance(entry, dict) for entry in sources):
            raise CommandError("each configured source is an object")
    elif "revision" in message or "sources" in message:
        raise CommandError(f"a {action} command does not take a configuration")
    video = _video(action, message, duration) if action in MEDIA_ACTIONS else {}
    if action not in MEDIA_ACTIONS and VIDEO_FIELDS & set(message):
        raise CommandError(f"a {action} command does not describe a stream")
    return Command(
        command_id=command_id,
        action=action,
        node_id=addressed_to,
        hub_epoch=epoch,
        capability=capability,
        grant_id=message.get("grant_id"),
        duration_seconds=float(duration),
        sequence=sequence,
        revision=revision if action == "configure" else 0,
        sources=tuple(sources) if action == "configure" else (),
        **video,
    )


def _video(action: str, message: dict, duration: object) -> dict:
    """The stream a media command is about. The host is never among them: it is the hub."""
    stream_id = message.get("stream_id")
    if not isinstance(stream_id, str) or not STREAM_ID.fullmatch(stream_id):
        raise CommandError(f"a {action.partition('_')[0]} command names its stream")
    if action.endswith("_stop"):
        if set(message) & (VIDEO_FIELDS - {"stream_id"}):
            raise CommandError("stopping a stream needs only its id")
        return {"stream_id": stream_id}
    if not 0 < duration <= MAX_VIDEO_SECONDS:  # type: ignore[operator]
        raise CommandError(f"a stream lasts more than 0 and at most {MAX_VIDEO_SECONDS} s")
    if action.endswith("_renew"):
        if set(message) & (VIDEO_FIELDS - {"stream_id"}):
            raise CommandError("renewing a stream needs only its id and a duration")
        return {"stream_id": stream_id}
    try:
        source_id = check_name(message.get("source_id", ""), "a source id")
    except InvalidName as error:
        raise CommandError(str(error)) from error
    port = message.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise CommandError("the hub's media port is an unprivileged port number")
    token = message.get("token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise CommandError("a stream carries a token from the hub")
    return {"stream_id": stream_id, "source_id": source_id, "port": port, "token": token}


class Seen:
    """The commands already handled, so a retransmission is answered but not acted on twice.

    A command that arrives again gets the same answer as the first time. Anything else
    would let a retry hold a session open indefinitely, which is exactly what a flaky link
    produces on its own without anybody meaning any harm.
    """

    def __init__(self, capacity: int = 256) -> None:
        self._capacity = capacity
        self._outcomes: OrderedDict[str, Outcome] = OrderedDict()

    def remember(self, command_id: str, outcome: Outcome) -> Outcome:
        self._outcomes[command_id] = outcome
        self._outcomes.move_to_end(command_id)
        while len(self._outcomes) > self._capacity:
            self._outcomes.popitem(last=False)
        return outcome

    def outcome(self, command_id: str) -> Outcome | None:
        if command_id not in self._outcomes:
            return None
        self._outcomes.move_to_end(command_id)
        return self._outcomes[command_id]

    def __contains__(self, command_id: object) -> bool:
        return command_id in self._outcomes

    def __len__(self) -> int:
        return len(self._outcomes)


def ack(command: Command, outcome: Outcome, *, detail: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "command_id": command.command_id,
        "node_id": command.node_id,
        "outcome": outcome,
        "detail": detail,
    }
