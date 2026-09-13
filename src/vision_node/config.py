"""Validated YAML configuration with environment overrides and safe defaults."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeConfig(Section):
    name: str = Field(default="vision-node", min_length=1)


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


class SpeakerConfig(AudioConfig):
    volume: int = Field(default=70, ge=0, le=100)


class LoggingConfig(Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VISION_NODE_", env_nested_delimiter="__", extra="forbid"
    )
    node: NodeConfig = Field(default_factory=NodeConfig)
    camera: CameraConfig = Field(default_factory=CameraConfig)
    microphone: AudioConfig = Field(default_factory=AudioConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return env_settings, init_settings, file_secret_settings


def load_config(path: str | Path | None = None) -> Settings:
    selected = path or os.environ.get("VISION_NODE_CONFIG")
    if selected is None:
        return Settings()
    with Path(selected).open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("configuration must be a YAML mapping")
    return Settings(**data)
