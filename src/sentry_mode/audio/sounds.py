"""Uploaded audio files, converted once to plain WAV so every speaker backend can play them."""

import re
import secrets
import tempfile
import threading
import wave
from pathlib import Path

from sentry_mode.audio.playback import command
from sentry_mode.config import Settings
from sentry_mode.core.errors import HardwareError
from sentry_mode.hardware.speaker import Speaker

MAX_SOUNDS = 64
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SECONDS = 60
SOUND_ID = re.compile(r"^[a-z0-9-]{1,40}-[0-9a-f]{8}$")


def slug(text: str) -> str:
    stem = Path(text).stem if text else ""
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:40] or "sound"


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as stream:
        return stream.getnframes() / stream.getframerate()


class SoundLibrary:
    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = threading.Lock()

    def path(self, sound_id: str) -> Path:
        path = self.directory / f"{sound_id}.wav"
        if not SOUND_ID.fullmatch(sound_id) or not path.is_file():
            raise LookupError("That audio file is no longer on the node.")
        return path

    def exists(self, sound_id: str) -> bool:
        try:
            self.path(sound_id)
            return True
        except LookupError:
            return False

    def listing(self) -> dict:
        try:
            found = [p for p in self.directory.glob("*.wav") if SOUND_ID.fullmatch(p.stem)]
        except OSError:
            found = []
        sounds = []
        for path in sorted(found, key=lambda p: p.name):
            try:
                seconds = wav_seconds(path)
            except (OSError, wave.Error, EOFError, ZeroDivisionError):
                continue
            sounds.append(
                {
                    "id": path.stem,
                    "name": path.stem[:-9].replace("-", " "),
                    "seconds": round(seconds, 1),
                }
            )
        return {"sounds": sounds, "limit": MAX_SOUNDS, "max_seconds": MAX_SECONDS}

    def add(self, filename: str, data: bytes) -> dict:
        """Decode any format ffmpeg understands; only the converted mono WAV is kept."""
        if not data:
            raise ValueError("The audio file is empty.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(f"Audio files are limited to {MAX_UPLOAD_BYTES // 1048576} MB.")
        with self.lock:
            if len(self.listing()["sounds"]) >= MAX_SOUNDS:
                raise BlockingIOError(f"The node holds {MAX_SOUNDS} audio files; delete one first.")
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            sound_id = f"{slug(filename)}-{secrets.token_hex(4)}"
            # Convert inside the library folder so the finished file is renamed, never
            # copied across filesystems.
            with tempfile.TemporaryDirectory(prefix=".upload-", dir=self.directory) as directory:
                source = Path(directory) / "upload"
                output = Path(directory) / "sound.wav"
                source.write_bytes(data)
                try:
                    # Decoding runs in ffmpeg with no network access and a hard time limit.
                    command(
                        [
                            "ffmpeg",
                            "-nostdin",
                            "-v",
                            "error",
                            "-y",
                            "-protocol_whitelist",
                            "file,pipe",
                            "-i",
                            str(source),
                            "-vn",
                            "-t",
                            str(MAX_SECONDS + 1),
                            "-ar",
                            "48000",
                            "-ac",
                            "1",
                            "-c:a",
                            "pcm_s16le",
                            str(output),
                        ],
                        timeout=60,
                    )
                    seconds = wav_seconds(output)
                except (HardwareError, OSError, wave.Error, EOFError, ZeroDivisionError) as exc:
                    raise ValueError(f"Could not read that audio file: {exc}") from exc
                if seconds <= 0:
                    raise ValueError("The audio file contains no sound.")
                if seconds > MAX_SECONDS:
                    raise ValueError(f"Audio files are limited to {MAX_SECONDS} seconds.")
                output.replace(self.directory / f"{sound_id}.wav")
        return {"saved": sound_id, **self.listing()}

    def delete(self, sound_id: str) -> dict:
        with self.lock:
            self.path(sound_id).unlink()
        return self.listing()

    def play(
        self,
        settings: Settings,
        sound_id: str,
        *,
        repeat: int = 1,
        volume: int = 100,
        stop_event: threading.Event | None = None,
    ) -> dict:
        source = self.path(sound_id)
        speaker = Speaker(settings.speaker)
        device = speaker.device()
        with tempfile.TemporaryDirectory(prefix="sentry-mode-sound-") as directory:
            output = Path(directory) / "playback.wav"
            # Same lead-in and tail as speech so Bluetooth speakers do not clip the start.
            filters = ",".join(
                [
                    f"aloop=loop={repeat - 1}:size=2147483647",
                    f"volume={volume / 100:g}",
                    f"adelay={settings.speech.lead_in_ms}:all=1",
                    f"apad=pad_dur={settings.speech.tail_ms / 1000:g}",
                ]
            )
            command(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-af",
                    filters,
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    str(output),
                ],
                timeout=30,
                stop_event=stop_event,
            )
            seconds = wav_seconds(output)
            speaker.play_file(output, timeout=seconds + 10, device=device, stop_event=stop_event)
        return {"message": "Audio file played through the node speaker.", "seconds": seconds}
