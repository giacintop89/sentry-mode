"""Turning an H.264 stream from a satellite into frames, in a program that can be killed.

The decoder is ffmpeg, in its own process group, reading the stream on its standard input
and writing raw BGR frames of a fixed size on its standard output. A damaged stream costs
that process, never this one. What goes in is bounded: when ffmpeg falls behind by more
than `backlog_bytes`, feeding it fails, and the caller ends the stream rather than letting
video pile up in memory. Every way out of here ends with the process reaped.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from typing import IO, Any

log = logging.getLogger(__name__)

WRITE_CHUNK = 64 * 1024


class Backlog(RuntimeError):
    """The decoder is not keeping up; the stream feeding it has to end."""


class DecoderGone(RuntimeError):
    """The decoder has exited, or was never started."""


class NetworkDecoder:
    def __init__(
        self,
        width: int,
        height: int,
        on_frame: Callable[[Any], None],
        *,
        ffmpeg: str = "ffmpeg",
        backlog_bytes: int = 4 * 1024 * 1024,
        grace_seconds: float = 2.0,
        name: str = "decoder",
    ) -> None:
        self.width = width
        self.height = height
        self.on_frame = on_frame
        self.ffmpeg = ffmpeg
        self.backlog_bytes = backlog_bytes
        self.grace_seconds = grace_seconds
        self.name = name
        self.frames = 0
        self.bytes_in = 0
        self.error: str | None = None
        self.complaints: deque[str] = deque(maxlen=20)
        self._queued = 0
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._process: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._stderr_done = threading.Event()

    def argv(self) -> list[str]:
        return [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            # Start from the first parameter sets instead of waiting for megabytes of
            # stream to analyse: a live camera would otherwise show nothing for seconds.
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            # Frame threads each hold a frame back; one thread decodes 640x480 easily.
            "-threads",
            "1",
            "-f",
            "h264",
            "-i",
            "pipe:0",
            "-an",
            "-vf",
            f"scale={self.width}:{self.height}",
            "-pix_fmt",
            "bgr24",
            "-f",
            "rawvideo",
            "pipe:1",
        ]

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed arguments, no shell
                self.argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            self.error = f"the decoder could not start: {exc}"
            raise DecoderGone(self.error) from exc
        self._process = process
        assert process.stdin and process.stdout and process.stderr
        for target, argument, role in (
            (self._write, process.stdin, "in"),
            (self._read, process.stdout, "out"),
            (self._complain, process.stderr, "err"),
        ):
            thread = threading.Thread(
                target=target, args=(argument,), name=f"{self.name}-{role}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    @property
    def alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None and not self._closed.is_set()

    def feed(self, data: bytes) -> None:
        """Queue bytes for the decoder, or refuse them when it has fallen too far behind."""
        if not self.alive:
            raise DecoderGone(self.error or "the decoder has stopped")
        with self._lock:
            if self._queued + len(data) > self.backlog_bytes:
                raise Backlog(f"the decoder is more than {self.backlog_bytes} bytes behind")
            self._queued += len(data)
        self.bytes_in += len(data)
        self._queue.put(data)

    @property
    def queued_bytes(self) -> int:
        with self._lock:
            return self._queued

    def close(self) -> int | None:
        """Stop the decoder and reap it. Safe to call more than once, from any thread."""
        self._closed.set()
        self._queue.put(None)
        process = self._process
        if process is None:
            return None
        code = self._end(process)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=self.grace_seconds)
        return code

    def _end(self, process: subprocess.Popen) -> int | None:
        try:
            return process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        for sent in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sent)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                return process.wait(timeout=self.grace_seconds)
            except subprocess.TimeoutExpired:
                continue
        log.error("%s could not be reaped", self.name)
        return None

    # -- the decoder's own threads -------------------------------------------------

    def _write(self, pipe: IO[bytes]) -> None:
        try:
            while True:
                data = self._queue.get()
                if data is None or self._closed.is_set():
                    break
                with self._lock:
                    self._queued -= len(data)
                for start in range(0, len(data), WRITE_CHUNK):
                    pipe.write(data[start : start + WRITE_CHUNK])
                pipe.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            if not self._closed.is_set():
                self.error = f"the decoder stopped taking input: {exc}"
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _read(self, pipe: IO[bytes]) -> None:
        import numpy as np

        size = self.width * self.height * 3
        try:
            while not self._closed.is_set():
                frame = _exactly(pipe, size)
                if frame is None:
                    break
                self.frames += 1
                image = np.frombuffer(frame, np.uint8).reshape(self.height, self.width, 3)
                try:
                    self.on_frame(image)
                except Exception:  # noqa: BLE001 - a consumer bug must not wedge the pipe
                    log.exception("%s: a frame could not be delivered", self.name)
        finally:
            pipe.close()
            if not self._closed.is_set():
                # Standard output can reach its end before the last complaint is read.
                self._stderr_done.wait(self.grace_seconds)
                last = self.complaints[-1] if self.complaints else "no message"
                self.error = self.error or f"the decoder exited: {last}"
                self._closed.set()
                self._queue.put(None)

    def _complain(self, pipe: IO[bytes]) -> None:
        try:
            with pipe:
                for line in pipe:
                    self.complaints.append(line.decode("utf-8", "replace").rstrip())
        finally:
            self._stderr_done.set()


def _exactly(pipe: IO[bytes], size: int) -> bytes | None:
    parts = []
    missing = size
    while missing:
        chunk = pipe.read(missing)
        if not chunk:
            return None
        parts.append(chunk)
        missing -= len(chunk)
    return b"".join(parts)


def keyframe_offset(data: bytes) -> int | None:
    """Where the first sequence parameter set starts, counting its start code.

    A decoder that starts in the middle of a group of pictures produces garbage until the
    next keyframe, so the bytes before the first SPS are discarded. With `--inline` the
    encoder repeats the parameter sets before every keyframe.
    """
    at = 0
    while True:
        found = data.find(b"\x00\x00\x01", at)
        if found < 0 or found + 3 >= len(data):
            return None
        if data[found + 3] & 0x1F == 7:
            return found - 1 if found > 0 and data[found - 1] == 0 else found
        at = found + 3


__all__ = ["Backlog", "DecoderGone", "NetworkDecoder", "keyframe_offset"]
