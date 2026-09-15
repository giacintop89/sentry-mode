"""Short built-in tunes synthesized to WAV, so Sentry can play them without audio files."""

import math
import struct
import tempfile
import threading
import wave
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentry_node.config import SpeakerConfig

RATE = 48000

# Each note is (frequency in Hz, seconds, decay); frequency 0 is a rest. A higher decay
# fades the note faster, which makes it ring like a bell instead of holding like a beep.
TUNES: dict[str, tuple[str, list[tuple[float, float, float]]]] = {
    "chime": ("Chime", [(659.25, 0.35, 4), (523.25, 0.8, 3)]),
    "doorbell": ("Doorbell", [(783.99, 0.5, 3), (622.25, 0.9, 2.5)]),
    "alert": ("Alert beeps", [(880, 0.15, 0), (0, 0.08, 0)] * 3),
    "siren": ("Siren", [(740, 0.3, 0), (988, 0.3, 0)] * 3),
    "fanfare": (
        "Fanfare",
        [(523.25, 0.14, 1), (659.25, 0.14, 1), (783.99, 0.14, 1), (1046.5, 0.6, 2)],
    ),
    # The rest lean on the intervals a doorbell avoids -- tritones, minor seconds and a
    # minor arpeggio -- plus blips too short to sing, for a node that sounds like a machine.
    "glitch": (
        "Glitch",
        [(1479.98, 0.05, 0), (0, 0.03, 0), (1046.5, 0.05, 0), (0, 0.03, 0)] * 2
        + [(1479.98, 0.05, 0), (523.25, 0.45, 5)],
    ),
    "neon": (
        "Neon drift",
        [(880, 0.18, 2), (659.25, 0.18, 2), (523.25, 0.18, 2), (440, 1.1, 1.2)],
    ),
    "uplink": (
        "Uplink",
        [(440, 0.07, 1), (587.33, 0.07, 1), (830.61, 0.07, 1), (1174.66, 0.5, 2.5)],
    ),
    "sentinel": (
        "Sentinel",
        [(622.25, 0.22, 0), (659.25, 0.22, 0)] * 3 + [(311.13, 0.6, 3)],
    ),
    "blackout": (
        "Blackout",
        [(196, 0.5, 0.6), (185, 0.5, 0.6), (174.61, 1.2, 1.2)],
    ),
}


def tune_seconds(name: str, repeat: int = 1) -> float:
    return sum(seconds for _, seconds, _ in TUNES[name][1]) * repeat


def generate_tune(
    path: Path, name: str, repeat: int = 1, volume: int = 60, pitch: float = 0
) -> float:
    """Write the tune as 16-bit mono WAV and return its duration in seconds."""
    notes = TUNES[name][1] * repeat
    amplitude = 32767 * 0.5 * volume / 100
    # Pitch is in semitones, so every note moves by the same ratio and the tune keeps
    # its intervals; the note lengths are untouched, and so is the duration.
    shift = 2 ** (pitch / 12)
    ramp = int(RATE * 0.005)
    frames = bytearray()
    for frequency, seconds, decay in notes:
        frequency *= shift
        count = int(RATE * seconds)
        for index in range(count):
            if not frequency:
                frames += b"\0\0"
                continue
            t = index / RATE
            # Short linear ramps at both ends avoid clicks between notes.
            envelope = min(1, index / ramp, (count - index - 1) / ramp) * math.exp(-decay * t)
            wave_value = math.sin(2 * math.pi * frequency * t)
            wave_value += 0.25 * math.sin(4 * math.pi * frequency * t)
            frames += struct.pack("<h", int(amplitude * envelope * wave_value / 1.25))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes(bytes(frames))
    return len(frames) / 2 / RATE


def play_tune(
    speaker: "SpeakerConfig",
    name: str,
    repeat: int = 1,
    volume: int = 60,
    pitch: float = 0,
    stop_event: threading.Event | None = None,
) -> float:
    """Render the tune to a temporary WAV, play it on the node speaker, and delete it."""
    # Imported here: Sentry's config reads TUNES while the settings module is still loading.
    from sentry_node.hardware.speaker import Speaker

    with tempfile.TemporaryDirectory(prefix="sentry-node-tune-") as directory:
        path = Path(directory) / "tune.wav"
        seconds = generate_tune(path, name, repeat, volume, pitch)
        Speaker(speaker).play_file(path, timeout=seconds + 10, stop_event=stop_event)
    return seconds
