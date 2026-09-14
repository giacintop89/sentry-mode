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

from sentry_node.audio.effects import VoiceEffects
from sentry_node.audio.tunes import TUNES
from sentry_node.vision.labels import CLASSES


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


class TTSAction(Model):
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


class TuneAction(Model):
    type: Literal["tune"] = "tune"
    tune: str = "chime"
    repeat: int = Field(default=1, ge=1, le=5)
    volume: int = Field(default=60, ge=0, le=100)

    @field_validator("tune")
    @classmethod
    def known_tune(cls, value):
        if value not in TUNES:
            raise ValueError("Select a supported tune.")
        return value


class SoundAction(Model):
    type: Literal["sound"] = "sound"
    sound: str = Field(pattern=r"^[a-z0-9-]{1,40}-[0-9a-f]{8}$")
    repeat: int = Field(default=1, ge=1, le=5)
    volume: int = Field(default=80, ge=0, le=100)


class PhotoAction(Model):
    type: Literal["photo"] = "photo"
    count: int = Field(default=1, ge=1, le=20)
    interval_seconds: float = Field(default=2, ge=0.5, le=60)


class VideoAction(Model):
    type: Literal["video"] = "video"
    duration_seconds: int = Field(default=10, ge=1, le=60)
    audio: StrictBool = True


class AudioAction(Model):
    type: Literal["audio"] = "audio"
    duration_seconds: int = Field(default=10, ge=1, le=60)


class SSHAction(Model):
    type: Literal["ssh"] = "ssh"
    command_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class TelegramAction(Model):
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


class Rule(Model):
    name: str = Field(min_length=1, max_length=64)
    enabled: StrictBool = True
    object: str
    min_confidence: float = Field(default=0.70, ge=0.1, le=0.99)
    min_count: int = Field(default=1, ge=1, le=20)
    consecutive_detections: int = Field(default=3, ge=1, le=20)
    rearm_after_absence_seconds: float = Field(default=10, ge=1, le=300)
    cooldown_seconds: float = Field(default=60, ge=0, le=3600)
    region: tuple[float, float, float, float] | None = None
    actions: list[
        Annotated[
            PhotoAction
            | AudioAction
            | VideoAction
            | TTSAction
            | TuneAction
            | SoundAction
            | SSHAction
            | TelegramAction,
            Field(discriminator="type"),
        ]
    ] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def distinct_actions(self):
        if len({action.type for action in self.actions}) != len(self.actions):
            raise ValueError(
                "A rule supports one action of each type: "
                "photo, audio recording, video, TTS, tune, audio file, SSH, and Telegram."
            )
        return self

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
        if value is not None:
            x1, y1, x2, y2 = value
            if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                raise ValueError(
                    "Region must be normalized left, top, right, bottom with positive area."
                )
        return value


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
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) for key in self.ssh_commands):
            raise ValueError(
                "SSH command IDs must contain only letters, digits, underscores or hyphens."
            )
        for rule in self.rules:
            for action in rule.actions:
                if isinstance(action, SSHAction) and action.command_id not in self.ssh_commands:
                    raise ValueError(f"Unknown SSH command: {action.command_id}")
        return self
