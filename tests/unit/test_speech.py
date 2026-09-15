import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from sentry_mode.audio.speech import speak
from sentry_mode.config import Settings, SpeechConfig
from sentry_mode.core.errors import HardwareError


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
        patch("sentry_mode.audio.speech.command", side_effect=synthesize),
        patch("sentry_mode.audio.speech.Speaker") as speaker,
        patch("sentry_mode.audio.speech.prepare_playback", return_value=2.75) as prepare,
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
    with patch("sentry_mode.audio.speech.command") as command:
        with pytest.raises(ValueError):
            speak(Settings(), text, voice, rate)
    command.assert_not_called()


def test_disabled_speech():
    with pytest.raises(HardwareError, match="disabled"):
        speak(Settings(speech=SpeechConfig(enabled=False)), "Hello")


def test_synthesis_preserves_multiline_utterance_without_overwriting(tmp_path):

    def synthesize(args, **kwargs):
        assert kwargs["input_text"] == "First line. Second line."
        path = Path(args[args.index("--output") + 1])
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(bytes(32000))

    with (
        patch(
            "sentry_mode.audio.speech.speech_engine",
            return_value=("kokoro", tmp_path / "kokoro-v1.0.onnx"),
        ),
        patch("sentry_mode.audio.speech.command", side_effect=synthesize) as command,
        patch("sentry_mode.audio.speech.Speaker"),
        patch("sentry_mode.audio.speech.prepare_playback", return_value=2.75),
    ):
        result = speak(Settings(), "First line.\nSecond line.")
    assert result["engine"] == "kokoro"
    assert "--speed" in command.call_args.args[0]


def test_wave_header_cannot_hide_truncated_audio(tmp_path):
    from sentry_mode.audio.speech import wave_duration

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
    from sentry_mode.audio.speech import prepare_playback

    output = tmp_path / "playback.wav"

    def encode(args, **kwargs):
        with wave.open(str(output), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(48000)
            stream.writeframes(bytes(48000 * 4 * 2))

    with patch("sentry_mode.audio.speech.command", side_effect=encode) as encode_command:
        seconds = prepare_playback(Settings(), tmp_path / "speech.wav", output)
    args = encode_command.call_args.args[0]
    assert (
        args[args.index("-af") + 1] == "aresample=48000:osf=s16,adelay=1000:all=1,apad=pad_dur=0.75"
    )
    assert args[args.index("-ar") + 1] == "48000"
    assert args[args.index("-ac") + 1] == "2"
    assert seconds == 2


def test_languages_without_a_neural_voice_offer_female_espeak_variants(tmp_path):
    from sentry_mode.audio.speech import available_voices

    config = Settings(speech=SpeechConfig(kokoro_directory=tmp_path / "none"))
    voices = available_voices(config)["voices"]
    assert [voice["id"] for voice in voices] == ["en", "en+f2", "en+f4", "it", "it+f2", "it+f4"]
    assert {voice["gender"] for voice in voices} == {"female"}


def test_espeak_always_receives_a_female_variant():
    calls = []

    def synthesize(args, **kwargs):
        calls.append(args)
        with wave.open(args[-1], "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(bytes(3200))
        return ""

    config = Settings(speech=SpeechConfig(engine="espeak"))
    with (
        patch("sentry_mode.audio.speech.command", side_effect=synthesize),
        patch("sentry_mode.audio.speech.Speaker"),
        patch("sentry_mode.audio.speech.prepare_playback", return_value=1.0),
    ):
        speak(config, "Hello", voice="en+f2")
        speak(config, "Ciao", voice="it-riccardo")
    assert [args[args.index("-v") + 1] for args in calls] == ["en+f2", "it+f3"]


@pytest.mark.parametrize("voice", ["es", "es+f3", "fr-siwis", "en+m3"])
def test_other_languages_and_male_variants_are_rejected(voice):
    with patch("sentry_mode.audio.speech.command") as command:
        with pytest.raises(ValueError, match="female"):
            speak(Settings(), "Hello", voice)
    command.assert_not_called()


def install_kokoro(directory: Path) -> Path:
    from sentry_mode.audio.speech import KOKORO_MODEL, KOKORO_VOICE_PACK

    directory.mkdir(parents=True, exist_ok=True)
    (directory / KOKORO_MODEL).write_bytes(b"model")
    (directory / KOKORO_VOICE_PACK).write_bytes(b"voices")
    return directory / KOKORO_MODEL


def test_each_language_offers_its_speakers_with_the_configured_one_first(tmp_path):
    from sentry_mode.audio.speech import available_voices, speech_engine

    model = install_kokoro(tmp_path)
    config = Settings(
        speech=SpeechConfig(kokoro_directory=tmp_path, speakers={"en": "bf_emma", "it": "if_sara"})
    )
    with patch("sentry_mode.audio.speech.importlib.util.find_spec", return_value=object()):
        voices = {voice["id"]: voice for voice in available_voices(config)["voices"]}
        assert speech_engine(config, "it") == ("kokoro", model)
        assert speech_engine(config, "en-af_heart") == ("kokoro", model)
    # A language's configured speaker answers to the plain id, so saved rules keep working.
    assert voices["en"]["label"] == "English · Emma" and voices["it"]["label"] == "Italiano · Sara"
    assert "en-bf_emma" not in voices and "it-if_sara" not in voices
    assert voices["en-af_heart"]["name"] == "Heart" and voices["en-af_heart"]["quality"] == "Studio"
    assert [voice for voice in voices if voice.startswith("it")] == ["it"]
    assert {voice["gender"] for voice in voices.values()} == {"female"}
    # An unknown speaker in the configuration falls back to a real one instead of failing.
    odd = Settings(speech=SpeechConfig(kokoro_directory=tmp_path, speakers={"en": "af_missing"}))
    with patch("sentry_mode.audio.speech.importlib.util.find_spec", return_value=object()):
        assert available_voices(odd)["voices"][0]["name"] == "Heart"


def test_kokoro_is_skipped_when_it_is_not_installed(tmp_path):
    from sentry_mode.audio.speech import available_voices, kokoro_installed, speech_engine

    config = Settings(speech=SpeechConfig(kokoro_directory=tmp_path / "kokoro"))
    assert not kokoro_installed(config)
    assert not [v for v in available_voices(config)["voices"] if v["engine"] == "kokoro"]
    # Without the model the voice of a rule is spoken by the basic engine, not refused.
    assert speech_engine(config, "it") == ("espeak", None)
    install_kokoro(tmp_path / "kokoro")
    with patch("sentry_mode.audio.speech.importlib.util.find_spec", return_value=None):
        assert not kokoro_installed(config)
    pinned = Settings(speech=SpeechConfig(engine="kokoro", kokoro_directory=tmp_path / "kokoro"))
    with patch("sentry_mode.audio.speech.importlib.util.find_spec", return_value=object()):
        with pytest.raises(HardwareError, match="not an installed Kokoro voice"):
            speech_engine(pinned, "en+f2")


def test_kokoro_synthesis_runs_in_its_own_process_with_the_editor_rate(tmp_path):
    install_kokoro(tmp_path)

    def synthesize(args, **kwargs):
        assert kwargs["input_text"] == "Ciao. Attendi qui."
        assert kwargs["input_text"] not in args
        path = Path(args[args.index("--output") + 1])
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(bytes(48000))

    config = Settings(speech=SpeechConfig(kokoro_directory=tmp_path))
    with (
        patch("sentry_mode.audio.speech.importlib.util.find_spec", return_value=object()),
        patch("sentry_mode.audio.speech.command", side_effect=synthesize) as command,
        patch("sentry_mode.audio.speech.Speaker"),
        patch("sentry_mode.audio.speech.prepare_playback", return_value=2.0),
    ):
        result = speak(config, "Ciao. Attendi qui.", voice="it", rate=350)
    args = command.call_args.args[0]
    assert result["engine"] == "kokoro" and "Studio" in result["message"]
    assert args[1:3] == ["-m", "sentry_mode.audio.kokoro"]
    assert args[args.index("--voice") + 1] == "if_sara"
    assert args[args.index("--language") + 1] == "it"
    # Words per minute scale Kokoro's own pace, and stay inside what it can speak.
    assert args[args.index("--speed") + 1] == "2.000"
    assert command.call_args.kwargs["timeout"] == 180


def test_kokoro_worker_writes_a_playable_wave_at_the_asked_volume(tmp_path, monkeypatch):
    import sys
    import types

    import numpy as np

    from sentry_mode.audio import kokoro

    spoken = {}

    class FakeKokoro:
        def __init__(self, model, voices):
            spoken["files"] = (model, voices)

        def create(self, text, voice, speed, lang):
            spoken["call"] = (text, voice, speed, lang)
            return np.full(2400, 0.5, dtype="float32"), 24000

    monkeypatch.setitem(sys.modules, "kokoro_onnx", types.SimpleNamespace(Kokoro=FakeKokoro))
    output = tmp_path / "speech.wav"
    seconds = kokoro.synthesize(
        "Ciao.",
        output,
        model=tmp_path / "model.onnx",
        voices=tmp_path / "voices.bin",
        voice="if_sara",
        language="it",
        speed=1.25,
        volume=0.5,
    )
    assert seconds == 0.1 and spoken["call"] == ("Ciao.", "if_sara", 1.25, "it")
    with wave.open(str(output), "rb") as stream:
        assert stream.getframerate() == 24000 and stream.getnchannels() == 1
        peak = max(abs(int.from_bytes(stream.readframes(1)[:2], "little", signed=True)), 0)
    # The speaker volume is applied to the samples before they reach the speaker.
    assert 8000 < peak < 8400


def test_a_speaker_this_node_does_not_have_falls_back_to_its_language(tmp_path):
    from sentry_mode.audio.speech import resolve_voice, speech_engine

    install_kokoro(tmp_path)
    config = Settings(speech=SpeechConfig(kokoro_directory=tmp_path))
    with patch("sentry_mode.audio.speech.importlib.util.find_spec", return_value=object()):
        # A message saved with a speaker that is gone still speaks Italian, not eSpeak.
        assert resolve_voice(config, "it-paola") == "it"
        assert speech_engine(config, "it-paola")[0] == "kokoro"
        assert resolve_voice(config, "en-af_bella") == "en-af_bella"
        # Asking for an eSpeak variant by name still gets that engine.
        assert resolve_voice(config, "it+f2") == "it+f2"
        assert speech_engine(config, "it+f2") == ("espeak", None)


def test_a_saved_message_is_rendered_once_into_an_mp3(tmp_path):
    from sentry_mode.audio.soundboard import SoundboardMessage
    from sentry_mode.audio.speech import render_sample

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        output = Path(args[-1])
        if args[0] == "espeak-ng":
            with wave.open(str(output), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(16000)
                stream.writeframes(bytes(32000))
        else:
            output.write_bytes(b"ID3 sample")
        return ""

    sample = tmp_path / "samples" / "0123456789abcdef.mp3"
    message = SoundboardMessage(text="Intruder alert", voice="en", rate=200)
    with patch("sentry_mode.audio.speech.command", side_effect=run):
        rendered = render_sample(
            Settings(speech=SpeechConfig(engine="espeak")), message, sample, stop_event=None
        )
    speech, encode = calls
    # The sample is stored at full scale so the volume of the moment stays a playback filter.
    assert speech[speech.index("-a") + 1] == "100"
    assert speech[speech.index("-s") + 1] == "200"
    assert encode[encode.index("-c:a") + 1] == "libmp3lame"
    assert rendered["seconds"] == 1 and rendered["engine"] == "espeak"
    assert sample.read_bytes() == b"ID3 sample"
    # Only the finished sample is left behind, never a half-written one.
    assert [item.name for item in sample.parent.iterdir()] == [sample.name]


def test_a_preview_is_levelled_and_pitched_the_way_the_node_would_play_it(tmp_path):
    from sentry_mode.audio.effects import VoiceEffects
    from sentry_mode.audio.speech import PREVIEW_LOUDNESS, sample_audio

    sample = tmp_path / "message.mp3"
    sample.write_bytes(b"ID3 stored")

    def encode(args, **kwargs):
        Path(args[-1]).write_bytes(b"ID3 preview")
        return ""

    def filters(call):
        return call.args[0][call.args[0].index("-af") + 1]

    with patch("sentry_mode.audio.speech.command", side_effect=encode) as command:
        # A phone has no amplifier, so every preview is levelled before it is sent.
        assert sample_audio(sample) == b"ID3 preview"
        assert filters(command.call_args) == PREVIEW_LOUDNESS
        # A card with a modification is pitched first, as it would be on the node.
        sample_audio(sample, VoiceEffects(preset="demon"))
        assert filters(command.call_args).startswith("rubberband=")
        assert filters(command.call_args).endswith("," + PREVIEW_LOUDNESS)


def test_playing_a_sample_never_synthesizes_and_sets_the_level(tmp_path):
    from sentry_mode.audio.effects import VoiceEffects
    from sentry_mode.audio.speech import play_sample

    sample = tmp_path / "sample.mp3"
    sample.write_bytes(b"ID3 sample")

    def encode(args, **kwargs):
        output = Path(args[-1])
        with wave.open(str(output), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(48000)
            stream.writeframes(bytes(48000 * 4))
        return ""

    with (
        patch("sentry_mode.audio.speech.command", side_effect=encode) as command,
        patch("sentry_mode.audio.speech.Speaker") as speaker,
    ):
        result = play_sample(
            Settings(), sample, effects=VoiceEffects(preset="demon", volume=40), stop_event=None
        )
    args = command.call_args.args[0]
    assert command.call_count == 1 and args[0] == "ffmpeg"
    assert args[args.index("-i") + 1] == str(sample)
    filters = args[args.index("-af") + 1].split(",")
    assert filters[:2] == ["aresample=48000:osf=s16", "volume=0.4"]
    assert filters[2].startswith("rubberband") and filters[3] == "adelay=1000:all=1"
    speaker.return_value.play_file.assert_called_once()
    assert result["sample"] and result["playback_seconds"] == 1
    assert result["effects"] == {"preset": "demon", "pitch": -7, "volume": 40}
