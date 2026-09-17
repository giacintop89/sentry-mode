"""Carrying the microphone to the hub, in blocks, for as long as the hub has asked.

The connection, the handshake and the retries are the camera's (see
`camera.publisher`); only what is sent differs. Blocks come from the shared capture, each
numbered from zero on every connection. A connection that cannot keep up is dropped and
started again rather than queued behind: the capture keeps half a second for it at most.
"""

import socket
from collections.abc import Callable

from sentry_satellite.audio.blocks import pack
from sentry_satellite.audio.capture import Capture, Tap
from sentry_satellite.camera.publisher import Connection, Publisher, Stream


class AudioPublisher(Publisher):
    kind = "audio"

    def __init__(
        self,
        stream: Stream,
        capture: Capture,
        connect: Callable[[int], Connection],
        *,
        stall_seconds: float = 3.0,
        **options,
    ) -> None:
        super().__init__(stream, (), connect, stall_seconds=stall_seconds, **options)
        self._capture = capture
        self.blocks_sent = 0
        self.gaps = 0

    def status(self) -> dict:
        return {**super().status(), "blocks_sent": self.blocks_sent, "gaps": self.gaps}

    def _once(self) -> None:
        self.state = "connecting"
        connection = self._connect(self.stream.port)
        with self._lock:
            self._connection = connection
        if self._stop.is_set():
            connection.close()
            return
        self.connections += 1
        tap: Tap | None = None
        try:
            self._handshake(connection)
            tap = self._capture.subscribe()
            self.encoders += 1
            self.state = "live"
            self.error = None
            self._pump(tap, connection)
        finally:
            if tap is not None:
                self._capture.unsubscribe(tap)
            with self._lock:
                self._connection = None
            try:
                connection.close()
            except OSError:
                pass

    def _pump(self, tap: Tap, connection: Connection) -> None:
        sequence = 0
        quiet_since = self._clock()
        while not self._stop.is_set() and not self._expired():
            chunk = tap.get(0.5)
            if chunk is None:
                if self._clock() - quiet_since > self._stall:
                    reason = self._capture.error or f"no sound for {self._stall:g} s"
                    raise RuntimeError(f"the microphone stopped: {reason}")
                continue
            quiet_since = self._clock()
            block = pack(sequence, chunk.first_sample, chunk.captured_ns, chunk.pcm, gap=chunk.gap)
            try:
                connection.sendall(block)
            except socket.timeout as error:
                raise ConnectionError("the hub is not taking the sound fast enough") from error
            sequence += 1
            self.blocks_sent += 1
            self.gaps += chunk.gap
            self.bytes_sent += len(block)
