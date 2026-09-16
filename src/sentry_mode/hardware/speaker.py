import json
import tempfile
import threading
from pathlib import Path

from sentry_mode.audio.playback import command, generate_tone
from sentry_mode.config import SpeakerConfig
from sentry_mode.core.errors import HardwareError
from sentry_mode.hardware.microphone import discover_devices, select_device


def sink_levels() -> tuple[dict[str, dict], str | None]:
    """Every PipeWire sink by name, with its id and its own gain, and the session default."""
    sinks: dict[str, dict] = {}
    default = None
    for item in json.loads(command(["pw-dump"])):
        if item.get("type") == "PipeWire:Interface:Metadata":
            for entry in item.get("metadata") or []:
                if entry.get("key") == "default.audio.sink":
                    default = (entry.get("value") or {}).get("name")
            continue
        info = item.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Audio/Sink" or not props.get("node.name"):
            continue
        gains = [
            entry["channelVolumes"]
            for entry in (info.get("params") or {}).get("Props") or []
            if entry and entry.get("channelVolumes")
        ]
        if gains:
            sinks[props["node.name"]] = {"id": item["id"], "level": max(gains[0])}
    return sinks, default


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

    def _sink(self) -> dict:
        """The chosen speaker as PipeWire holds it, which is the only backend with the knob."""
        device = self.device()
        if device.backend != "pipewire":
            raise HardwareError(f"{device.backend} devices have no sink level of their own")
        sinks, default = sink_levels()
        sink = sinks.get(default if device.name == "auto" else device.name)
        if sink is None:
            raise HardwareError("the speaker reports no level of its own")
        return sink

    def output_level(self) -> dict:
        """The sink's own gain, as a percentage of unity. It caps everything played through it."""
        try:
            return {"supported": True, "level": round(self._sink()["level"] * 100), "reason": None}
        except (HardwareError, ValueError, KeyError, TypeError) as exc:
            return {"supported": False, "level": None, "reason": str(exc)}

    def set_output_level(self, percent: int) -> dict:
        """Set that gain. Unity is the top: above it the samples are multiplied into clipping."""
        sink = self._sink()
        # wpctl counts in a cubic scale, so what it is given is the cube root of the gain.
        command(["wpctl", "set-volume", str(sink["id"]), f"{(percent / 100) ** (1 / 3):.6f}"])
        return self.output_level()

    def test_output(self) -> None:
        device = self.device()
        with tempfile.TemporaryDirectory(prefix="sentry-mode-tone-") as directory:
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
