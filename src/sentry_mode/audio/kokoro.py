"""Kokoro synthesis, run as a short-lived subprocess like every other speech engine.

The model is a third of a gigabyte of weights: loading it inside the long-running web
service would keep that resident next to the detector, so it is loaded, used once and
dropped with the process. Text arrives on stdin, never on the command line.
"""

import argparse
import sys
import wave
from pathlib import Path

import numpy as np


def synthesize(
    text: str,
    output: Path,
    *,
    model: Path,
    voices: Path,
    voice: str,
    language: str,
    speed: float = 1.0,
    volume: float = 1.0,
) -> float:
    from kokoro_onnx import Kokoro

    samples, rate = Kokoro(str(model), str(voices)).create(
        text, voice=voice, speed=speed, lang=language
    )
    audio = np.clip(np.asarray(samples, dtype="float32") * volume, -1.0, 1.0)
    with wave.open(str(output), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes((audio * 32767).astype("<i2").tobytes())
    return len(audio) / rate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Speak one utterance with a Kokoro voice.")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--voices", type=Path, required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--volume", type=float, default=1.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    text = sys.stdin.read().strip()
    if not text:
        print("no text to speak", file=sys.stderr)
        return 2
    synthesize(
        text,
        args.output,
        model=args.model,
        voices=args.voices,
        voice=args.voice,
        language=args.language,
        speed=args.speed,
        volume=args.volume,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
