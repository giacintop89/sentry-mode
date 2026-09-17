"""A stream from a satellite, asked for while somebody holds a lease and not a moment longer.

The camera and the microphone on a node work the same way from the hub's side. The first
lease asks the satellite service for a stream; a supervisor thread renews it, notices when
data stops arriving, and asks again, with a growing wait, for as long as somebody still
wants it. The last lease given back ends the stream. What differs is what the bytes are
turned into, and that is left to the subclass.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sentry_mode.satellites.config import MediaConfig
from sentry_mode.sources.manager import PURPOSES, DemandTable, Lease

log = logging.getLogger(__name__)


@dataclass(eq=False)
class Attempt:
    """One stream that was asked for. Callbacks about an older one are ignored."""

    opened_at: float
    stream_id: str | None = None
    renewed_at: float = 0.0
    connected: bool = False
    over: bool = False


class LeasedStream:
    kind = "stream"
    """What the stream carries, as the node's commands and the logs name it."""
    purposes: tuple[str, ...] = PURPOSES

    def __init__(
        self,
        node_id: str,
        name: str,
        *,
        media: MediaConfig,
        display_name: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.node_id = node_id
        self.name = name
        self.source_id = f"{node_id}.{name}"
        self.display_name = display_name or f"{node_id} {name}"
        self.media = media
        self.clock = clock
        self.lifecycle = threading.Lock()
        self.lock = threading.Lock()
        self.demands = DemandTable(self.source_id, on_change=self._apply, purposes=self.purposes)
        self.supervisor: threading.Thread | None = None
        self.stopped = threading.Event()
        self.stopped.set()
        self.attempt: Attempt | None = None
        self.sink: Any = None
        self.last_data_at: float | None = None
        self.state = "idle"
        self.error: str | None = None
        self.last_end: str | None = None
        self.streams = 0
        self.failures = 0

    # -- what a subclass provides -----------------------------------------------------

    def _ask(self, connect: Callable[[], Any], on_end: Callable[[str, bool], None]) -> str:
        """Ask the node for a stream; the stream id comes back."""
        raise NotImplementedError

    def _renew(self, stream_id: str) -> bool:
        raise NotImplementedError

    def _cancel(self, stream_id: str) -> None:
        raise NotImplementedError

    def _open_sink(self) -> Any:
        """Where a connected stream's bytes go: something with `feed` and `close`."""
        raise NotImplementedError

    def _leased(self, leases: list[Lease]) -> None:
        """The leases changed and at least one is held. Runs under the lifecycle lock."""

    def _idle(self) -> None:
        """The last lease was given back and the stream has ended."""

    # -- leases ----------------------------------------------------------------------

    def _apply(self) -> None:
        with self.lifecycle:
            leases = self.demands.leases()
            if not leases:
                self._stop()
                return
            if self.stopped.is_set():
                self._start()
            self._leased(leases)

    def _start(self) -> None:
        if self.supervisor is not None and self.supervisor.is_alive():
            self.supervisor.join(timeout=5)
        self.stopped = threading.Event()
        with self.lock:
            self.state, self.error = "starting", None
        self.supervisor = threading.Thread(
            target=self._supervise, args=(self.stopped,), name=f"remote-{self.source_id}"
        )
        self.supervisor.start()

    def _stop(self) -> None:
        self.stopped.set()
        if self.supervisor is not None:
            self.supervisor.join(timeout=10)
        self._end_attempt("nobody is using it")
        self._idle()
        with self.lock:
            self.state = "idle"

    # -- the supervisor --------------------------------------------------------------

    def _supervise(self, stopped: threading.Event) -> None:
        wait = 1.0
        next_try = 0.0
        while not stopped.wait(0.25):
            now = self.clock()
            with self.lock:
                attempt = self.attempt
            if attempt is None or attempt.over:
                if now < next_try:
                    continue
                if self._begin(now):
                    wait = 1.0
                else:
                    next_try = now + wait
                    wait = min(wait * 2, 30.0)
                continue
            if now - attempt.renewed_at >= self.media.renew_every_seconds:
                assert attempt.stream_id is not None
                try:
                    renewed = self._renew(attempt.stream_id)
                except Exception as exc:  # noqa: BLE001 - a failed renewal is a lost stream
                    renewed, self.error = False, str(exc)
                if renewed:
                    attempt.renewed_at = now
                else:
                    self._fail("the stream could not be renewed")
                    continue
            self._judge(attempt, now)
        self._end_attempt("nobody is using it")

    def _begin(self, now: float) -> bool:
        attempt = Attempt(opened_at=now, renewed_at=now)
        with self.lock:
            self.attempt = attempt
            if self.state != "offline":
                self.state = "starting"
        try:
            stream_id = self._ask(
                lambda: self._connected(attempt),
                lambda reason, over: self._ended(attempt, reason, over),
            )
        except Exception as exc:  # noqa: BLE001 - the node or the link said no
            with self.lock:
                attempt.over = True
                self.failures += 1
                self.state, self.error = "offline", str(exc)
            return False
        with self.lock:
            attempt.stream_id = stream_id
            self.streams += 1
        return True

    def _judge(self, attempt: Attempt, now: float) -> None:
        """Say how the stream is doing, and give up on one that never delivered."""
        with self.lock:
            last = self.last_data_at
            if last is not None and now - last <= self.media.stall_seconds:
                self.state = "live"
                return
            if attempt.connected:
                self.state = "stale"
                return
            waited = now - attempt.opened_at
            if waited < self.media.connect_timeout_seconds:
                return
        self._fail(f"no {self.kind} from {self.node_id} within {waited:g} s")

    def _fail(self, reason: str) -> None:
        with self.lock:
            self.failures += 1
            self.state, self.error = "offline", reason
        self._end_attempt(reason)

    def _end_attempt(self, reason: str) -> None:
        with self.lock:
            attempt, self.attempt = self.attempt, None
            sink, self.sink = self.sink, None
        if attempt is not None and not attempt.over:
            attempt.over = True
            if attempt.stream_id is not None:
                try:
                    self._cancel(attempt.stream_id)
                except Exception:  # noqa: BLE001 - the stream ends on its own anyway
                    log.exception("%s: the stream could not be stopped", self.source_id)
        if sink is not None:
            sink.close()

    # -- callbacks from the gateway ------------------------------------------------------

    def _connected(self, attempt: Attempt) -> Any:
        with self.lock:
            if attempt is not self.attempt or attempt.over:
                raise RuntimeError("that stream is no longer wanted")
        sink = self._open_sink()
        with self.lock:
            stale, self.sink = self.sink, sink
            attempt.connected = True
        if stale is not None:
            stale.close()
        return sink

    def _ended(self, attempt: Attempt, reason: str, over: bool) -> None:
        with self.lock:
            if attempt is not self.attempt:
                return
            attempt.connected = False
            self.last_end = reason
            if over:
                attempt.over = True
            if not self.stopped.is_set():
                self.error = reason

    # -- what the rest of the hub reads ---------------------------------------------------

    def stream_status(self) -> dict:
        now = self.clock()
        with self.lock:
            attempt = self.attempt
            state = self.state
            leased = not self.stopped.is_set()
            return {
                "source_id": self.source_id,
                "node_id": self.node_id,
                "remote": True,
                "state": state,
                "capture_running": leased and state != "offline",
                "error": self.error,
                "last_end": self.last_end,
                "data_age_seconds": None
                if self.last_data_at is None
                else round(now - self.last_data_at, 2),
                "streams": self.streams,
                "failures": self.failures,
                "stream_connected": bool(attempt and attempt.connected),
            }

    def close(self) -> None:
        for lease in self.demands.leases():
            lease.release()
        with self.lifecycle:
            self._stop()


__all__ = ["Attempt", "LeasedStream"]
