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

LANGUAGES = {"en": "English", "it": "Italiano"}
# Piper voice files do not record the speaker's gender, so only these known female
# speakers are offered; any other installed model is ignored.
PIPER_SPEAKERS = {
    "alba": "Alba",
    "amy": "Amy",
    "cori": "Cori",
    "jenny_dioco": "Jenny",
    "kristin": "Kristin",
    "lessac": "Lessac",
    "paola": "Paola",
}
# Female eSpeak variants give the basic engine a choice of voice in every language.
ESPEAK_VARIANTS = {"f3": "Female 1", "f2": "Female 2", "f4": "Female 3"}
ESPEAK_DEFAULT_VARIANT = "f3"
PIPER_MODEL = re.compile(r"([a-z]{2,3})_[A-Z]{2}-([a-z0-9_]+)-(x_low|low|medium|high)")


def piper_models(config: Settings) -> dict[str, Path]:
    """Installed Piper voices by id: configured language defaults, then discovered speakers."""
    directory = config.speech.model_directory
    voices = {}
    for language, name in config.speech.models.items():
        match = PIPER_MODEL.fullmatch(name)
        if language in LANGUAGES and match and match[2] in PIPER_SPEAKERS:
            voices[language] = directory / f"{name}.onnx"
    defaults = set(voices.values())
    try:
        found = sorted(directory.glob("*.onnx"))
    except OSError:
        found = []
    for model in found:
        match = PIPER_MODEL.fullmatch(model.stem)
        if match and match[1] in LANGUAGES and match[2] in PIPER_SPEAKERS and model not in defaults:
            voices.setdefault(f"{match[1]}-{match[2]}", model)
    return voices


def voice_language(voice: str) -> str:
    return re.split(r"[-+]", voice, maxsplit=1)[0]


def speech_engine(config: Settings, voice: str) -> tuple[str, Path | None]:
    model = None if "+" in voice else piper_models(config).get(voice)
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
    """Voices grouped by language: natural Piper speakers, or eSpeak voice types."""
    models = piper_models(config)
    voices = []
    for language in LANGUAGES:
        natural = []
        for voice, model in models.items():
            if voice_language(voice) != language:
                continue
            try:
                engine, _ = speech_engine(config, voice)
            except HardwareError:
                continue
            if engine == "piper":
                match = PIPER_MODEL.fullmatch(model.stem)
                natural.append((voice, PIPER_SPEAKERS[match[2]]))
        if natural:
            choices = [(*choice, "piper") for choice in natural]
        elif config.speech.engine == "piper":
            continue
        else:
            # The plain language id already speaks with the default female variant.
            choices = [
                (
                    language if variant == ESPEAK_DEFAULT_VARIANT else f"{language}+{variant}",
                    name,
                    "espeak",
                )
                for variant, name in ESPEAK_VARIANTS.items()
            ]
        for voice, name, engine in choices:
            voices.append(
                {
                    "id": voice,
                    "language": language,
                    "language_label": LANGUAGES[language],
                    "name": name,
                    "gender": "female",
                    "label": f"{LANGUAGES[language]} · {name}",
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
    language, _, variant = voice.partition("+")
    if voice_language(language) not in LANGUAGES or (variant and variant not in ESPEAK_VARIANTS):
        raise ValueError("Speech voice must be an English or Italian female voice.")
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
                f"{voice_language(voice)}+{variant or ESPEAK_DEFAULT_VARIANT}",
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
