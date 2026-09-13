"""Bounded subprocess operations and generated PCM test audio."""

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

from vision_node.core.errors import HardwareError


def command(args: list[str], timeout: float = 8) -> str:
    if not shutil.which(args[0]):
        raise HardwareError(f"{args[0]} is not installed")
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise HardwareError(f"{args[0]} failed: {detail}") from exc
    return result.stdout


def generate_tone(path: Path, seconds: float = 1, volume: int = 70) -> None:
    rate = 48000
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        samples = []
        count = int(rate * seconds)
        for index in range(count):
            envelope = min(1, index / 480, (count - index - 1) / 480)
            value = int(
                32767 * volume / 100 * 0.3 * envelope * math.sin(2 * math.pi * 440 * index / rate)
            )
            samples.append(struct.pack("<h", value))
        stream.writeframes(b"".join(samples))
