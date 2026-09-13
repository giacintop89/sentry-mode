import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from sentry_node.audio.speech import speak
from sentry_node.config import Settings, SpeechConfig
from sentry_node.core.errors import HardwareError


def test_text_is_synthesized_via_stdin_and_temporary_audio_is_deleted():
    paths = []

    def synthesize(args, **kwargs):
        assert kwargs["input_text"] == "Hello; $(echo test)"
        assert kwargs["input_text"] not in args
        output = Path(args[-1])
        paths.append(output)
        with wave.open(str(output), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(bytes(32000))
        return ""

    with (
        patch("sentry_node.audio.speech.command", side_effect=synthesize),
        patch("sentry_node.audio.speech.Speaker") as speaker,
        patch("sentry_node.audio.speech.prepare_playback", return_value=2.75) as prepare,
    ):
        result = speak(
            Settings(speech=SpeechConfig(engine="espeak")),
            "Hello; $(echo test)",
            voice="en",
            rate=175,
        )
    assert result["seconds"] == 1
    assert result["playback_seconds"] == 2.75
    prepare.assert_called_once()
    speaker.return_value.play_file.assert_called_once()
    assert not paths[0].exists()


@pytest.mark.parametrize(
    "text,voice,rate",
    [(" ", "en", 175), ("x" * 1001, "en", 175), ("hello", "../../file", 175), ("hello", "en", 2)],
)
def test_invalid_speech_never_invokes_commands(text, voice, rate):
    with patch("sentry_node.audio.speech.command") as command:
        with pytest.raises(ValueError):
            speak(Settings(), text, voice, rate)
    command.assert_not_called()


def test_disabled_speech():
    with pytest.raises(HardwareError, match="disabled"):
        speak(Settings(speech=SpeechConfig(enabled=False)), "Hello")


def test_piper_voice_selection_and_missing_model_error(tmp_path):
    from sentry_node.audio.speech import speech_engine

    config = Settings(speech=SpeechConfig(engine="piper", model_directory=tmp_path))
    with pytest.raises(HardwareError, match="not installed"):
        speech_engine(config, "it")
    model = tmp_path / "it_IT-paola-medium.onnx"
    model.write_bytes(b"model")
    Path(str(model) + ".json").write_text("{}")
    with patch("sentry_node.audio.speech.importlib.util.find_spec", return_value=object()):
        assert speech_engine(config, "it") == ("piper", model)


def test_synthesis_preserves_multiline_utterance_without_overwriting(tmp_path):

    def synthesize(args, **kwargs):
        assert kwargs["input_text"] == "First line. Second line."
        path = Path(args[args.index("--output-file") + 1])
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(bytes(32000))

    with (
        patch(
            "sentry_node.audio.speech.speech_engine",
            return_value=("piper", tmp_path / "model.onnx"),
        ),
        patch("sentry_node.audio.speech.command", side_effect=synthesize) as command,
        patch("sentry_node.audio.speech.Speaker"),
        patch("sentry_node.audio.speech.prepare_playback", return_value=2.75),
    ):
        result = speak(Settings(), "First line.\nSecond line.")
    assert result["engine"] == "piper"
    assert "--length-scale" in command.call_args.args[0]


def test_wave_header_cannot_hide_truncated_audio(tmp_path):
    from sentry_node.audio.speech import wave_duration

    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(bytes(32000))
    path.write_bytes(path.read_bytes()[:-100])
    with pytest.raises(HardwareError, match="truncated"):
        wave_duration(path)


def test_playback_preparation_includes_padding_and_stereo_resampling(tmp_path):
    from sentry_node.audio.speech import prepare_playback

    output = tmp_path / "playback.wav"

    def encode(args, **kwargs):
        with wave.open(str(output), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(48000)
            stream.writeframes(bytes(48000 * 4 * 2))

    with patch("sentry_node.audio.speech.command", side_effect=encode) as encode_command:
        seconds = prepare_playback(Settings(), tmp_path / "speech.wav", output)
    args = encode_command.call_args.args[0]
    assert (
        args[args.index("-af") + 1] == "aresample=48000:osf=s16,adelay=1000:all=1,apad=pad_dur=0.75"
    )
    assert args[args.index("-ar") + 1] == "48000"
    assert args[args.index("-ac") + 1] == "2"
    assert seconds == 2
