import shutil
import subprocess
import sys
import threading
import time
import wave
from unittest.mock import patch

import numpy as np
import pytest

from sentry_node.audio.effects import VoiceEffects
from sentry_node.audio.speech import prepare_playback
from sentry_node.audio.talk import TalkStream
from sentry_node.config import Settings
from sentry_node.core.models import AudioDevice


def tone():
    return (np.sin(2 * np.pi * 997 * np.arange(96000) / 48000) * 4000).astype("<i2")


def frequency(data):
    data = data[len(data) // 4 : len(data) * 3 // 4]
    spectrum = abs(np.fft.rfft(data * np.hanning(len(data))))
    return np.argmax(spectrum) * 48000 / len(data)


@pytest.fixture
def rubberband():
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is not installed")
    if "rubberband" not in subprocess.check_output(
        ["ffmpeg", "-hide_banner", "-filters"], stderr=subprocess.DEVNULL, text=True
    ):
        pytest.skip("FFmpeg rubberband filter is unavailable")


@pytest.mark.parametrize("preset,ratio", [("demon", 2 ** (-7 / 12)), ("chipmunk", 2 ** (7 / 12))])
def test_tts_pitch_changes_frequency_without_changing_duration(tmp_path, rubberband, preset, ratio):
    source, output = tmp_path / "source.wav", tmp_path / "output.wav"
    with wave.open(str(source), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(tone().tobytes())
    config = Settings(speech={"lead_in_ms": 0, "tail_ms": 0})
    seconds = prepare_playback(config, source, output, effects=VoiceEffects(preset=preset))
    with wave.open(str(output)) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, 2).mean(axis=1)
    assert seconds == pytest.approx(2, abs=0.06)
    assert frequency(data) == pytest.approx(997 * ratio, rel=0.02)


@pytest.mark.parametrize("preset,ratio", [("demon", 2 ** (-7 / 12)), ("chipmunk", 2 ** (7 / 12))])
def test_live_effect_pipeline_drains_and_releases_children(tmp_path, rubberband, preset, ratio):
    output = tmp_path / "played.pcm"
    popen = subprocess.Popen
    children = []

    def launch(args, **kwargs):
        if args[0] == "pw-play":
            args = [
                sys.executable,
                "-c",
                'import sys; open(sys.argv[1], "wb").write(sys.stdin.buffer.read())',
                str(output),
            ]
        child = popen(args, **kwargs)
        children.append(child)
        return child

    stream = TalkStream(Settings(speech={"lead_in_ms": 0, "tail_ms": 0}), threading.Lock())
    with (
        patch("sentry_node.audio.talk.subprocess.Popen", side_effect=launch),
        patch(
            "sentry_node.audio.talk.Speaker.device",
            return_value=AudioDevice("auto", "Test", "pipewire"),
        ),
    ):
        try:
            token = stream.start(VoiceEffects(preset=preset))["token"]
            data = tone().tobytes()
            for sequence, index in enumerate(range(0, len(data), 9600)):
                stream.chunk(token, sequence, data[index : index + 9600])
                time.sleep(0.03)
            stream.finish(token)
        finally:
            stream.close()
    data = np.frombuffer(output.read_bytes(), "<i2")
    assert len(data) / 48000 == pytest.approx(2, abs=0.06)
    assert frequency(data) == pytest.approx(997 * ratio, rel=0.02)
    assert not stream.lock.locked()
    assert all(child.poll() is not None for child in children)
