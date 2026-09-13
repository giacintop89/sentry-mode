import json
import subprocess
import wave
from unittest.mock import patch

import pytest

from vision_node.audio.playback import command, generate_tone
from vision_node.config import SpeakerConfig
from vision_node.core.errors import HardwareError
from vision_node.core.models import AudioDevice
from vision_node.hardware.microphone import discover_devices, select_device
from vision_node.hardware.speaker import Speaker


def test_discovery_filters_monitor_sources():
    data = [{"name": "streamcam", "description": "Logitech StreamCam"}, {"name": "speaker.monitor"}]
    with patch("vision_node.hardware.microphone.command", return_value=json.dumps(data)):
        assert [d.name for d in discover_devices("sources")] == ["streamcam"]


def test_alsa_fallback():
    with patch(
        "vision_node.hardware.microphone.command",
        side_effect=[
            HardwareError("missing"),
            HardwareError("missing"),
            "card 2: Camera [StreamCam], device 0: USB Audio [USB Audio]",
        ],
    ):
        devices = discover_devices("sources")
    assert devices[0].name == "plughw:CARD=Camera,DEV=0"


def test_human_readable_selection_and_ambiguity():
    devices = [AudioDevice("bluez_sink.address", "Living room", "pulse")]
    assert select_device(devices, "Living room") == devices[0]
    with pytest.raises(HardwareError):
        select_device(devices * 2, "Living room")


def test_generated_tone_is_valid(tmp_path):
    path = tmp_path / "tone.wav"
    generate_tone(path)
    with wave.open(str(path)) as stream:
        assert stream.getnframes() == 48000
        assert stream.getsampwidth() == 2


def test_timeout_becomes_hardware_error():
    with (
        patch("vision_node.audio.playback.shutil.which", return_value="/bin/tool"),
        patch(
            "vision_node.audio.playback.subprocess.run",
            side_effect=subprocess.TimeoutExpired("tool", 8),
        ),
    ):
        with pytest.raises(HardwareError):
            command(["tool"])


def test_speaker_uses_name_without_changing_session_defaults():
    device = AudioDevice("bluez_sink.current", "Living room", "pulse")
    with (
        patch("vision_node.hardware.speaker.discover_devices", return_value=[device]),
        patch("vision_node.hardware.speaker.command") as call,
    ):
        Speaker(SpeakerConfig(device="Living room")).test_output()
    assert call.call_args.args[0][:2] == ["paplay", "--device=bluez_sink.current"]
