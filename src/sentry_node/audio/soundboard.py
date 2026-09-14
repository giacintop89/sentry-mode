"""Saved speech messages, replayed on the node speaker from the soundboard."""

import json
import os
import secrets
import tempfile
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sentry_node.audio.effects import VoiceEffects

MAX_MESSAGES = 48
MAX_FILE_BYTES = 262144


class SoundboardMessage(BaseModel):
    """Everything needed to speak a message again exactly as it was saved."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)
    voice: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_+\-]*$"
    )
    rate: int | None = Field(default=None, ge=80, le=450)
    effects: VoiceEffects = Field(default_factory=VoiceEffects)


class SavedMessage(SoundboardMessage):
    id: str = Field(pattern=r"^[0-9a-f]{16}$")


class Soundboard:
    """A small JSON file of saved messages, written atomically."""

    def __init__(self, path: Path):
        self.path = path
        self.guard = threading.Lock()
        self.messages: list[SavedMessage] = []
        self.error: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Saved soundboard is too large.")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.messages = [SavedMessage.model_validate(item) for item in data["messages"]]
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            self.messages = []
            self.error = f"Saved soundboard could not be loaded: {exc}"

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=self.path.parent, delete=False, encoding="utf-8"
            ) as out:
                name = Path(out.name)
                json.dump({"messages": [m.model_dump(mode="json") for m in self.messages]}, out)
                out.flush()
                os.fsync(out.fileno())
            name.replace(self.path)
            name = None
        finally:
            if name is not None:
                name.unlink(missing_ok=True)

    def listing(self) -> dict:
        with self.guard:
            return {
                "messages": [m.model_dump(mode="json") for m in self.messages],
                "limit": MAX_MESSAGES,
                "error": self.error,
            }

    def save(self, message: SoundboardMessage) -> dict:
        with self.guard:
            fields = message.model_dump(mode="json")
            for existing in self.messages:
                if existing.model_dump(mode="json", exclude={"id"}) == fields:
                    return {"saved": existing.id, "created": False}
            if len(self.messages) >= MAX_MESSAGES:
                raise BlockingIOError(
                    f"The soundboard holds {MAX_MESSAGES} messages; delete one first."
                )
            saved = SavedMessage(id=secrets.token_hex(8), **fields)
            self.messages.append(saved)
            try:
                self._write()
            except OSError:
                self.messages.pop()
                raise
            self.error = None
            return {"saved": saved.id, "created": True}

    def delete(self, message_id: str) -> dict:
        with self.guard:
            remaining = [m for m in self.messages if m.id != message_id]
            if len(remaining) == len(self.messages):
                raise LookupError("That message is no longer on the soundboard.")
            previous, self.messages = self.messages, remaining
            try:
                self._write()
            except OSError:
                self.messages = previous
                raise
            return {"deleted": message_id}

    def get(self, message_id: str) -> SavedMessage:
        with self.guard:
            for message in self.messages:
                if message.id == message_id:
                    return message
        raise LookupError("That message is no longer on the soundboard.")
