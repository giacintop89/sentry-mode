import tempfile
from pathlib import Path

from vision_node.audio.playback import command, generate_tone
from vision_node.config import SpeakerConfig
from vision_node.core.errors import HardwareError
from vision_node.hardware.microphone import discover_devices, select_device


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
        with tempfile.TemporaryDirectory(prefix="vision-node-tone-") as directory:
            path = Path(directory) / "tone.wav"
            generate_tone(path, volume=self.config.volume)
            if device.backend == "pulse":
                args = ["paplay"]
                if device.name != "auto":
                    args.append(f"--device={device.name}")
            else:
                args = ["aplay", "-D", device.name]
            command([*args, str(path)])
