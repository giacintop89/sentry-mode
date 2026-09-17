"""The one encoder command this agent runs, built from checked options and nothing else.

Nothing in a configuration or a command becomes an argument as text. The width, height,
rate, bitrate and keyframe interval are numbers the configuration already checked; the
rest is fixed here. The encoder writes an H.264 elementary stream to its standard output,
with the parameter sets repeated before every keyframe so that a receiver joining late, or
again, can start at the next one.
"""

from collections.abc import Mapping
from typing import Any

ENCODER = "rpicam-vid"


def encoder_argv(options: Mapping[str, Any], *, program: str = ENCODER) -> list[str]:
    fps = int(options["fps"])
    intra = max(1, round(float(options["keyframe_seconds"]) * fps))
    return [
        program,
        "--nopreview",
        "--timeout",
        "0",
        "--codec",
        "h264",
        "--inline",
        "--intra",
        str(intra),
        "--width",
        str(int(options["width"])),
        "--height",
        str(int(options["height"])),
        "--framerate",
        str(fps),
        "--bitrate",
        f"{int(options['bitrate_kbps']) * 1000}",
        "--flush",
        "--output",
        "-",
    ]
