"""One capture from the microphone, shared by every reader, running only while one reads.

ALSA gives a capture device to one program at a time, so the agent never opens it twice:
the activity detector and a stream to the hub both read from this one capture. Each
reader has a short queue of its own. A reader that falls behind loses its oldest blocks
and is told so; it never holds up the capture or another reader.

The capture numbers samples from the moment it first started. When the capture program
dies and is started again, the count jumps by the time it was away, so what was lost is
visible to the hub as a gap rather than hidden.
"""

import logging
import os
import select
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Condition, Event, Lock, Thread

from sentry_satellite.audio.blocks import BLOCK_BYTES, RATE, SAMPLE_BYTES
from sentry_satellite.subprocesses import Child

log = logging.getLogger(__name__)

QUEUE_BLOCKS = 5
"""Half a second per reader. Anything older is not worth sending."""


@dataclass(frozen=True)
class Chunk:
    first_sample: int
    captured_ns: int
    pcm: bytes
    gap: bool = False


class Tap:
    """One reader's queue."""

    def __init__(self, limit: int = QUEUE_BLOCKS) -> None:
        self._chunks: deque[Chunk] = deque()
        self._limit = limit
        self._ready = Condition()
        self._gap = False
        self.dropped = 0
        self.error: str | None = None
        self.closed = False

    def put(self, chunk: Chunk) -> None:
        with self._ready:
            if len(self._chunks) >= self._limit:
                self._chunks.popleft()
                self.dropped += 1
                self._gap = True
            self._chunks.append(chunk)
            self._ready.notify()

    def fail(self, error: str) -> None:
        with self._ready:
            self.error = error
            self._ready.notify_all()

    def get(self, timeout: float) -> Chunk | None:
        with self._ready:
            if not self._chunks and not self.closed:
                self._ready.wait(timeout)
            if not self._chunks:
                return None
            chunk = self._chunks.popleft()
            if self._gap:
                self._gap = False
                chunk = Chunk(chunk.first_sample, chunk.captured_ns, chunk.pcm, gap=True)
            return chunk

    def close(self) -> None:
        with self._ready:
            self.closed = True
            self._ready.notify_all()


class Capture:
    def __init__(
        self,
        name: str,
        argv: Callable[[], Sequence[str]],
        *,
        child: Callable[..., Child] = Child,
        retry_seconds: float = 1.0,
        stall_seconds: float = 3.0,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.name = name
        self._argv = argv
        self._child = child
        self._retry = retry_seconds
        self._stall = stall_seconds
        self._clock_ns = clock_ns
        self._lock = Lock()
        self._taps: list[Tap] = []
        self._thread: Thread | None = None
        self._wake = Event()
        self.samples = 0
        self.starts = 0
        self.error: str | None = None
        self._ended_ns: int | None = None

    # -- readers -----------------------------------------------------------------

    def subscribe(self, limit: int = QUEUE_BLOCKS) -> Tap:
        tap = Tap(limit)
        with self._lock:
            self._taps.append(tap)
            old = self._thread
            if old is not None and old.is_alive() and not self._wake.is_set():
                return tap
        if old is not None:
            old.join(timeout=5)  # the last reader just left and the capture is closing
        with self._lock:
            if self._taps and (self._thread is None or not self._thread.is_alive()):
                self._wake.clear()
                self._thread = Thread(target=self._run, name=f"capture:{self.name}", daemon=True)
                self._thread.start()
        return tap

    def unsubscribe(self, tap: Tap) -> None:
        tap.close()
        with self._lock:
            if tap in self._taps:
                self._taps.remove(tap)
            if not self._taps:
                self._wake.set()

    def stop(self) -> None:
        with self._lock:
            taps, self._taps = self._taps, []
            thread = self._thread
            self._wake.set()
        for tap in taps:
            tap.close()
        if thread is not None:
            thread.join(timeout=5)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            readers = len(self._taps)
            dropped = sum(tap.dropped for tap in self._taps)
        return {
            "readers": readers,
            "starts": self.starts,
            "seconds": round(self.samples / RATE, 1),
            "dropped_blocks": dropped,
            "error": self.error,
        }

    # -- the capture's own thread -----------------------------------------------

    def _wanted(self) -> bool:
        with self._lock:
            return bool(self._taps) and not self._wake.is_set()

    def _run(self) -> None:
        while self._wanted():
            try:
                self._once()
            except (OSError, RuntimeError, ValueError) as error:
                self.error = str(error) or type(error).__name__
                log.warning("%s: %s", self.name, self.error)
                with self._lock:
                    taps = list(self._taps)
                for tap in taps:
                    tap.fail(self.error)
            if self._wake.wait(self._retry):
                break

    def _once(self) -> None:
        child = self._child(f"microphone:{self.name}", tuple(self._argv()), capture=True)
        child.start()
        self.starts += 1
        if self._ended_ns is not None:
            # Whatever the microphone heard while the program was away is lost; say how much.
            away = max(0, self._clock_ns() - self._ended_ns)
            self.samples += away * RATE // 1_000_000_000
        try:
            pipe = child.stdout
            if pipe is None:
                raise RuntimeError("the capture has no output")
            descriptor = pipe.fileno()
            pending = b""
            quiet_since = self._clock_ns()
            while self._wanted():
                ready, _, _ = select.select([descriptor], [], [], 0.25)
                if not ready:
                    if self._clock_ns() - quiet_since > self._stall * 1e9:
                        raise RuntimeError(f"the microphone said nothing for {self._stall:g} s")
                    continue
                data = os.read(descriptor, BLOCK_BYTES - len(pending))
                if not data:
                    code = child.stop()
                    last = child.complaints[-1] if child.complaints else "no message"
                    raise RuntimeError(f"the capture exited with status {code}: {last}")
                now = self._clock_ns()
                quiet_since = now
                pending += data
                if len(pending) < BLOCK_BYTES:
                    continue
                chunk = Chunk(self.samples, now, pending)
                self.samples += len(pending) // SAMPLE_BYTES
                pending = b""
                self.error = None
                with self._lock:
                    taps = list(self._taps)
                for tap in taps:
                    tap.put(chunk)
        finally:
            child.stop()
            self._ended_ns = self._clock_ns()
