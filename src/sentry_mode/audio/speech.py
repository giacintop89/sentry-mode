"""Local speech synthesis with complete, buffered playback on the node speaker."""

import importlib.util
import re
import sys
import tempfile
import threading
import wave
from pathlib import Path

from sentry_mode.audio.effects import VoiceEffects
from sentry_mode.audio.playback import command
from sentry_mode.config import Settings
from sentry_mode.core.errors import HardwareError
from sentry_mode.hardware.speaker import Speaker

LANGUAGES = {"en": "English", "it": "Italiano"}
# Female eSpeak variants give the basic engine a choice of voice in every language.
ESPEAK_VARIANTS = {"f3": "Female 1", "f2": "Female 2", "f4": "Female 3"}
ESPEAK_DEFAULT_VARIANT = "f3"
# One Kokoro model holds every speaker, so its voices are a list, not installed files.
# A speaker's name carries language and gender: a American, b British, i Italian, f female.
KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_VOICE_PACK = "voices-v1.0.bin"
KOKORO_LANGUAGES = {"a": ("en", "en-us"), "b": ("en", "en-gb"), "i": ("it", "it")}
KOKORO_SPEAKERS = {
    "af_heart": "Heart",
    "af_bella": "Bella",
    "af_nicole": "Nicole",
    "af_aoede": "Aoede",
    "af_kore": "Kore",
    "af_sarah": "Sarah",
    "af_nova": "Nova",
    "bf_emma": "Emma",
    "bf_isabella": "Isabella",
    "if_sara": "Sara",
}
QUALITY = {"kokoro": "Studio", "espeak": "Basic"}


def kokoro_files(config: Settings) -> tuple[Path, Path]:
    directory = config.speech.kokoro_directory
    return directory / KOKORO_MODEL, directory / KOKORO_VOICE_PACK


def kokoro_installed(config: Settings) -> bool:
    model, pack = kokoro_files(config)
    return (
        model.is_file() and pack.is_file() and importlib.util.find_spec("kokoro_onnx") is not None
    )


def kokoro_voices(config: Settings) -> dict[str, tuple[str, str, str]]:
    """Offered voices by id: speaker name, Kokoro's own name, Kokoro's language.

    Each language's configured speaker answers to the plain language id, so `it` keeps
    meaning "the Italian voice" for a saved rule while the others are named after it.
    """
    if not kokoro_installed(config) or config.speech.engine == "espeak":
        return {}
    voices = {}
    for language in LANGUAGES:
        speakers = [s for s in KOKORO_SPEAKERS if KOKORO_LANGUAGES[s[0]][0] == language]
        if not speakers:
            continue
        default = config.speech.speakers.get(language, speakers[0])
        if default not in speakers:
            default = speakers[0]
        for speaker in [default] + [s for s in speakers if s != default]:
            name, spoken = KOKORO_SPEAKERS[speaker], KOKORO_LANGUAGES[speaker[0]][1]
            voices[language if speaker == default else f"{language}-{speaker}"] = (
                name,
                speaker,
                spoken,
            )
    return voices


def voice_language(voice: str) -> str:
    return re.split(r"[-+]", voice, maxsplit=1)[0]


def resolve_voice(config: Settings, voice: str) -> str:
    """The voice actually spoken.

    A speaker this node does not have — one retired with an engine, or a message saved
    elsewhere — is spoken by its language's voice rather than dropping to the basic engine.
    An explicit eSpeak variant is left alone: it asks for that engine by name.
    """
    voices = kokoro_voices(config)
    if voice in voices or "+" in voice:
        return voice
    language = voice_language(voice)
    return language if language in voices else voice


def speech_engine(config: Settings, voice: str) -> tuple[str, Path | None]:
    # A voice id names its own engine, so a rule that speaks with Heart keeps speaking
    # with Heart; only a missing model or an engine pinned in the configuration changes it.
    if resolve_voice(config, voice) in kokoro_voices(config):
        return "kokoro", kokoro_files(config)[0]
    if config.speech.engine == "kokoro":
        raise HardwareError(
            f"{voice!r} is not an installed Kokoro voice. Run scripts/setup_speech.sh, "
            "or set speech.engine to auto to fall back to the basic engine."
        )
    return "espeak", None


def available_voices(config: Settings) -> dict:
    """Voices grouped by language: Kokoro speakers, or eSpeak voice types."""
    kokoro = kokoro_voices(config)
    voices = []
    for language in LANGUAGES:
        studio = [
            (voice, name)
            for voice, (name, _, _) in kokoro.items()
            if voice_language(voice) == language
        ]
        if studio:
            choices = [(*choice, "kokoro") for choice in studio]
        elif config.speech.engine == "kokoro":
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
                    "quality": QUALITY[engine],
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
    voice = resolve_voice(config, voice)
    speaker = Speaker(config.speaker)
    device = speaker.device()
    with tempfile.TemporaryDirectory(prefix="sentry-mode-speech-") as directory:
        output = Path(directory) / "speech.wav"
        # Flatten lines into a single utterance so stdin readers cannot overwrite
        # earlier lines. Text goes through stdin, never shell/command options.
        utterance = " ".join(text.splitlines()).strip()
        if engine == "kokoro":
            _, kokoro_speaker, spoken = kokoro_voices(config)[voice]
            args = [
                sys.executable,
                "-m",
                "sentry_mode.audio.kokoro",
                "--model",
                str(model),
                "--voices",
                str(kokoro_files(config)[1]),
                "--voice",
                kokoro_speaker,
                "--language",
                spoken,
                # Kokoro speaks at its own pace; the editor's words per minute scale it.
                "--speed",
                f"{min(max(rate / 175, 0.5), 2):.3f}",
                "--volume",
                f"{config.speaker.volume / 100:g}",
                "--output",
                str(output),
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
            timeout=15 if engine == "espeak" else 180,
            input_text=utterance,
            stop_event=stop_event,
        )
        seconds = wave_duration(output)
        playback = Path(directory) / "playback.wav"
        playback_seconds = prepare_playback(config, output, playback, stop_event, effects)
        speaker.play_file(
            playback, timeout=playback_seconds + 10, device=device, stop_event=stop_event
        )
    quality = QUALITY[engine]
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
