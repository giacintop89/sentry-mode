"""A microphone on a satellite: heard while somebody listens or records, and silent otherwise.

Sound comes in through the media gateway as numbered blocks of PCM (see `blocks`). The
microphone puts them back in order, fills short gaps with silence, and hands every block
to each subscriber: a browser listening, a recording being written. Each subscriber has
its own bounded queue; one that falls behind loses its oldest audio, never anyone else's,
and never makes the hub hold more than a couple of seconds for it.

A recording reads the same blocks through a named pipe, so ffmpeg records a satellite
microphone exactly as it records the hub's own, from an input it names on its command line.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from sentry_mode.audio.blocks import RATE, SAMPLE_BYTES, BlockError, Reassembler
from sentry_mode.satellites.config import MediaConfig
from sentry_mode.sources.manager import Lease
from sentry_mode.sources.remote import LeasedStream

log = logging.getLogger(__name__)

PURPOSES = ("listening", "recording", "monitoring")
MAX_LISTENERS = 2
"""Browsers that may listen to one satellite microphone at once."""
QUEUE_SECONDS = 2.0
QUEUE_BYTES = int(RATE * SAMPLE_BYTES * QUEUE_SECONDS)
FFMPEG_INPUT = ("-f", "s16le", "-ar", str(RATE), "-ac", "1")


class AudioLink(Protocol):
    """What a remote microphone needs from the satellite service."""

    def start_audio(
        self,
        node_id: str,
        source_id: str,
        connect: Callable[[], Any],
        on_end: Callable[[str, bool], None],
    ) -> str: ...

    def renew_audio(self, node_id: str, stream_id: str) -> bool: ...

    def stop_audio(self, node_id: str, stream_id: str) -> None: ...


class Subscription:
    """One reader's share of the sound, with its own bounded queue."""

    def __init__(self, lease: Lease, limit: int = QUEUE_BYTES) -> None:
        self.lease = lease
        self.limit = limit
        self.dropped_bytes = 0
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._ready = threading.Condition()
        self._closed = False

    def put(self, pcm: bytes) -> None:
        with self._ready:
            if self._closed:
                return
            self._chunks.append(pcm)
            self._size += len(pcm)
            while self._size > self.limit and len(self._chunks) > 1:
                old = self._chunks.popleft()
                self._size -= len(old)
                self.dropped_bytes += len(old)
            self._ready.notify()

    def read(self, timeout: float = 1.0) -> bytes | None:
        """What has arrived since the last read; b"" if nothing did; None once closed."""
        with self._ready:
            if not self._chunks and not self._closed:
                self._ready.wait(timeout)
            if not self._chunks:
                return None if self._closed else b""
            data = b"".join(self._chunks)
            self._chunks.clear()
            self._size = 0
            return data

    def close(self) -> None:
        with self._ready:
            self._closed = True
            self._ready.notify_all()
        self.lease.release()

    @property
    def closed(self) -> bool:
        return self._closed


class _Sink:
    """What the gateway feeds for one connection."""

    def __init__(self, microphone: RemoteMicrophone) -> None:
        self.microphone = microphone
        self.reassembler = Reassembler()

    def feed(self, data: bytes) -> None:
        try:
            chunks = self.reassembler.feed(data)
        except BlockError:
            self.microphone.corrupt += 1
            raise
        if chunks:
            self.microphone._pcm(chunks)

    def close(self) -> None:
        self.microphone._retire(self.reassembler)


class RemoteMicrophone(LeasedStream):
    kind = "audio"
    purposes = PURPOSES

    def __init__(
        self,
        node_id: str,
        name: str,
        options: dict | None = None,
        *,
        link: AudioLink,
        media: MediaConfig,
        display_name: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(node_id, name, media=media, display_name=display_name, clock=clock)
        self.link = link
        self.options = dict(options or {})
        self.subscriptions: list[Subscription] = []
        self.admission = threading.Lock()
        self.bytes_in = 0
        self.corrupt = 0
        self.level_dbfs: float | None = None
        self.totals = Reassembler().status()

    # -- the link --------------------------------------------------------------------

    def _ask(self, connect: Callable[[], Any], on_end: Callable[[str, bool], None]) -> str:
        return self.link.start_audio(self.node_id, self.name, connect, on_end)

    def _renew(self, stream_id: str) -> bool:
        return self.link.renew_audio(self.node_id, stream_id)

    def _cancel(self, stream_id: str) -> None:
        self.link.stop_audio(self.node_id, stream_id)

    def _open_sink(self) -> _Sink:
        return _Sink(self)

    def _retire(self, reassembler: Reassembler) -> None:
        """Keep the counts of a connection that ended, so the status adds up across them."""
        with self.lock:
            for key, value in reassembler.status().items():
                self.totals[key] += value

    def _pcm(self, chunks: list[bytes]) -> None:
        now = self.clock()
        with self.lock:
            self.last_data_at = now
            self.bytes_in += sum(map(len, chunks))
            self.state, self.error = "live", None
            readers = list(self.subscriptions)
        self.level_dbfs = _dbfs(chunks[-1])
        for reader in readers:
            for chunk in chunks:
                reader.put(chunk)

    # -- readers ---------------------------------------------------------------------

    def hold(self, purpose: str, owner: str, **params: Any) -> Lease:
        return self.demands.hold(purpose, owner, **params)

    def subscribe(self, purpose: str, owner: str) -> Subscription:
        with self.admission:
            with self.lock:
                listening = sum(s.lease.purpose == "listening" for s in self.subscriptions)
            if purpose == "listening" and listening >= MAX_LISTENERS:
                raise BlockingIOError(
                    f"Only {MAX_LISTENERS} listeners can hear {self.source_id} at once."
                )
            subscription = Subscription(self.demands.hold(purpose, owner))
            with self.lock:
                self.subscriptions.append(subscription)
        return subscription

    @property
    def listeners(self) -> int:
        with self.lock:
            return sum(s.lease.purpose == "listening" for s in self.subscriptions)

    def unsubscribe(self, subscription: Subscription) -> None:
        with self.lock:
            if subscription in self.subscriptions:
                self.subscriptions.remove(subscription)
        subscription.close()

    @contextmanager
    def listen(self, owner: str = "browser") -> Iterator[Iterator[bytes]]:
        """Raw 16 kHz mono PCM for as long as the caller reads it."""
        subscription = self.subscribe("listening", owner)

        def chunks() -> Iterator[bytes]:
            while True:
                data = subscription.read()
                if data is None:
                    return
                if data:
                    yield data

        try:
            yield chunks()
        finally:
            self.unsubscribe(subscription)

    @contextmanager
    def recording(self, owner: str) -> Iterator[PipeInput]:
        """An ffmpeg input that reads this microphone until it is ended or the block exits."""
        subscription = self.subscribe("recording", owner)
        sound = PipeInput(subscription, lambda: self.unsubscribe(subscription))
        try:
            yield sound
        finally:
            sound.end()

    # -- what the rest of the hub reads ---------------------------------------------------

    def status(self) -> dict:
        summary = self.stream_status()
        sink = self.sink
        with self.lock:
            totals = dict(self.totals)
            if sink is not None:
                for key, value in sink.reassembler.status().items():
                    totals[key] += value
            summary.update(
                kind="microphone",
                display_name=self.display_name,
                sample_rate=RATE,
                bytes_in=self.bytes_in,
                corrupt_connections=self.corrupt,
                level_dbfs=self.level_dbfs,
                listeners=sum(s.lease.purpose == "listening" for s in self.subscriptions),
                dropped_bytes=sum(s.dropped_bytes for s in self.subscriptions),
                blocks=totals,
            )
        summary["leases"] = self.demands.summary()
        return summary

    def close(self) -> None:
        with self.lock:
            readers, self.subscriptions = self.subscriptions, []
        for reader in readers:
            reader.close()
        super().close()


class PipeInput:
    """A microphone's sound as a named pipe ffmpeg can read.

    ffmpeg blocked reading a pipe does not notice SIGINT, so a recording is ended from this
    side: `end` closes the pipe, ffmpeg reads to the end of it and writes a complete file.
    """

    def __init__(self, subscription: Subscription, release: Callable[[], None]) -> None:
        self.subscription = subscription
        self.release = release
        self.folder = Path(tempfile.mkdtemp(prefix="sentry-remote-audio-"))
        self.pipe = self.folder / "sound.pcm"
        os.mkfifo(self.pipe, 0o600)
        self.argv = [*FFMPEG_INPUT, "-i", str(self.pipe)]
        self.written = 0
        self._done = threading.Event()
        self._ended = False
        self._feeder = threading.Thread(target=self._feed, name="record-remote-audio")
        self._feeder.start()

    def _feed(self) -> None:
        try:
            with open(self.pipe, "wb", buffering=0) as out:
                while not self._done.is_set():
                    data = self.subscription.read(0.25)
                    if data is None:
                        return
                    if data:
                        out.write(data)
                        self.written += len(data)
        except OSError as exc:
            if not self._done.is_set():
                log.info("a recording stopped reading its microphone: %s", exc)

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._done.set()
        self.release()
        # A reader that never came leaves the feeder waiting in open(); be that reader.
        try:
            os.close(os.open(self.pipe, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass
        self._feeder.join(timeout=5)
        self.pipe.unlink(missing_ok=True)
        try:
            self.folder.rmdir()
        except OSError:
            pass


def _dbfs(pcm: bytes) -> float | None:
    import numpy as np

    samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float64)
    if not samples.size:
        return None
    rms = float(np.sqrt(np.mean(samples * samples)))
    return round(20 * np.log10(max(rms, 1.0) / 32768.0), 1)


class MicrophoneTable:
    """The satellite microphones this hub can hear, by source id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._microphones: dict[str, RemoteMicrophone] = {}

    def add(self, microphone: RemoteMicrophone) -> RemoteMicrophone:
        with self._lock:
            if microphone.source_id in self._microphones:
                raise ValueError(f"microphone {microphone.source_id} is already known")
            self._microphones[microphone.source_id] = microphone
        return microphone

    def get(self, source_id: str) -> RemoteMicrophone | None:
        with self._lock:
            return self._microphones.get(source_id)

    def __contains__(self, source_id: object) -> bool:
        with self._lock:
            return source_id in self._microphones

    def __iter__(self) -> Iterator[RemoteMicrophone]:
        with self._lock:
            return iter(list(self._microphones.values()))

    def status(self) -> dict[str, dict]:
        return {microphone.source_id: microphone.status() for microphone in self}

    def close(self) -> None:
        for microphone in self:
            microphone.close()


__all__ = ["AudioLink", "MicrophoneTable", "PipeInput", "RemoteMicrophone", "Subscription"]
