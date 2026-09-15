"""Bounded subprocess operations and generated PCM test audio."""

import math
import shutil
import signal
import struct
import subprocess
import threading
import time
import wave
from pathlib import Path

from sentry_node.core.errors import HardwareError


def command(
    args: list[str],
    timeout: float = 8,
    input_text: str | None = None,
    stop_event: threading.Event | None = None,
) -> str:
    if not shutil.which(args[0]):
        raise HardwareError(f"{args[0]} is not installed")
    if stop_event is not None:
        return _cancellable_command(args, timeout, input_text, stop_event)
    try:
        result = subprocess.run(
            args, input=input_text, capture_output=True, text=True, timeout=timeout, check=True
        )
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


def record_for(args: list[str], seconds: float) -> None:
    """Stop native PipeWire recording with SIGINT so its WAV header is finalized."""
    if not shutil.which(args[0]):
        raise HardwareError(f"{args[0]} is not installed")
    process = None
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stopped_at_limit = False
        try:
            _, stderr = process.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            stopped_at_limit = True
            process.send_signal(signal.SIGINT)
            _, stderr = process.communicate(timeout=3)
        # pw-record 1.4 exits 1 on its signal-driven normal stop. The microphone
        # adapter still validates the resulting WAV and requires nonzero samples.
        normal_signal_stop = stopped_at_limit and process.returncode == 1 and not stderr.strip()
        if process.returncode != 0 and not normal_signal_stop:
            raise HardwareError(f"{args[0]} failed: {stderr or process.returncode}")
    except (OSError, subprocess.SubprocessError) as exc:
        raise HardwareError(f"{args[0]} recording failed: {exc}") from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()


def _cancellable_command(args, timeout, input_text, stop_event) -> str:
    process = None
    try:
        if stop_event.is_set():
            raise HardwareError("Audio cancelled during shutdown.")
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + timeout
        pending_input = input_text
        while True:
            if stop_event.is_set():
                raise HardwareError("Audio cancelled during shutdown.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HardwareError(f"{args[0]} timed out after {timeout:g} seconds")
            try:
                stdout, stderr = process.communicate(
                    input=pending_input, timeout=min(0.2, remaining)
                )
                if process.returncode != 0:
                    raise HardwareError(f"{args[0]} failed: {stderr or process.returncode}")
                return stdout
            except subprocess.TimeoutExpired:
                pending_input = None
    except (OSError, subprocess.SubprocessError) as exc:
        raise HardwareError(f"{args[0]} failed: {exc}") from exc
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
