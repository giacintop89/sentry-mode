import tempfile
import threading
from pathlib import Path

from sentry_node.audio.playback import command, generate_tone
from sentry_node.config import SpeakerConfig
from sentry_node.core.errors import HardwareError
from sentry_node.hardware.microphone import discover_devices, select_device


class Speaker:
    def __init__(self, config: SpeakerConfig):
        self.config = config

    def list_devices(self):
        return discover_devices("sinks")

    def device(self):
        if not self.config.enabled:
            raise HardwareError("speaker is disabled")
        return select_device(self.list_devices(), self.config.device)

    def is_available(self) -> bool:
        try:
            self.device()
            return True
        except HardwareError:
            return False

    def test_output(self) -> None:
        device = self.device()
        with tempfile.TemporaryDirectory(prefix="sentry-node-tone-") as directory:
            path = Path(directory) / "tone.wav"
            generate_tone(path, volume=self.config.volume)
            self.play_file(path, device=device)

    def play_file(
        self,
        path: Path,
        *,
        timeout: float = 8,
        device=None,
        stop_event: threading.Event | None = None,
    ) -> None:
        device = device if device is not None else self.device()
        if device.backend == "pulse":
            args = ["paplay"]
            if device.name != "auto":
                args.append(f"--device={device.name}")
        elif device.backend == "pipewire":
            args = [
                "pw-play",
                "--target",
                device.name,
                "--latency",
                f"{self.config.pipewire_latency_ms}ms",
            ]
        else:
            args = ["aplay", "-D", device.name]
        try:
            command([*args, str(path)], timeout=timeout, stop_event=stop_event)
        except HardwareError as exc:
            raise HardwareError(
                f"Speaker playback failed ({device.backend}, {device.name}): {exc}"
            ) from exc
