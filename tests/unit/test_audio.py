import json
import subprocess
import wave
from unittest.mock import patch

import pytest

from sentry_mode.audio.playback import command, generate_tone
from sentry_mode.config import SpeakerConfig
from sentry_mode.core.errors import HardwareError
from sentry_mode.core.models import AudioDevice
from sentry_mode.hardware.microphone import discover_devices, select_device
from sentry_mode.hardware.speaker import Speaker


def test_discovery_filters_monitor_sources():
    data = [{"name": "streamcam", "description": "Logitech StreamCam"}, {"name": "speaker.monitor"}]
    with patch("sentry_mode.hardware.microphone.command", return_value=json.dumps(data)):
        assert [d.name for d in discover_devices("sources")] == ["streamcam"]


def test_alsa_fallback():
    with patch(
        "sentry_mode.hardware.microphone.command",
        side_effect=[
            HardwareError("missing"),
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
        patch("sentry_mode.audio.playback.shutil.which", return_value="/bin/tool"),
        patch(
            "sentry_mode.audio.playback.subprocess.run",
            side_effect=subprocess.TimeoutExpired("tool", 8),
        ),
    ):
        with pytest.raises(HardwareError):
            command(["tool"])


def test_speaker_uses_name_without_changing_session_defaults():
    device = AudioDevice("bluez_sink.current", "Living room", "pulse")
    with (
        patch("sentry_mode.hardware.speaker.discover_devices", return_value=[device]),
        patch("sentry_mode.hardware.speaker.command") as call,
    ):
        Speaker(SpeakerConfig(device="Living room")).test_output()
    assert call.call_args.args[0][:2] == ["paplay", "--device=bluez_sink.current"]


def test_native_pipewire_discovery_without_pactl():
    objects = [
        {
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Sink",
                    "node.name": "bluez_output.current",
                    "node.description": "Bluetooth speaker",
                }
            },
        },
        {
            "type": "PipeWire:Interface:Node",
            "info": {"props": {"media.class": "Audio/Source", "node.name": "streamcam"}},
        },
        {
            "type": "PipeWire:Interface:Port",
            "info": {"props": {"media.class": "Audio/Sink", "node.name": "monitor-port"}},
        },
    ]
    with patch(
        "sentry_mode.hardware.microphone.command",
        side_effect=[
            HardwareError("pactl missing"),
            HardwareError("pactl missing"),
            json.dumps(objects),
        ],
    ):
        devices = discover_devices("sinks")
    assert devices == [AudioDevice("bluez_output.current", "Bluetooth speaker", "pipewire")]


SINKS = json.dumps(
    [
        {
            "type": "PipeWire:Interface:Metadata",
            "metadata": [{"key": "default.audio.sink", "value": {"name": "bluez_output.current"}}],
        },
        {
            "id": 56,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {"media.class": "Audio/Sink", "node.name": "hdmi"},
                "params": {"Props": [{"channelVolumes": [0.063997, 0.063997]}, None]},
            },
        },
        {
            "id": 93,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {"media.class": "Audio/Sink", "node.name": "bluez_output.current"},
                "params": {"Props": [{"channelVolumes": [0.10163]}]},
            },
        },
    ]
)


def test_sink_level_is_read_as_the_gain_applied_not_the_cubic_number():
    """A sink a control panel shows at 0.47 is applying a tenth of the signal."""
    devices = [AudioDevice("bluez_output.current", "Bluetooth speaker", "pipewire")]
    with (
        patch("sentry_mode.hardware.speaker.discover_devices", return_value=devices),
        patch("sentry_mode.hardware.speaker.command", return_value=SINKS),
    ):
        level = Speaker(SpeakerConfig(device="Bluetooth speaker")).output_level()
    assert level == {"supported": True, "level": 10, "reason": None}


def test_session_default_speaker_follows_the_sink_pipewire_calls_default():
    devices = [
        AudioDevice("hdmi", "HDMI", "pipewire"),
        AudioDevice("bluez_output.current", "Bluetooth speaker", "pipewire"),
    ]
    with (
        patch("sentry_mode.hardware.speaker.discover_devices", return_value=devices),
        patch("sentry_mode.hardware.speaker.command", return_value=SINKS),
    ):
        assert Speaker(SpeakerConfig()).output_level()["level"] == 10


def test_setting_the_level_asks_for_its_cube_root_against_the_sink_id():
    devices = [AudioDevice("bluez_output.current", "Bluetooth speaker", "pipewire")]
    with (
        patch("sentry_mode.hardware.speaker.discover_devices", return_value=devices),
        patch("sentry_mode.hardware.speaker.command", return_value=SINKS) as call,
    ):
        Speaker(SpeakerConfig(device="Bluetooth speaker")).set_output_level(50)
    assert call.call_args_list[1].args[0] == ["wpctl", "set-volume", "93", "0.793701"]


@pytest.mark.parametrize(
    "backend,devices",
    [
        ("alsa", [AudioDevice("default", "Card", "alsa")]),
        ("pipewire", [AudioDevice("elsewhere", "Another sink", "pipewire")]),
    ],
)
def test_a_speaker_without_a_level_of_its_own_offers_no_control(backend, devices):
    with (
        patch("sentry_mode.hardware.speaker.discover_devices", return_value=devices),
        patch("sentry_mode.hardware.speaker.command", return_value=SINKS),
    ):
        level = Speaker(SpeakerConfig(device=devices[0].description)).output_level()
    assert level["supported"] is False and level["level"] is None and level["reason"]


@pytest.mark.parametrize(
    "backend,name", [("pipewire", "auto"), ("pulse", "auto"), ("alsa", "default")]
)
def test_auto_uses_session_default_instead_of_first_hdmi_card(backend, name):
    selected = select_device([AudioDevice("first-hdmi", "HDMI", backend)], "auto")
    assert selected.name == name and selected.backend == backend


def test_native_pipewire_playback_uses_default_target():
    devices = [
        AudioDevice("hdmi", "HDMI", "pipewire"),
        AudioDevice("bluetooth", "Bluetooth speaker", "pipewire"),
    ]
    with (
        patch("sentry_mode.hardware.speaker.discover_devices", return_value=devices),
        patch("sentry_mode.hardware.speaker.command") as command,
    ):
        Speaker(SpeakerConfig()).test_output()
    assert command.call_args.args[0][:3] == ["pw-play", "--target", "auto"]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_native_pipewire_recording_stops_gracefully(exit_code):
    import signal

    from sentry_mode.audio.playback import record_for

    with (
        patch("sentry_mode.audio.playback.shutil.which", return_value="/usr/bin/pw-record"),
        patch("sentry_mode.audio.playback.subprocess.Popen") as popen,
    ):
        process = popen.return_value
        process.communicate.side_effect = [subprocess.TimeoutExpired("pw-record", 2), ("", "")]
        process.returncode = exit_code
        process.poll.return_value = exit_code
        record_for(["pw-record", "file.wav"], seconds=2)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        process.kill.assert_not_called()


def test_native_pipewire_recording_reaps_unresponsive_child():
    from sentry_mode.audio.playback import record_for

    with (
        patch("sentry_mode.audio.playback.shutil.which", return_value="/usr/bin/pw-record"),
        patch("sentry_mode.audio.playback.subprocess.Popen") as popen,
    ):
        process = popen.return_value
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("pw-record", 2),
            subprocess.TimeoutExpired("pw-record", 3),
            ("", ""),
        ]
        process.poll.return_value = None
        with pytest.raises(HardwareError):
            record_for(["pw-record", "file.wav"], seconds=2)
        process.kill.assert_called_once()
        assert process.communicate.call_count == 3


def test_recording_early_failure_is_not_treated_as_signal_stop():
    from sentry_mode.audio.playback import record_for

    with (
        patch("sentry_mode.audio.playback.shutil.which", return_value="/usr/bin/pw-record"),
        patch("sentry_mode.audio.playback.subprocess.Popen") as popen,
    ):
        process = popen.return_value
        process.communicate.return_value = ("", "")
        process.returncode = 1
        process.poll.return_value = 1
        with pytest.raises(HardwareError):
            record_for(["pw-record", "file.wav"], seconds=2)


def test_live_listening_is_counted_while_a_listener_is_connected():
    from sentry_mode.audio.monitor import AudioMonitor
    from sentry_mode.config import Settings

    monitor = AudioMonitor()
    with (
        patch("sentry_mode.audio.monitor.shutil.which", return_value="/usr/bin/pw-record"),
        patch(
            "sentry_mode.audio.monitor.Microphone.device",
            return_value=AudioDevice("mic", "Microphone", "pipewire"),
        ),
        patch("sentry_mode.audio.monitor.subprocess.Popen") as popen,
    ):
        popen.return_value.poll.return_value = 0
        with monitor.listen(Settings()):
            assert monitor.listening == 1
        assert monitor.listening == 0
        with patch("sentry_mode.audio.monitor.shutil.which", return_value=None):
            with pytest.raises(HardwareError):
                with monitor.listen(Settings()):
                    pass
    assert monitor.listening == 0


def test_shutdown_cancels_and_reaps_active_audio_process():
    import sys
    import threading

    stopped = threading.Event()
    timer = threading.Timer(0.3, stopped.set)
    timer.start()
    try:
        with patch("sentry_mode.audio.playback.subprocess.Popen", wraps=subprocess.Popen) as spawn:
            with pytest.raises(HardwareError, match="cancelled"):
                command([sys.executable, "-c", "import time; time.sleep(60)"], stop_event=stopped)
            spawn.assert_called_once()
    finally:
        timer.cancel()
        timer.join()


def test_cancellable_command_preserves_stdin_and_output():
    import sys
    import threading

    result = command(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        input_text="hello",
        stop_event=threading.Event(),
    )
    assert result == "HELLO\n"
