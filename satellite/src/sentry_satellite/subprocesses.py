"""Child processes that are watched without being waited for.

The camera and the audio profiles hand their work to a program the system already has —
`rpicam-vid`, `ffmpeg`, `arecord` — and none of that may end up on the thread that answers
health checks or handles a stop. A child is started, polled, and asked to leave politely
before it is made to.
"""

import logging
import os
import signal
import subprocess
from collections import deque
from dataclasses import dataclass, field
from threading import Lock, Thread
from typing import IO

log = logging.getLogger(__name__)


@dataclass
class Child:
    """One supervised program.

    `stop` is the whole point: SIGTERM, a bounded wait, then SIGKILL, and it returns
    whatever happened instead of hanging. A satellite that cannot be stopped has to be
    power-cycled, and nobody is standing next to it.
    """

    name: str
    command: tuple[str, ...]
    grace_seconds: float = 3.0
    capture: bool = False
    """Hand the program's output to the caller instead of throwing it away."""
    _process: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    restarts: int = field(default=0, init=False)
    complaints: deque[str] = field(default_factory=lambda: deque(maxlen=20), init=False)
    """The last lines the program wrote to its error stream, read as they come so that a
    chatty program can never fill the pipe and stall."""

    @property
    def stdout(self) -> IO[bytes] | None:
        with self._lock:
            return None if self._process is None else self._process.stdout

    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            if self._process is not None:
                self.restarts += 1
            log.info("starting %s", self.name)
            self._process = subprocess.Popen(  # noqa: S603 - the command is from configuration
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if self.capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            Thread(
                target=self._drain,
                args=(self._process.stderr,),
                name=f"stderr:{self.name}",
                daemon=True,
            ).start()

    def _drain(self, stream: IO[bytes]) -> None:
        with stream:
            for line in stream:
                self.complaints.append(line.decode("utf-8", "replace").rstrip())

    def poll(self) -> int | None:
        """The exit status, or None while it is still running."""
        with self._lock:
            return None if self._process is None else self._process.poll()

    def stop(self) -> int | None:
        with self._lock:
            process = self._process
            self._process = None
        if process is None:
            return None
        try:
            return self._end(process)
        finally:
            if process.stdout is not None:
                process.stdout.close()

    def _end(self, process: subprocess.Popen) -> int | None:
        if process.poll() is not None:
            return process.returncode
        log.info("stopping %s", self.name)
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
        try:
            return process.wait(timeout=self.grace_seconds)
        except subprocess.TimeoutExpired:
            log.warning("%s did not stop when asked; killing it", self.name)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            try:
                return process.wait(timeout=self.grace_seconds)
            except subprocess.TimeoutExpired:
                log.error("%s is unkillable; leaving it to init", self.name)
                return None
