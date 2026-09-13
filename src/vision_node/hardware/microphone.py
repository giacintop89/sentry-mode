"""Audio discovery with PulseAudio/PipeWire and ALSA fallback."""

import json
import re
import tempfile
import wave
from pathlib import Path

from vision_node.audio.playback import command
from vision_node.config import AudioConfig
from vision_node.core.errors import HardwareError
from vision_node.core.models import AudioDevice


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
        if devices[0].backend == "pulse":
            return AudioDevice("auto", "session default", "pulse")
        return devices[0]
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

    def test_input(self) -> dict[str, float]:
        device = self.device()
        with tempfile.TemporaryDirectory(prefix="vision-node-input-") as directory:
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
            command(args, timeout=10)
            try:
                with wave.open(str(path), "rb") as stream:
                    frames = stream.getnframes()
                    if frames == 0:
                        raise HardwareError("microphone recorded no samples")
                    return {"seconds": frames / stream.getframerate()}
            except (OSError, wave.Error) as exc:
                raise HardwareError(f"invalid microphone recording: {exc}") from exc
