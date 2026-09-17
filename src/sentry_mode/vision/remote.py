"""A camera on a satellite, driven like the hub's own: by leases.

While nobody holds a lease the node sends nothing. The first lease asks the satellite
service for a stream; a supervisor thread renews it, notices when video stops arriving,
and asks again, with a growing wait, for as long as somebody still wants pictures. Frames
are decoded as they arrive and only the newest is kept; the shared scheduler takes what it
has time for.

What this camera reports is what it actually receives: the decoded size, the rate measured
over the last seconds, how old the newest frame is, and why the last stream ended.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sentry_mode.config import DetectionConfig
from sentry_mode.satellites.config import MediaConfig
from sentry_mode.sources.manager import DemandTable, Lease
from sentry_mode.vision.detection import DetectionWorker
from sentry_mode.vision.network import NetworkDecoder
from sentry_mode.vision.scheduler import InferenceScheduler

log = logging.getLogger(__name__)

PREVIEW_FPS = 10.0
SIZES = ((320, 240), (640, 480), (1280, 720))


class VideoLink(Protocol):
    """What a remote camera needs from the satellite service."""

    def start_video(
        self,
        node_id: str,
        source_id: str,
        connect: Callable[[], Any],
        on_end: Callable[[str, bool], None],
    ) -> str: ...

    def renew_video(self, node_id: str, stream_id: str) -> bool: ...

    def stop_video(self, node_id: str, stream_id: str) -> None: ...


@dataclass(eq=False)
class _Attempt:
    """One stream the camera asked for. Callbacks about an older one are ignored."""

    opened_at: float
    stream_id: str | None = None
    renewed_at: float = 0.0
    connected: bool = False
    over: bool = False


class RemoteCamera:
    def __init__(
        self,
        node_id: str,
        name: str,
        options: dict,
        *,
        link: VideoLink,
        scheduler: InferenceScheduler,
        detection: DetectionConfig,
        media: MediaConfig,
        display_name: str | None = None,
        decoder: Callable[..., NetworkDecoder] = NetworkDecoder,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.node_id = node_id
        self.name = name
        self.source_id = f"{node_id}.{name}"
        self.display_name = display_name or f"{node_id} {name}"
        self.link = link
        self.media = media
        self.clock = clock
        self.decoder_factory = decoder
        self.width, self.height = _size(options)
        self.requested_fps = float(options.get("fps", 10))
        self.base_detection = detection
        self.detection = DetectionWorker(
            detection.model_copy(update={"enabled": False}), scheduler, self.source_id
        )
        self.lifecycle = threading.Lock()
        self.lock = threading.Lock()
        self.demands = DemandTable(self.source_id, on_change=self._apply)
        self.supervisor: threading.Thread | None = None
        self.stopped = threading.Event()
        self.stopped.set()
        self.attempt: _Attempt | None = None
        self.decoder: NetworkDecoder | None = None
        self.raw_frame: Any = None
        self.captured = 0
        self.frame_times: deque[float] = deque(maxlen=64)
        self.last_frame_at: float | None = None
        self.state = "idle"
        self.error: str | None = None
        self.last_end: str | None = None
        self.streams = 0
        self.failures = 0

    # -- leases ----------------------------------------------------------------------

    def hold(self, purpose: str, owner: str, **params: Any) -> Lease:
        params.setdefault("fps", 2.0)
        params.setdefault("detect", False)
        if params.get("confidence") is None:
            params.pop("confidence", None)
        return self.demands.hold(purpose, owner, **params)

    def _apply(self) -> None:
        with self.lifecycle:
            leases = self.demands.leases()
            if not leases:
                self._stop()
                return
            if self.stopped.is_set():
                self._start()
            watching = [lease for lease in leases if lease.params.get("detect")]
            if not watching:
                if self.detection.enabled:
                    self.detection.configure(False, False)
                return
            asked = [
                lease.params["confidence"] for lease in watching if "confidence" in lease.params
            ]
            wanted = self.base_detection.model_copy(
                update={
                    "enabled": True,
                    "max_fps": max(lease.params["fps"] for lease in watching),
                    "confidence": min(asked, default=self.base_detection.confidence),
                }
            )
            if wanted == self.detection.config and not self.detection.stopped.is_set():
                return
            self.detection.pause()
            self.detection.config = wanted
            try:
                self.detection.configure(True, True)
            except Exception as exc:
                self.detection.enabled, self.detection.error = False, str(exc)

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
        self.detection.pause()
        if self.supervisor is not None:
            self.supervisor.join(timeout=10)
        self._end_attempt("nobody is watching")
        with self.lock:
            self.raw_frame = None
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
                    renewed = self.link.renew_video(self.node_id, attempt.stream_id)
                except Exception as exc:  # noqa: BLE001 - a failed renewal is a lost stream
                    renewed, self.error = False, str(exc)
                if renewed:
                    attempt.renewed_at = now
                else:
                    self._fail("the stream could not be renewed")
                    continue
            self._judge(attempt, now)
        self._end_attempt("nobody is watching")

    def _begin(self, now: float) -> bool:
        attempt = _Attempt(opened_at=now, renewed_at=now)
        with self.lock:
            self.attempt = attempt
            if self.state != "offline":
                self.state = "starting"
        try:
            stream_id = self.link.start_video(
                self.node_id,
                self.name,
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

    def _judge(self, attempt: _Attempt, now: float) -> None:
        """Say how the stream is doing, and give up on one that never delivered."""
        with self.lock:
            last = self.last_frame_at
            if last is not None and now - last <= self.media.stall_seconds:
                self.state = "live"
                return
            if attempt.connected:
                self.state = "stale"
                return
            waited = now - attempt.opened_at
            if waited < self.media.connect_timeout_seconds:
                return
        self._fail(f"no video from {self.node_id} within {waited:g} s")

    def _fail(self, reason: str) -> None:
        with self.lock:
            self.failures += 1
            self.state, self.error = "offline", reason
        self._end_attempt(reason)

    def _end_attempt(self, reason: str) -> None:
        with self.lock:
            attempt, self.attempt = self.attempt, None
            decoder, self.decoder = self.decoder, None
        if attempt is not None and not attempt.over:
            attempt.over = True
            if attempt.stream_id is not None:
                try:
                    self.link.stop_video(self.node_id, attempt.stream_id)
                except Exception:  # noqa: BLE001 - the stream ends on its own anyway
                    log.exception("%s: the stream could not be stopped", self.source_id)
        if decoder is not None:
            decoder.close()

    # -- callbacks from the gateway ------------------------------------------------------

    def _connected(self, attempt: _Attempt) -> NetworkDecoder:
        with self.lock:
            if attempt is not self.attempt or attempt.over:
                raise RuntimeError("that stream is no longer wanted")
        decoder = self.decoder_factory(
            self.width,
            self.height,
            self._frame,
            ffmpeg=self.media.ffmpeg,
            backlog_bytes=self.media.backlog_bytes,
            name=f"decoder-{self.source_id}",
        )
        decoder.start()
        with self.lock:
            stale, self.decoder = self.decoder, decoder
            attempt.connected = True
        if stale is not None:
            stale.close()
        return decoder

    def _ended(self, attempt: _Attempt, reason: str, over: bool) -> None:
        with self.lock:
            if attempt is not self.attempt:
                return
            attempt.connected = False
            self.last_end = reason
            if over:
                attempt.over = True
            if not self.stopped.is_set():
                self.error = reason

    def _frame(self, image: Any) -> None:
        now = self.clock()
        with self.lock:
            self.raw_frame = image
            self.captured += 1
            self.last_frame_at = now
            self.frame_times.append(now)
            self.state, self.error = "live", None
        self.detection.submit(image)

    # -- what the rest of the hub reads ---------------------------------------------------

    def delivered_fps(self) -> float:
        with self.lock:
            times = list(self.frame_times)
        now = self.clock()
        recent = [t for t in times if now - t <= 5.0]
        if len(recent) < 2:
            return 0.0
        return round((len(recent) - 1) / max(recent[-1] - recent[0], 1e-6), 1)

    def latest_frame(self):
        with self.lock:
            return self.raw_frame

    def latest_capture(self):
        with self.lock:
            if self.raw_frame is None or self.last_frame_at is None:
                return None
            return self.raw_frame, self.last_frame_at

    def status(self) -> dict:
        now = self.clock()
        with self.lock:
            attempt = self.attempt
            decoder = self.decoder
            state = self.state
            leased = not self.stopped.is_set()
            summary = {
                "source_id": self.source_id,
                "node_id": self.node_id,
                "remote": True,
                "state": state,
                "capture_running": leased and state != "offline",
                "error": self.error,
                "last_end": self.last_end,
                "width": self.width,
                "height": self.height,
                "requested_fps": self.requested_fps,
                "captured_frames": self.captured,
                "frame_age_seconds": None
                if self.last_frame_at is None
                else round(now - self.last_frame_at, 2),
                "streams": self.streams,
                "failures": self.failures,
                "stream_connected": bool(attempt and attempt.connected),
            }
        summary["fps"] = self.delivered_fps()
        summary["decoder"] = (
            None
            if decoder is None
            else {
                "frames": decoder.frames,
                "bytes_in": decoder.bytes_in,
                "queued_bytes": decoder.queued_bytes,
                "error": decoder.error,
            }
        )
        summary["leases"] = self.demands.summary()
        summary["detection"] = self.detection.status()
        return summary

    def close(self) -> None:
        for lease in self.demands.leases():
            lease.release()
        with self.lifecycle:
            self._stop()


def _size(options: dict) -> tuple[int, int]:
    size = (options.get("width"), options.get("height"))
    return size if size in SIZES else (640, 480)  # type: ignore[return-value]


__all__ = ["RemoteCamera", "VideoLink"]
