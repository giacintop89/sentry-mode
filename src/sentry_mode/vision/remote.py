"""A camera on a satellite, driven like the hub's own: by leases.

While nobody holds a lease the node sends nothing; `LeasedStream` asks for video, renews
it and asks again while somebody still wants pictures. Frames are decoded as they arrive
and only the newest is kept; the shared scheduler takes what it has time for.

What this camera reports is what it actually receives: the decoded size, the rate measured
over the last seconds, how old the newest frame is, and why the last stream ended.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

from sentry_mode.config import DetectionConfig
from sentry_mode.satellites.config import MediaConfig
from sentry_mode.sources.manager import Lease
from sentry_mode.sources.remote import LeasedStream
from sentry_mode.vision.detection import DetectionWorker
from sentry_mode.vision.network import NetworkDecoder
from sentry_mode.vision.scheduler import InferenceScheduler

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


class RemoteCamera(LeasedStream):
    kind = "video"

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
        self.link = link
        self.decoder_factory = decoder
        self.width, self.height = _size(options)
        self.requested_fps = float(options.get("fps", 10))
        self.base_detection = detection
        super().__init__(node_id, name, media=media, display_name=display_name, clock=clock)
        self.detection = DetectionWorker(
            detection.model_copy(update={"enabled": False}), scheduler, self.source_id
        )
        self.raw_frame: Any = None
        self.captured = 0
        self.frame_times: deque[float] = deque(maxlen=64)

    @property
    def decoder(self) -> NetworkDecoder | None:
        return self.sink

    # -- leases ----------------------------------------------------------------------

    def hold(self, purpose: str, owner: str, **params: Any) -> Lease:
        params.setdefault("fps", 2.0)
        params.setdefault("detect", False)
        if params.get("confidence") is None:
            params.pop("confidence", None)
        return self.demands.hold(purpose, owner, **params)

    def _leased(self, leases: list[Lease]) -> None:
        watching = [lease for lease in leases if lease.params.get("detect")]
        if not watching:
            if self.detection.enabled:
                self.detection.configure(False, False)
            return
        asked = [lease.params["confidence"] for lease in watching if "confidence" in lease.params]
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

    def _idle(self) -> None:
        self.detection.pause()
        with self.lock:
            self.raw_frame = None

    # -- the link --------------------------------------------------------------------

    def _ask(self, connect: Callable[[], Any], on_end: Callable[[str, bool], None]) -> str:
        return self.link.start_video(self.node_id, self.name, connect, on_end)

    def _renew(self, stream_id: str) -> bool:
        return self.link.renew_video(self.node_id, stream_id)

    def _cancel(self, stream_id: str) -> None:
        self.link.stop_video(self.node_id, stream_id)

    def _open_sink(self) -> NetworkDecoder:
        decoder = self.decoder_factory(
            self.width,
            self.height,
            self._frame,
            ffmpeg=self.media.ffmpeg,
            backlog_bytes=self.media.backlog_bytes,
            name=f"decoder-{self.source_id}",
        )
        decoder.start()
        return decoder

    def _frame(self, image: Any) -> None:
        now = self.clock()
        with self.lock:
            self.raw_frame = image
            self.captured += 1
            self.last_data_at = now
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
            if self.raw_frame is None or self.last_data_at is None:
                return None
            return self.raw_frame, self.last_data_at

    def status(self) -> dict:
        summary = self.stream_status()
        summary["frame_age_seconds"] = summary.pop("data_age_seconds")
        decoder = self.decoder
        with self.lock:
            summary.update(
                width=self.width,
                height=self.height,
                requested_fps=self.requested_fps,
                captured_frames=self.captured,
            )
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


def _size(options: dict) -> tuple[int, int]:
    size = (options.get("width"), options.get("height"))
    return size if size in SIZES else (640, 480)  # type: ignore[return-value]


__all__ = ["RemoteCamera", "VideoLink"]
