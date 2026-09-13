"""Local speech synthesis with complete, buffered playback on the node speaker."""

import importlib.util
import re
import sys
import tempfile
import threading
import wave
from pathlib import Path

from sentry_node.audio.effects import VoiceEffects
from sentry_node.audio.playback import command
from sentry_node.config import Settings
from sentry_node.core.errors import HardwareError
from sentry_node.hardware.speaker import Speaker


def speech_engine(config: Settings, voice: str) -> tuple[str, Path | None]:
    name = config.speech.models.get(voice)
    model = config.speech.model_directory / f"{name}.onnx" if name else None
    available = (
        model is not None
        and model.is_file()
        and Path(str(model) + ".json").is_file()
        and importlib.util.find_spec("piper") is not None
    )
    if config.speech.engine == "piper" and not available:
        raise HardwareError(f"Piper voice {voice!r} is not installed. Run scripts/setup_speech.sh.")
    if config.speech.engine != "espeak" and available:
        return "piper", model
    return "espeak", None


def available_voices(config: Settings) -> dict:
    labels = {
        "en": "English",
        "it": "Italiano",
        "es": "Español",
        "fr": "Français",
        "de": "Deutsch",
        "pt": "Português",
    }
    voices = []
    for voice in dict.fromkeys([config.speech.voice, *labels, *config.speech.models]):
        try:
            engine, _ = speech_engine(config, voice)
        except HardwareError:
            continue
        voices.append(
            {
                "id": voice,
                "label": labels.get(voice, voice),
                "engine": engine,
                "quality": "Natural" if engine == "piper" else "Basic",
            }
        )
    return {"voices": voices, "default": config.speech.voice}


def wave_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        frames = audio.getnframes()
        if not frames or len(audio.readframes(frames)) != (
            frames * audio.getnchannels() * audio.getsampwidth()
        ):
            raise HardwareError("speech audio is empty or truncated")
        return frames / audio.getframerate()


def prepare_playback(
    config: Settings,
    source: Path,
    output: Path,
    stop_event: threading.Event | None = None,
    effects: VoiceEffects | None = None,
) -> float:
    # One continuous stream opens the Bluetooth transport before speech and keeps
    # it open past the last word. Resampling is done before real-time playback.
    lead_filter = f"adelay={config.speech.lead_in_ms}:all=1"
    tail_filter = f"apad=pad_dur={config.speech.tail_ms / 1000:g}"
    filters = f"aresample=48000:osf=s16,{lead_filter},{tail_filter}"
    if effects and effects.pitch_filter():
        filters = f"aresample=48000:osf=s16,{effects.pitch_filter()},{lead_filter},{tail_filter}"
    command(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            filters,
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        timeout=15,
        stop_event=stop_event,
    )
    return wave_duration(output)


def speak(
    config: Settings,
    text: str,
    voice: str | None = None,
    rate: int | None = None,
    *,
    stop_event: threading.Event | None = None,
    effects: VoiceEffects | None = None,
) -> dict:
    effects = effects or VoiceEffects()
    if effects.volume is not None:
        config = config.model_copy(
            update={"speaker": config.speaker.model_copy(update={"volume": effects.volume})}
        )
    if not config.speech.enabled:
        raise HardwareError("text-to-speech is disabled")
    if not text.strip() or len(text) > 1000:
        raise ValueError("Speech text must contain 1 to 1000 characters.")
    voice = voice if voice is not None else config.speech.voice
    rate = rate if rate is not None else config.speech.rate
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_+\-]{0,63}", voice):
        raise ValueError("Invalid speech voice.")
    if not 80 <= rate <= 450:
        raise ValueError("Speech rate must be between 80 and 450 words per minute.")
    engine, model = speech_engine(config, voice)
    speaker = Speaker(config.speaker)
    device = speaker.device()
    with tempfile.TemporaryDirectory(prefix="sentry-node-speech-") as directory:
        output = Path(directory) / "speech.wav"
        # Flatten lines into a single utterance so stdin readers cannot overwrite
        # earlier lines. Text goes through stdin, never shell/command options.
        utterance = " ".join(text.splitlines()).strip()
        if engine == "piper":
            args = [
                sys.executable,
                "-m",
                "piper",
                "--model",
                str(model),
                "--output-file",
                str(output),
                "--length-scale",
                f"{175 / rate:g}",
                "--volume",
                f"{config.speaker.volume / 100:g}",
            ]
        else:
            args = [
                "espeak-ng",
                "--stdin",
                "-v",
                voice,
                "-s",
                str(rate),
                "-a",
                str(config.speaker.volume),
                "-w",
                str(output),
            ]
        command(
            args,
            timeout=120 if engine == "piper" else 15,
            input_text=utterance,
            stop_event=stop_event,
        )
        seconds = wave_duration(output)
        playback = Path(directory) / "playback.wav"
        playback_seconds = prepare_playback(config, output, playback, stop_event, effects)
        speaker.play_file(
            playback, timeout=playback_seconds + 10, device=device, stop_event=stop_event
        )
    quality = "Natural" if engine == "piper" else "Basic"
    return {
        "message": f"Speech played through the node speaker ({quality} voice).",
        "seconds": seconds,
        "playback_seconds": playback_seconds,
        "voice": voice,
        "rate": rate,
        "engine": engine,
        "effects": {
            "preset": effects.preset,
            "pitch": effects.semitones,
            "volume": config.speaker.volume,
        },
    }
