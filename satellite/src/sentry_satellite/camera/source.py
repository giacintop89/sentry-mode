"""The camera as a source: it reports nothing on its own, and streams only when asked.

A camera produces no events here. It sits in the list of sources so that the hub learns it
exists, what it can give and whether its encoder is present, and it waits. Video starts
when the hub sends `video_start` and ends when the hub stops renewing it.
"""

import shutil
from collections.abc import Iterator, Mapping
from threading import Event
from typing import Any

from sentry_satellite.camera.profile import ENCODER, encoder_argv
from sentry_satellite.sensors import Reading


class CsiCamera:
    def __init__(
        self, source_id: str, options: Mapping[str, Any], *, program: str | None = None
    ) -> None:
        self.source_id = source_id
        self.options = dict(options)
        self.program = program or shutil.which(ENCODER)
        self.error: str | None = (
            None if self.program else f"{ENCODER} is not installed (apt install rpicam-apps-core)"
        )
        self._stop = Event()

    def argv(self) -> list[str]:
        if self.program is None:
            raise RuntimeError(self.error)
        return encoder_argv(self.options, program=self.program)

    def read(self) -> Iterator[Reading]:
        self._stop.wait()
        yield from ()

    def stop(self) -> None:
        self._stop.set()
