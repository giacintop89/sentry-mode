"""Validated voice effects shared by synthesized speech and live phone audio."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VoiceEffects(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    preset: Literal["natural", "demon", "chipmunk", "custom"] = "natural"
    pitch: float = Field(default=0, ge=-12, le=12)
    volume: int | None = Field(default=None, ge=0, le=100)

    @property
    def semitones(self) -> float:
        return {"natural": 0, "demon": -7, "chipmunk": 7}.get(self.preset, self.pitch)

    def pitch_filter(self) -> str | None:
        if not self.semitones:
            return None
        return f"rubberband=tempo=1:pitch={2 ** (self.semitones / 12):.8f}"
