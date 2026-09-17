"""The four messages that are not events: state, health, commands and their answers.

Events have had a written contract since the first satellite: a schema generated from the
models, fixtures both sides are held to, and a test that fails when they drift. The other
four channels never did. Each side read the other's JSON with `.get()` and a default,
which was survivable while both sides were Python written in the same week.

It stops being survivable when the other side is a microcontroller in C++ with fixed
buffers. A field that is sometimes absent, a number that is sometimes a float and a string
of unbounded length are all cheap in Python and all expensive there, and none of them can
be discovered by reading a `.get()` call. So this module writes down what is already being
said, with the limits an implementation needs in order to size a buffer for it.

Nothing here changes the wire. The models describe today's messages, and the tests on both
sides prove it: what the hub and the Linux agent actually publish is validated against
these schemas. The hub's own readers stay as tolerant as they are; a contract is what a
sender may rely on being accepted, not a new way for a heartbeat to be thrown away.

Two asymmetries are deliberate and documented rather than tidied up:

- A command carries no `schema_version`. The agent refuses fields it does not know, so
  adding one would make every hub reject every node it was talking to five minutes before.
  Commands are versioned by the contract directory they are published in, like the topics.
- `health` is open where the others are closed. It is diagnostics: a board reports what it
  can measure and says nothing about the rest, and a hub that refused a heartbeat for
  carrying one unknown counter would be throwing away the evidence it most needs.
"""

from __future__ import annotations

from typing import Annotated, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sentry_mode.satellites.protocol import SCHEMA_VERSION, Wire, _spell_out_formats
from sentry_mode.sources.models import NAME, ClockStatus, Quality

MAX_SOURCES = 32
"""How many sources one node may declare or be given. The agent's own limit."""

MAX_TEXT = 128
"""The longest a single option value may be, as the hub's configuration API already says."""

MAX_ID = 64
MAX_DETAIL = 256

DRIVER = r"^[a-z][a-z0-9_]{0,31}$"
"""What a source kind looks like: `gpio`, `bme280`, `board`."""

STREAM_ID = r"^[A-Za-z0-9_-]{8,64}$"
TOKEN = r"^[A-Za-z0-9_-]{32,128}$"

Capability = Literal["events", "video", "audio"]
Action = Literal[
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
]
Outcome = Literal["received", "applied", "failed"]
Reset = Literal["power", "brownout", "button", "watchdog", "software", "debugger"]
"""Why a board is running again, in the words a board can honestly use. There is no
`unknown`: a node that cannot tell leaves the field out, and a list that offered the word
would invite a firmware to write it where saying nothing is the truthful answer."""

CAPABILITIES: tuple[str, ...] = get_args(Capability)
ACTIONS: tuple[str, ...] = get_args(Action)
OUTCOMES: tuple[str, ...] = get_args(Outcome)
RESETS: tuple[str, ...] = get_args(Reset)
GRANTS = ("grant", "renew", "revoke")
MEDIA = tuple(
    f"{kind}_{what}" for kind in ("video", "audio") for what in ("start", "renew", "stop")
)
STREAM_FIELDS = ("stream_id", "source_id", "port", "token")
MAX_STREAM_SECONDS = 600

Scalar = bool | int | float | str | None
Text = Annotated[str, StringConstraints(max_length=MAX_TEXT)]
Option = bool | int | float | Text | None
"""What one option or one reading may be. The length is in the contract rather than in a
comment because the other end has to decide how much memory to set aside for it."""


class Open(BaseModel):
    """A message whose unknown fields are kept rather than refused: health, and only health."""

    model_config = ConfigDict(extra="allow", frozen=True, allow_inf_nan=False)


# -- state: what a node is, and whether it is here -------------------------------------


class DeclaredSource(Wire):
    """One source as the node says it has it, with the options it is running under."""

    source_id: str = Field(pattern=NAME)
    kind: str = Field(pattern=DRIVER)
    enabled: bool = True
    options: dict[str, Option] = Field(default_factory=dict)


class NodeState(Wire):
    """The retained snapshot on `state`, and the will the broker publishes for a node.

    Everything past `online` is optional, and that is what makes a small implementation
    possible: the lwIP MQTT client writes the will's topic and payload with 8-bit lengths,
    so a goodbye has to fit in 255 bytes including the topic. A node that is leaving says
    who it is, which boot and which connection is ending, and that it is gone. The full
    snapshot, with the sources, is the same message with the optional parts filled in.
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    node_id: str = Field(pattern=NAME)
    boot_id: UUID
    connection_id: UUID
    online: bool
    agent_version: str | None = Field(default=None, max_length=32)
    profile: str | None = Field(default=None, max_length=32)
    config_revision: int | None = Field(default=None, ge=0)
    sources: list[DeclaredSource] = Field(default_factory=list, max_length=MAX_SOURCES)


# -- health: how it is getting on ------------------------------------------------------


class Drops(Wire):
    """What the node threw away and why, counted rather than hidden."""

    count: int = Field(default=0, ge=0)
    bytes: int = Field(default=0, ge=0)
    age: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)


class Queue(Wire):
    """What is waiting to be sent, and whether the node is allowed to send it."""

    events: int = Field(ge=0)
    bytes: int = Field(ge=0)
    published: int = Field(ge=0)
    refused: int = Field(ge=0)
    drops: Drops = Field(default_factory=Drops)
    granted: bool = False


class Reading(Open):
    """The last thing a source measured, as health reports it."""

    value: Option = None
    unit: str | None = Field(default=None, max_length=16)
    quality: Quality = Quality.VALID


class SourceReport(Open):
    """One source's diagnostics. Open: a camera says more than a contact does."""

    readings: int = Field(default=0, ge=0)
    last_reading_age_seconds: float | None = None
    driver: str | None = Field(default=None, max_length=32)
    error: str | None = Field(default=None, max_length=MAX_DETAIL)
    last: Reading | None = None


class Board(Open):
    """What the board says about itself. Every measurement is optional on purpose.

    A microcontroller has no load average and no available memory in the sense Linux means
    them. It reports what it can measure and leaves out the rest; what it leaves out is
    unknown, which is not the same as zero and must never be written as zero.
    """

    uptime_seconds: float | None = Field(default=None, ge=0)
    temperature_c: float | None = None
    load1: float | None = Field(default=None, ge=0)
    memory_available_kb: int | None = Field(default=None, ge=0)
    throttled: str | None = Field(default=None, max_length=32)
    reset: Reset | None = None
    """Why the node is running this time, when its chip can tell. A board that keeps
    coming back the same way is a board with something wrong with it, and which way it
    is decides who has to fix it. Absent means the node did not say, which is not the
    same as a clean start and must never be shown as one."""


class HealthReport(Wire):
    """The heartbeat on `health`: proof of life, and the numbers that qualify the rest."""

    schema_version: Literal[1] = SCHEMA_VERSION
    node_id: str = Field(pattern=NAME)
    boot_id: UUID
    agent_uptime_seconds: float = Field(ge=0)
    clock_status: ClockStatus = ClockStatus.UNKNOWN
    queue: Queue
    sources: dict[str, SourceReport] = Field(default_factory=dict)
    board: Board = Field(default_factory=Board)


# -- commands: the short, closed list of what the hub may ask -------------------------


class ConfiguredSource(Wire):
    """A source in a `configure` command, spelled the way the node's own file spells it.

    Its options are written beside `id` and `kind` rather than under an `options` key: this
    is the node's file, sent over the wire, and the node writes it down as it arrived.
    """

    model_config = ConfigDict(extra="allow", frozen=True, allow_inf_nan=False)

    id: str = Field(pattern=NAME)
    kind: str = Field(pattern=DRIVER)

    @model_validator(mode="after")
    def _bounded(self) -> ConfiguredSource:
        for name, value in (self.__pydantic_extra__ or {}).items():
            if not isinstance(value, Scalar):
                raise ValueError(f"{self.id}.{name} must be a plain value")
            if isinstance(value, str) and len(value) > MAX_TEXT:
                raise ValueError(f"{self.id}.{name} is longer than {MAX_TEXT} characters")
        return self


class HubCommand(Wire):
    """One instruction on a node's `commands` topic.

    The grammar is closed, and it is closed here for the same reason the agent closes it:
    a command can lend a capability for a while, take it back, replace the list of sources
    with one made of drivers the node already has, or ask a camera or a microphone for a
    stream to a port on the hub. It cannot name a program, a file, a rule or another host.

    Which fields a command carries depends on what it is asking for, and a field that does
    not belong to the action is refused rather than ignored: a `stop` that carries a token
    is not a stop with something extra, it is a message nobody meant to send.
    """

    command_id: str = Field(min_length=1, max_length=MAX_ID)
    action: Action
    node_id: str = Field(pattern=NAME)
    hub_epoch: int = Field(ge=0)
    capability: Capability | None = None
    grant_id: UUID | None = None
    duration_seconds: float = Field(default=0.0, ge=0)
    sequence: int = Field(default=0, ge=0)
    revision: int = Field(default=0, ge=0)
    sources: list[ConfiguredSource] = Field(default_factory=list, max_length=MAX_SOURCES)
    stream_id: str | None = Field(default=None, pattern=STREAM_ID)
    source_id: str | None = Field(default=None, pattern=NAME)
    port: int | None = Field(default=None, ge=1024, le=65535)
    token: str | None = Field(default=None, pattern=TOKEN)

    @model_validator(mode="after")
    def _only_what_the_action_takes(self) -> HubCommand:
        said = self.model_fields_set
        if self.action in GRANTS and self.capability is None:
            raise ValueError(f"a {self.action} names the capability it is about")
        if self.action == "configure":
            if self.revision < 1:
                raise ValueError("a configuration is numbered from 1")
        elif said & {"revision", "sources"}:
            raise ValueError(f"a {self.action} command does not take a configuration")
        if self.action in MEDIA:
            self._stream(said)
        elif said & set(STREAM_FIELDS):
            raise ValueError(f"a {self.action} command does not describe a stream")
        return self

    def _stream(self, said: set[str]) -> None:
        if self.stream_id is None:
            raise ValueError(f"a {self.action} names its stream")
        if self.action.endswith("_stop"):
            if said & {"source_id", "port", "token"}:
                raise ValueError("stopping a stream needs only its id")
            return
        if not 0 < self.duration_seconds <= MAX_STREAM_SECONDS:
            raise ValueError(f"a stream lasts more than 0 and at most {MAX_STREAM_SECONDS} s")
        if self.action.endswith("_renew"):
            if said & {"source_id", "port", "token"}:
                raise ValueError("renewing a stream needs only its id and a duration")
            return
        missing = [name for name in ("source_id", "port", "token") if getattr(self, name) is None]
        if missing:
            raise ValueError(f"starting a stream needs {', '.join(missing)}")


class CommandAck(Wire):
    """What the node answers on `acks`, once, for each command it was sent.

    An answer is about one command and says what became of it. It is not a receipt for an
    event, and `applied` is not a promise that the hub agreed with the result.
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    command_id: str = Field(min_length=1, max_length=MAX_ID)
    node_id: str = Field(pattern=NAME)
    outcome: Outcome
    detail: str | None = Field(default=None, max_length=MAX_DETAIL)


MESSAGES: dict[str, type[Wire]] = {
    "state": NodeState,
    "health": HealthReport,
    "command": HubCommand,
    "ack": CommandAck,
}
"""Each control message by the name of the schema published for it."""

TITLES = {
    "state": "Satellite state, version 1",
    "health": "Satellite health, version 1",
    "command": "Hub command to a satellite, version 1",
    "ack": "Satellite answer to a command, version 1",
}


def control_schema(name: str) -> dict:
    """The schema published for one control message, generated from the model above."""
    schema = MESSAGES[name].model_json_schema()
    _spell_out_formats(schema)
    schema["$id"] = f"https://sentry-mode.invalid/contracts/satellite/v1/control/{name}.schema.json"
    schema["title"] = TITLES[name]
    return schema


__all__ = [
    "ACTIONS",
    "CAPABILITIES",
    "MAX_SOURCES",
    "MESSAGES",
    "OUTCOMES",
    "RESETS",
    "Board",
    "CommandAck",
    "ConfiguredSource",
    "DeclaredSource",
    "HealthReport",
    "HubCommand",
    "NodeState",
    "Queue",
    "SourceReport",
    "control_schema",
]
