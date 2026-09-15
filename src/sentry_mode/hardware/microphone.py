"""Audio discovery with PulseAudio/PipeWire and ALSA fallback."""

import json
import re
import tempfile
import wave
from pathlib import Path

from sentry_mode.audio.playback import command, record_for
from sentry_mode.config import AudioConfig
from sentry_mode.core.errors import HardwareError
from sentry_mode.core.models import AudioDevice


def discover_devices(kind: str) -> list[AudioDevice]:
    try:
        data = json.loads(command(["pactl", "--format=json", "list", kind]))
        devices = [
            AudioDevice(item["name"], item.get("description", item["name"]), "pulse")
            for item in data
            if kind != "sources" or not item["name"].endswith(".monitor")
        ]
        if devices:
            return devices
    except (HardwareError, ValueError, KeyError, TypeError):
        pass
    # Older pactl versions may lack JSON support.
    try:
        rows = command(["pactl", "list", "short", kind]).splitlines()
        devices = [
            AudioDevice(parts[1], parts[1], "pulse")
            for row in rows
            if len(parts := row.split()) >= 2
            and (kind != "sources" or not parts[1].endswith(".monitor"))
        ]
        if devices:
            return devices
    except HardwareError:
        pass
    # Native PipeWire works even without pactl or a Pulse compatibility server.
    try:
        objects = json.loads(command(["pw-dump"]))
        media_class = "Audio/Source" if kind == "sources" else "Audio/Sink"
        devices = []
        for item in objects:
            props = item.get("info", {}).get("props", {})
            if item.get("type") != "PipeWire:Interface:Node":
                continue
            if props.get("media.class") == media_class and props.get("node.name"):
                name = props["node.name"]
                devices.append(AudioDevice(name, props.get("node.description", name), "pipewire"))
        if devices:
            return devices
    except (HardwareError, ValueError, KeyError, TypeError, AttributeError):
        pass
    tool = "arecord" if kind == "sources" else "aplay"
    try:
        output = command([tool, "-l"])
        return [
            AudioDevice(f"plughw:CARD={card},DEV={device}", description, "alsa")
            for card, description, device in re.findall(
                r"card \d+: (\S+) \[([^\]]+)\], device (\d+):", output
            )
        ]
    except HardwareError:
        return []


def select_device(devices: list[AudioDevice], configured: str) -> AudioDevice:
    if not devices:
        raise HardwareError("no audio devices available; check user audio session and connection")
    if configured == "auto":
        backend = devices[0].backend
        return AudioDevice("default" if backend == "alsa" else "auto", "session default", backend)
    matches = [
        device
        for device in devices
        if configured == device.name or configured.casefold() == device.description.casefold()
    ]
    if len(matches) != 1:
        raise HardwareError(f"audio device {configured!r} is missing or ambiguous")
    return matches[0]


class Microphone:
    def __init__(self, config: AudioConfig):
        self.config = config

    def list_devices(self) -> list[AudioDevice]:
        return discover_devices("sources")

    def device(self) -> AudioDevice:
        if not self.config.enabled:
            raise HardwareError("microphone is disabled")
        return select_device(self.list_devices(), self.config.device)

    def is_available(self) -> bool:
        try:
            self.device()
            return True
        except HardwareError:
            return False

    def ffmpeg_input(self) -> list[str]:
        """ffmpeg input options for the configured microphone, for recordings of any length."""
        device = self.device()
        if device.backend == "alsa":
            return ["-f", "alsa", "-i", device.name]
        # PipeWire sessions also serve the Pulse protocol that ffmpeg reads.
        return ["-f", "pulse", "-i", "default" if device.name == "auto" else device.name]

    def test_input(self) -> dict[str, float]:
        device = self.device()
        with tempfile.TemporaryDirectory(prefix="sentry-mode-input-") as directory:
            path = Path(directory) / "input.wav"
            if device.backend == "pulse":
                args = [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "pulse",
                    "-i",
                    "default" if device.name == "auto" else device.name,
                    "-t",
                    "2",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-acodec",
                    "pcm_s16le",
                    str(path),
                ]
            elif device.backend == "pipewire":
                args = [
                    "pw-record",
                    "--target",
                    device.name,
                    "--rate",
                    "16000",
                    "--channels",
                    "1",
                    "--format",
                    "s16",
                    str(path),
                ]
            else:
                args = [
                    "arecord",
                    "-D",
                    device.name,
                    "-d",
                    "2",
                    "-f",
                    "S16_LE",
                    "-r",
                    "16000",
                    "-c",
                    "1",
                    str(path),
                ]
            if device.backend == "pipewire":
                record_for(args, seconds=2)
            else:
                command(args, timeout=10)
            try:
                with wave.open(str(path), "rb") as stream:
                    frames = stream.getnframes()
                    if frames == 0:
                        raise HardwareError("microphone recorded no samples")
                    return {"seconds": frames / stream.getframerate()}
            except (OSError, wave.Error) as exc:
                raise HardwareError(f"invalid microphone recording: {exc}") from exc
