"""Carrying the encoder's output to the hub, for as long as the hub has asked and no longer.

One stream is one connection to the hub's media port, made with the same certificate the
node uses for MQTT. The first line on it says which stream this is and carries the one-off
token the hub sent with the command; the hub answers with one line, and after that the
connection carries nothing but H.264. A fresh encoder is started for every connection, so
the first thing the hub sees on it is a keyframe with its parameter sets.

A refusal from the hub ends the stream: the hub will ask again if it still wants one. A
lost connection, a silent encoder or an encoder that exits is retried, with a growing wait,
until the stream expires. Nothing is buffered here. If the link cannot take what the
encoder produces, the connection is dropped and started again, never queued behind.
"""

import json
import logging
import os
import select
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Protocol

from sentry_satellite.subprocesses import Child

log = logging.getLogger(__name__)

MAX_REPLY = 1024
CHUNK = 64 * 1024


class Connection(Protocol):
    def sendall(self, data: bytes, /) -> None: ...

    def recv(self, size: int, /) -> bytes: ...

    def settimeout(self, value: float | None, /) -> None: ...

    def close(self) -> None: ...


class Refused(RuntimeError):
    """The hub answered, and the answer was no."""


@dataclass(frozen=True)
class Stream:
    """What the hub asked for: which camera, under which id, to which port, proven how."""

    stream_id: str
    source_id: str
    port: int
    token: str
    hub_epoch: int


class Publisher:
    def __init__(
        self,
        stream: Stream,
        argv: Sequence[str],
        connect: Callable[[int], Connection],
        *,
        expires_at: float,
        clock: Callable[[], float] = time.monotonic,
        child: Callable[..., Child] = Child,
        stall_seconds: float = 5.0,
        reply_seconds: float = 5.0,
        send_seconds: float = 5.0,
        retry_seconds: float = 1.0,
        max_retry_seconds: float = 30.0,
    ) -> None:
        self.stream = stream
        self._argv = tuple(argv)
        self._connect = connect
        self._clock = clock
        self._child = child
        self._expires_at = expires_at
        self._stall = stall_seconds
        self._reply = reply_seconds
        self._send = send_seconds
        self._retry = retry_seconds
        self._max_retry = max_retry_seconds
        self._stop = Event()
        self._lock = Lock()
        self._connection: Connection | None = None
        self._thread: Thread | None = None
        self.state = "starting"
        self.error: str | None = None
        self.interruptions = 0
        self.last_interruption: str | None = None
        self.connections = 0
        self.encoders = 0
        self.bytes_sent = 0

    # -- control, from the command thread -----------------------------------------

    def start(self) -> None:
        self._thread = Thread(target=self._run, name=f"video:{self.stream.source_id}", daemon=True)
        self._thread.start()

    def renew(self, expires_at: float) -> None:
        with self._lock:
            self._expires_at = max(self._expires_at, expires_at)

    def stop(self) -> None:
        """Ask the stream to end, without waiting for it: this runs on the link's thread."""
        self._stop.set()
        with self._lock:
            connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def join(self, timeout: float) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            remaining = max(0.0, self._expires_at - self._clock())
        return {
            "stream_id": self.stream.stream_id,
            "state": self.state,
            "error": self.error,
            "interruptions": self.interruptions,
            "last_interruption": self.last_interruption,
            "connections": self.connections,
            "encoders": self.encoders,
            "bytes_sent": self.bytes_sent,
            "expires_in_seconds": round(remaining, 1),
        }

    # -- the stream's own thread --------------------------------------------------

    def _expired(self) -> bool:
        with self._lock:
            return self._clock() >= self._expires_at

    def _run(self) -> None:
        wait = self._retry
        try:
            while not self._stop.is_set() and not self._expired():
                started = self._clock()
                try:
                    self._once()
                except Refused as error:
                    self.error = f"the hub refused the stream: {error}"
                    log.warning("%s: %s", self.stream.source_id, self.error)
                    self.state = "refused"
                    return
                except (OSError, ValueError, RuntimeError) as error:
                    if self._stop.is_set():
                        break
                    self.error = str(error) or type(error).__name__
                    self.interruptions += 1
                    self.last_interruption = self.error
                    log.warning("%s: stream interrupted: %s", self.stream.source_id, self.error)
                if self._stop.is_set():
                    break
                # A connection that lasted a while earned a quick retry; one that failed at
                # once waits longer each time, so a hub that is down is not hammered.
                wait = self._retry if self._clock() - started > self._max_retry else wait
                self.state = "retrying"
                if self._stop.wait(wait):
                    break
                wait = min(wait * 2, self._max_retry)
            self.state = "stopped" if self._stop.is_set() else "expired"
        finally:
            with self._lock:
                self._connection = None

    def _once(self) -> None:
        self.state = "connecting"
        connection = self._connect(self.stream.port)
        with self._lock:
            self._connection = connection
        if self._stop.is_set():
            connection.close()
            return
        self.connections += 1
        encoder: Child | None = None
        try:
            self._handshake(connection)
            encoder = self._child(f"encoder:{self.stream.source_id}", self._argv, capture=True)
            encoder.start()
            self.encoders += 1
            self.state = "live"
            self.error = None
            self._copy(encoder, connection)
        finally:
            if encoder is not None:
                encoder.stop()
            with self._lock:
                self._connection = None
            try:
                connection.close()
            except OSError:
                pass

    def _handshake(self, connection: Connection) -> None:
        header = {
            "schema_version": 1,
            "stream_id": self.stream.stream_id,
            "source_id": self.stream.source_id,
            "token": self.stream.token,
        }
        connection.settimeout(self._reply)
        connection.sendall(json.dumps(header, separators=(",", ":")).encode() + b"\n")
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = connection.recv(1)
            if not chunk:
                raise ConnectionError("the hub closed the connection before answering")
            reply += chunk
            if len(reply) > MAX_REPLY:
                raise ValueError("the hub's answer is too long")
        answer = json.loads(reply)
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            detail = answer.get("detail") if isinstance(answer, dict) else None
            raise Refused(str(detail or "no reason given"))
        connection.settimeout(self._send)

    def _copy(self, encoder: Child, connection: Connection) -> None:
        pipe = encoder.stdout
        if pipe is None:
            raise RuntimeError("the encoder has no output")
        descriptor = pipe.fileno()
        quiet_since = self._clock()
        while not self._stop.is_set() and not self._expired():
            ready, _, _ = select.select([descriptor], [], [], 0.5)
            if not ready:
                if self._clock() - quiet_since > self._stall:
                    raise RuntimeError(f"the encoder said nothing for {self._stall:g} s")
                continue
            data = os.read(descriptor, CHUNK)
            if not data:
                code = encoder.stop()
                last = encoder.complaints[-1] if encoder.complaints else "no message"
                raise RuntimeError(f"the encoder exited with status {code}: {last}")
            quiet_since = self._clock()
            try:
                connection.sendall(data)
            except socket.timeout as error:
                raise ConnectionError("the hub is not taking the video fast enough") from error
            self.bytes_sent += len(data)
