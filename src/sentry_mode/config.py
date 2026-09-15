"""Validated YAML configuration with environment overrides and safe defaults."""

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sentry_mode.sentry.config import SentryConfig


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeConfig(Section):
    name: str = Field(default="sentry-mode", min_length=1)


class CameraConfig(Section):
    enabled: bool = True
    device: int | str = 0
    width: int = Field(default=1920, gt=0)
    height: int = Field(default=1080, gt=0)
    fps: float = Field(default=30, gt=0)

    @field_validator("device")
    @classmethod
    def validate_device(cls, value: int | str) -> int | str:
        if isinstance(value, str) and value.isdecimal():
            return int(value)
        if isinstance(value, int) and value < 0 or isinstance(value, str) and not value.strip():
            raise ValueError("camera device must be a nonnegative index or a path")
        return value


class AudioConfig(Section):
    enabled: bool = True
    device: str = Field(default="auto", min_length=1)


class DetectionConfig(Section):
    enabled: bool = False
    model: Path = Path("models/yolox/yolox_nano.onnx")
    confidence: float = Field(default=0.45, ge=0.1, le=0.95)
    max_fps: float = Field(default=2, ge=0.2, le=10)


class SpeakerConfig(AudioConfig):
    volume: int = Field(default=70, ge=0, le=100)
    pipewire_latency_ms: int = Field(default=250, ge=20, le=1000)


class SpeechConfig(Section):
    enabled: bool = True
    engine: Literal["auto", "kokoro", "piper", "espeak"] = "auto"
    model_directory: Path = Path("models/piper")
    kokoro_directory: Path = Path("models/kokoro")
    models: dict[str, str] = Field(
        default_factory=lambda: {"en": "en_US-lessac-medium", "it": "it_IT-paola-medium"}
    )
    lead_in_ms: int = Field(default=1000, ge=0, le=5000)
    tail_ms: int = Field(default=750, ge=0, le=5000)
    voice: str = Field(default="en", min_length=1, max_length=64)
    rate: int = Field(default=175, ge=80, le=450)

    @field_validator("models")
    @classmethod
    def validate_models(cls, models: dict[str, str]) -> dict[str, str]:
        import re

        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in models.values()):
            raise ValueError("speech model names must be simple filenames without a path")
        return models


class LoggingConfig(Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SENTRY_MODE_", env_nested_delimiter="__", extra="forbid"
    )
    node: NodeConfig = Field(default_factory=NodeConfig)
    camera: CameraConfig = Field(default_factory=CameraConfig)
    microphone: AudioConfig = Field(default_factory=AudioConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    sentry: SentryConfig = Field(default_factory=SentryConfig)
    sentry_state_file: Path = Path(".local/sentry.json")
    soundboard_file: Path = Path(".local/soundboard.json")
    captures_directory: Path = Path(".local/captures")
    sounds_directory: Path = Path(".local/sounds")
    web_frame_origins: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("web_frame_origins")
    @classmethod
    def validate_frame_origins(cls, values: list[str]) -> list[str]:
        for origin in values:
            if not re.fullmatch(
                r"https?://(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?",
                origin,
            ):
                raise ValueError("Frame origins must be exact HTTP(S) origins without paths.")
            port = urlsplit(origin).port
            if port is not None and not 1 <= port <= 65535:
                raise ValueError("Frame origin port must be between 1 and 65535.")
        return values

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return env_settings, init_settings, file_secret_settings


def config_path() -> Path | None:
    """The YAML file this process was started with, if any."""
    selected = os.environ.get("SENTRY_MODE_CONFIG")
    return Path(selected) if selected else None


def save_sections(path: Path, sections: dict[str, dict]) -> None:
    """Merge section values into the YAML file, leaving every other setting untouched."""
    data = {}
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
        if not isinstance(data, dict):
            raise ValueError("configuration must be a YAML mapping")
    for section, values in sections.items():
        current = data.get(section)
        data[section] = {**current, **values} if isinstance(current, dict) else dict(values)
    Settings(**data)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_config(path: str | Path | None = None) -> Settings:
    selected = path or os.environ.get("SENTRY_MODE_CONFIG")
    if selected is None:
        return Settings()
    with Path(selected).open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("configuration must be a YAML mapping")
    return Settings(**data)
