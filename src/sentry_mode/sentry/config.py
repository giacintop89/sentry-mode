"""Validated, persistable Sentry rules; arming is always a runtime action."""

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    field_validator,
    model_validator,
)

from sentry_mode.audio.effects import VoiceEffects
from sentry_mode.audio.tunes import TUNES
from sentry_mode.sources.models import PRIMARY_CAMERA, PRIMARY_MICROPHONE, SourceRef
from sentry_mode.vision.labels import CLASSES


def source_id(value: str) -> str:
    """A source identifier as the registry spells it: `legacy-primary` or `node.name`."""
    return SourceRef.parse(value).id


SourceId = Annotated[str, Field(min_length=1, max_length=81)]

TRIGGER_SOURCE = "trigger_source"
"""What a photo or video names to use the camera that set the rule off.

The underscore keeps it apart from every real source name, which may not contain one."""


def camera_id(value: str) -> str:
    return value if value == TRIGGER_SOURCE else source_id(value)


IfUnavailable = Literal["fail", "skip", "stop"]
"""What a photo or video does when its camera has nothing recent to give.

`fail` reports the step as failed, `skip` reports it as skipped, and `stop` also drops the
steps of that sequence still to come. None of them takes the picture from another camera.
"""


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


class Step(Model):
    """A rule's actions are a sequence of steps.

    Steps run one after another in the order the editor lists them; a step marked
    with_previous starts at the same moment as the step before it instead of waiting
    for it, so consecutive marked steps form one group that runs together.
    """

    with_previous: StrictBool = False


class WaitAction(Step):
    type: Literal["wait"] = "wait"
    seconds: float = Field(default=2, ge=0.1, le=60)


class TTSAction(Step):
    type: Literal["tts"] = "tts"
    text: str = Field(min_length=1, max_length=1000)
    voice: str = Field(default="en", pattern=r"^[A-Za-z0-9][A-Za-z0-9_+\-]{0,63}$")
    rate: int = Field(default=175, ge=80, le=450)
    effects: VoiceEffects = Field(default_factory=VoiceEffects)

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Speech text cannot be blank.")
        return value


class TuneAction(Step):
    type: Literal["tune"] = "tune"
    tune: str = "chime"
    repeat: int = Field(default=1, ge=1, le=5)
    volume: int = Field(default=60, ge=0, le=100)
    pitch: float = Field(default=0, ge=-24, le=24)

    @field_validator("tune")
    @classmethod
    def known_tune(cls, value):
        if value not in TUNES:
            raise ValueError("Select a supported tune.")
        return value


class SoundAction(Step):
    type: Literal["sound"] = "sound"
    sound: str = Field(pattern=r"^[a-z0-9-]{1,40}-[0-9a-f]{8}$")
    repeat: int = Field(default=1, ge=1, le=5)
    volume: int = Field(default=80, ge=0, le=100)


class PhotoAction(Step):
    """Photos from one camera. Before rules could name a camera it was always this node's."""

    type: Literal["photo"] = "photo"
    count: int = Field(default=1, ge=1, le=20)
    interval_seconds: float = Field(default=2, ge=0.5, le=60)
    source_id: SourceId = PRIMARY_CAMERA
    if_unavailable: IfUnavailable = "fail"

    @field_validator("source_id")
    @classmethod
    def known_shape(cls, value):
        return camera_id(value)


class VideoAction(Step):
    """A clip from one camera, with sound only from a microphone the rule chose.

    With `audio_source_id` left out, a clip from this node's camera keeps the sound of this
    node's microphone, as it always had. A clip from any other camera is then silent: moving
    a rule to another camera never records sound from somewhere else.
    """

    type: Literal["video"] = "video"
    duration_seconds: int = Field(default=10, ge=1, le=60)
    audio: StrictBool = True
    source_id: SourceId = PRIMARY_CAMERA
    audio_source_id: SourceId | None = None
    if_unavailable: IfUnavailable = "fail"

    @field_validator("source_id")
    @classmethod
    def known_camera(cls, value):
        return camera_id(value)

    @field_validator("audio_source_id")
    @classmethod
    def known_shape(cls, value):
        return None if value is None else source_id(value)

    @property
    def microphone(self) -> str | None:
        """The microphone this clip records, or None for a silent one."""
        if not self.audio:
            return None
        if self.audio_source_id is not None:
            return self.audio_source_id
        return PRIMARY_MICROPHONE if self.source_id == PRIMARY_CAMERA else None


class AudioAction(Step):
    type: Literal["audio"] = "audio"
    duration_seconds: int = Field(default=10, ge=1, le=60)
    audio_source_id: SourceId = PRIMARY_MICROPHONE

    @field_validator("audio_source_id")
    @classmethod
    def known_shape(cls, value):
        return source_id(value)


class SSHAction(Step):
    type: Literal["ssh"] = "ssh"
    command_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class TelegramAction(Step):
    type: Literal["telegram"] = "telegram"
    text: str = Field(min_length=1, max_length=4096)
    silent: StrictBool = False

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Telegram message cannot be blank.")
        return value


class TelegramConfig(Model):
    bot_token: SecretStr = Field(default_factory=lambda: SecretStr(""))
    chat_id: str = Field(default="", max_length=128, pattern=r"^-?[0-9]+$|^@[A-Za-z0-9_]+$|^$")

    @field_validator("bot_token")
    @classmethod
    def valid_token(cls, value):
        token = value.get_secret_value()
        if token and not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,128}", token):
            raise ValueError("Enter a valid Telegram bot token from BotFather.")
        return value


class SSHCommand(Model):
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:\-]*$")
    user: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$|^$")
    port: int = Field(default=22, ge=1, le=65535)
    command: str = Field(min_length=1, max_length=4096)
    identity_file: str = Field(default="", max_length=1024)
    timeout_seconds: int = Field(default=5, ge=1, le=60)

    @field_validator("command", "identity_file")
    @classmethod
    def no_null(cls, value):
        if "\0" in value:
            raise ValueError("NUL characters are not allowed.")
        return value

    @field_validator("command")
    @classmethod
    def command_not_blank(cls, value):
        if not value.strip():
            raise ValueError("SSH command cannot be blank.")
        return value


Action = Annotated[
    WaitAction
    | PhotoAction
    | AudioAction
    | VideoAction
    | TTSAction
    | TuneAction
    | SoundAction
    | SSHAction
    | TelegramAction,
    Field(discriminator="type"),
]


class Rule(Model):
    """A rule as the first version of the editor saves it: always about this node's camera.

    It is still what `/api/sentry/config` speaks. The engine works on the second version
    below, and converts between the two without losing anything the first can express.
    """

    name: str = Field(min_length=1, max_length=64)
    enabled: StrictBool = True
    object: str
    min_confidence: float = Field(default=0.70, ge=0.1, le=0.99)
    min_count: int = Field(default=1, ge=1, le=20)
    consecutive_detections: int = Field(default=3, ge=1, le=20)
    rearm_after_absence_seconds: float = Field(default=10, ge=1, le=300)
    cooldown_seconds: float = Field(default=60, ge=0, le=3600)
    region: tuple[float, float, float, float] | None = None
    actions: list[Action] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def runnable_sequence(self):
        return runnable(self)

    @field_validator("object")
    @classmethod
    def known_object(cls, value):
        if value not in CLASSES:
            raise ValueError("Select a supported object category.")
        return value

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Rule name cannot be blank.")
        return value.strip()

    @field_validator("region")
    @classmethod
    def valid_region(cls, value):
        return region(value)


def runnable(rule):
    if all(isinstance(action, WaitAction) for action in rule.actions):
        raise ValueError("A rule needs at least one step that does something besides wait.")
    return rule


def region(value):
    if value is not None:
        x1, y1, x2, y2 = value
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError(
                "Region must be normalized left, top, right, bottom with positive area."
            )
    return value


def check_references(config) -> None:
    """What both versions of the rules document require of each other's parts."""
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) for key in config.ssh_commands):
        raise ValueError(
            "SSH command IDs must contain only letters, digits, underscores or hyphens."
        )
    for rule in config.rules:
        for action in rule.actions:
            if isinstance(action, SSHAction) and action.command_id not in config.ssh_commands:
                raise ValueError(f"Unknown SSH command: {action.command_id}")


class SentryConfig(Model):
    detection_fps: float = Field(default=2, ge=0.5, le=5)
    test_mode: StrictBool = False
    action_ttl_seconds: int = Field(default=15, ge=1, le=120)
    rules: list[Rule] = Field(
        default_factory=lambda: [
            Rule(
                name="Person at entrance",
                object="person",
                actions=[TTSAction(text="Hello. Please wait here.")],
            )
        ],
        max_length=32,
    )
    ssh_commands: dict[str, SSHCommand] = Field(default_factory=dict, max_length=32)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)

    @model_validator(mode="after")
    def valid_references(self):
        if len({r.name for r in self.rules}) != len(self.rules):
            raise ValueError("Rule names must be unique.")
        check_references(self)
        return self


# -- version 2 -------------------------------------------------------------------------

SCHEMA_VERSION = 2

RULE_ID = r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
"""A rule's permanent name. Renaming a rule changes what people read, never this."""


class VisionTrigger(Model):
    """An object seen by a camera, confirmed over consecutive detections.

    These are exactly the fields a first-version rule had at its top level, moved here
    because they describe what sets the rule off, not the rule.
    """

    type: Literal["vision"] = "vision"
    source_id: SourceId = PRIMARY_CAMERA
    object: str
    min_confidence: float = Field(default=0.70, ge=0.1, le=0.99)
    min_count: int = Field(default=1, ge=1, le=20)
    consecutive_detections: int = Field(default=3, ge=1, le=20)
    rearm_after_absence_seconds: float = Field(default=10, ge=1, le=300)
    region: tuple[float, float, float, float] | None = None

    @field_validator("source_id")
    @classmethod
    def known_shape(cls, value):
        return source_id(value)

    @field_validator("object")
    @classmethod
    def known_object(cls, value):
        if value not in CLASSES:
            raise ValueError("Select a supported object category.")
        return value

    @field_validator("region")
    @classmethod
    def valid_region(cls, value):
        return region(value)


EVENT_KIND = r"^[a-z][a-z0-9]*\.[a-z][a-z0-9_]*$"


class SensorEventTrigger(Model):
    """A reported change on a sensor, such as a PIR going high.

    `edge` says which way: `rising` for a value that became true, `falling` for one that
    became false. A reading of doubtful quality is never taken as either.
    """

    type: Literal["sensor_event"] = "sensor_event"
    source_id: SourceId
    kind: str = Field(pattern=EVENT_KIND)
    edge: Literal["rising", "falling", "any"] = "rising"

    @field_validator("source_id")
    @classmethod
    def known_shape(cls, value):
        return source_id(value)


class ThresholdTrigger(Model):
    """A measurement past a limit for long enough, released only once it is well back.

    `hysteresis` is how far back the value has to come before the rule can fire again, so
    a reading hovering on the limit does not fire on every sample. A value that was
    already past the limit when Sentry started is where things stand, not a crossing.
    """

    type: Literal["threshold"] = "threshold"
    source_id: SourceId
    kind: str = Field(pattern=EVENT_KIND)
    above: float | None = None
    below: float | None = None
    hysteresis: float = Field(default=0, ge=0)
    for_seconds: float = Field(default=0, ge=0, le=3600)

    @field_validator("source_id")
    @classmethod
    def known_shape(cls, value):
        return source_id(value)

    @model_validator(mode="after")
    def one_limit(self):
        if (self.above is None) == (self.below is None):
            raise ValueError("A threshold needs exactly one of above or below.")
        return self


class AudioEventTrigger(Model):
    """A sound class heard by a microphone. Accepted in a document; not armable yet."""

    type: Literal["audio_event"] = "audio_event"
    source_id: SourceId
    kind: str = Field(pattern=EVENT_KIND)
    min_level: float | None = None

    @field_validator("source_id")
    @classmethod
    def known_shape(cls, value):
        return source_id(value)


class PresenceStateTrigger(Model):
    """A known device arriving or leaving. Accepted in a document; not armable yet."""

    type: Literal["presence_state"] = "presence_state"
    source_id: SourceId
    state: Literal["present", "absent"] = "present"

    @field_validator("source_id")
    @classmethod
    def known_shape(cls, value):
        return source_id(value)


class HealthEventTrigger(Model):
    """A node going stale or offline, as the hub sees it. Not armable yet.

    The hub raises these about a node, so a rule on them has to work precisely when the
    node itself is silent; it depends on the hub's own health service, never on the node.
    """

    type: Literal["health_event"] = "health_event"
    node_id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
    state: Literal["stale", "offline", "online"] = "offline"


Trigger = Annotated[
    VisionTrigger
    | SensorEventTrigger
    | ThresholdTrigger
    | AudioEventTrigger
    | PresenceStateTrigger
    | HealthEventTrigger,
    Field(discriminator="type"),
]

ARMABLE_TRIGGERS = frozenset({"vision", "sensor_event", "threshold"})
"""Triggers the engine can watch today. The others validate, save, and refuse to arm."""


class RuleV2(Model):
    id: str = Field(pattern=RULE_ID)
    name: str = Field(min_length=1, max_length=64)
    enabled: StrictBool = True
    trigger: Trigger
    cooldown_seconds: float = Field(default=60, ge=0, le=3600)
    actions: list[Action] = Field(min_length=1, max_length=16)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Rule name cannot be blank.")
        return value.strip()

    @model_validator(mode="after")
    def runnable_sequence(self):
        return runnable(self)


def default_rules() -> list[RuleV2]:
    return [
        RuleV2(
            id="person-at-entrance",
            name="Person at entrance",
            trigger=VisionTrigger(object="person"),
            actions=[TTSAction(text="Hello. Please wait here.")],
        )
    ]


FaultPolicy = Literal["global", "isolated"]


class SentryConfigV2(Model):
    """The rules document, second version.

    `fault_policy` decides what a failing source does to the rest: `global` disarms
    everything, as the first version always did; `isolated` stops only the rules that
    need what failed.
    """

    detection_fps: float = Field(default=2, ge=0.5, le=5)
    test_mode: StrictBool = False
    action_ttl_seconds: int = Field(default=15, ge=1, le=120)
    fault_policy: FaultPolicy = "isolated"
    rules: list[RuleV2] = Field(default_factory=default_rules, max_length=32)
    ssh_commands: dict[str, SSHCommand] = Field(default_factory=dict, max_length=32)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)

    @model_validator(mode="after")
    def valid_references(self):
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ValueError("Rule IDs must be unique.")
        if len({r.name for r in self.rules}) != len(self.rules):
            raise ValueError("Rule names must be unique.")
        check_references(self)
        return self
