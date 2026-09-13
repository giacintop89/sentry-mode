import subprocess
import sys
import threading
from unittest.mock import patch

import pytest

from sentry_node.audio.talk import TalkStream
from sentry_node.config import Settings
from sentry_node.core.errors import HardwareError
from sentry_node.core.models import AudioDevice


@pytest.fixture
def talk(tmp_path, monkeypatch):
    output = tmp_path / "received.pcm"
    popen = subprocess.Popen

    def sink(*args, **kwargs):
        return popen(
            [
                sys.executable,
                "-c",
                'import sys; open(sys.argv[1], "wb").write(sys.stdin.buffer.read())',
                str(output),
            ],
            **kwargs,
        )

    monkeypatch.setattr("sentry_node.audio.talk.subprocess.Popen", sink)
    monkeypatch.setattr(
        "sentry_node.audio.talk.Speaker.device", lambda _: AudioDevice("auto", "Test", "pipewire")
    )
    config = Settings(speech={"lead_in_ms": 10, "tail_ms": 10})
    stream = TalkStream(config, threading.Lock())
    try:
        yield stream, output
    finally:
        stream.close()


def test_continuous_audio_preserves_samples_and_releases_speaker(talk):
    stream, output = talk
    token = stream.start()["token"]
    with pytest.raises(BlockingIOError):
        stream.start()
    for i in range(3):
        stream.chunk(token, i, bytes([i, 1]) * 100)
    stream.finish(token)
    assert output.read_bytes() == bytes(960) + b"".join(
        bytes([i, 1]) * 100 for i in range(3)
    ) + bytes(960)
    assert not stream.lock.locked()
    assert not stream.thread.is_alive()
    # A fresh press opens a new session, and stale requests cannot affect it.
    next_token = stream.start()["token"]
    assert next_token != token
    with pytest.raises(ValueError, match="Unknown"):
        stream.finish(token, cancel=True)
    stream.finish(next_token, cancel=True)


def test_bad_audio_or_sequence_never_enters_stream(talk):
    stream, _ = talk
    token = stream.start()["token"]
    for data in [b"", b"x", bytes(9602)]:
        with pytest.raises(ValueError, match="PCM16"):
            stream.chunk(token, 0, data)
    with pytest.raises(ValueError, match="sequence"):
        stream.chunk(token, 1, bytes(10))
    with pytest.raises(ValueError, match="Unknown"):
        stream.chunk("wrong-session", 0, bytes(10))
    assert stream.sequence == 0
    stream.finish(token, cancel=True)
    assert not stream.lock.locked()


def test_lost_phone_expires_and_reaps_player(talk, monkeypatch):
    stream, _ = talk
    monkeypatch.setattr("sentry_node.audio.talk.IDLE_SECONDS", 0.1)
    token = stream.start()["token"]
    stream.thread.join(timeout=2)
    assert not stream.thread.is_alive()
    assert not stream.lock.locked()
    with pytest.raises(HardwareError, match="expired"):
        stream.chunk(token, 0, bytes(100))


def test_shutdown_releases_live_audio(talk):
    stream, _ = talk
    stream.start()
    stream.close()
    assert not stream.thread.is_alive()
    assert not stream.lock.locked()


def test_missing_player_releases_lock(talk):
    stream, _ = talk
    with patch("sentry_node.audio.talk.subprocess.Popen", side_effect=FileNotFoundError("pw-play")):
        with pytest.raises(FileNotFoundError):
            stream.start()
    assert not stream.lock.locked()
