"""One model, shared by every camera, and a fair share of it for each.

Each camera offers its newest frame into a slot that holds one frame. Nothing queues: a
frame that is replaced before its turn is skipped, and a skipped frame proves nothing about
what was in front of the camera. One worker picks the source whose turn has come, runs the
model and hands the result back to that source only, tagged with the frame it came from.

A source asks for a rate. The source whose next turn is earliest goes first, and the global
budget spaces every inference, so a fast camera takes more turns but never all of them. A
sensor may briefly raise a camera's rate (a boost); the boost ends on its own and does not
lift that camera above the others' due turns.

The model is loaded once and only the worker calls it. The confidence it prefilters at is
the lowest any source asked for, set by the worker between inferences; each source then
keeps only what reaches its own threshold.

With `isolation: process` the model runs in a child process that serves one request at a
time. A request that does not come back within the timeout gets the child killed, every
source is told, and the next demand starts a fresh one.
"""

from __future__ import annotations

import importlib
import multiprocessing
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from sentry_mode.config import DetectionConfig
from sentry_mode.core.errors import HardwareError

if TYPE_CHECKING:
    from sentry_mode.vision.detection import Detection

MAX_RATE = 10.0
"""No source is served more often than this, boosted or not."""
MAX_BOOST_SECONDS = 30.0
RATE_WINDOW = 20


@dataclass(frozen=True)
class FramePacket:
    """One captured frame on its way to the model, and where it came from.

    `epoch` changes whenever the source restarts its demand, so a result that arrives for
    an earlier epoch is recognised as stale. `sequence` counts captured frames within an
    epoch; it is not the preview's JPEG sequence.
    """

    source_id: str
    epoch: int
    sequence: int
    captured: float
    frame: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class Inference:
    packet: FramePacket
    detections: list[Detection]
    elapsed: float


@dataclass
class Demand:
    fps: float
    confidence: float
    on_result: Callable[[Inference], None]
    on_error: Callable[[str], None]
    epoch: int


@dataclass
class _Source:
    demands: dict[object, Demand] = field(default_factory=dict)
    pending: FramePacket | None = None
    last_sequence: tuple[int, int] = (-1, -1)
    next_due: float = 0.0
    boost_until: float = 0.0
    boost_factor: float = 1.0
    served: deque[float] = field(default_factory=lambda: deque(maxlen=RATE_WINDOW))
    skipped: int = 0
    inferences: int = 0

    def rate(self, now: float) -> float:
        if not self.demands:
            return 0.0
        rate = max(demand.fps for demand in self.demands.values())
        if now < self.boost_until:
            rate *= self.boost_factor
        return min(rate, MAX_RATE)

    def effective(self, now: float) -> float:
        if len(self.served) < 2 or now - self.served[-1] > 5:
            return 0.0
        return (len(self.served) - 1) / max(1e-6, self.served[-1] - self.served[0])


def _resolve(factory: str) -> Callable[[DetectionConfig], Any]:
    module, _, name = factory.partition(":")
    return getattr(importlib.import_module(module), name)


class InferenceStalled(HardwareError):
    pass


class InProcessModel:
    """The model in this process. Only the scheduler's worker calls `detect`."""

    def __init__(self, config: DetectionConfig, build: Callable[[DetectionConfig], Any]) -> None:
        self.config = config
        self.detector = build(config)

    def detect(self, frame, confidence: float) -> list[Detection]:
        if self.detector.config.confidence != confidence:
            self.detector.config = self.config.model_copy(update={"confidence": confidence})
        return self.detector.detect(frame)

    def close(self) -> None:
        pass


def _serve(connection, factory: str, config: dict) -> None:
    """The child process: load once, then answer one frame at a time."""
    settings = DetectionConfig.model_validate(config)
    try:
        detector = _resolve(factory)(settings)
    except Exception as exc:
        connection.send(("error", str(exc)))
        return
    connection.send(("ready", None))
    while True:
        try:
            request = connection.recv()
        except EOFError:
            return
        if request is None:
            return
        frame, confidence = request
        try:
            if detector.config.confidence != confidence:
                detector.config = settings.model_copy(update={"confidence": confidence})
            found = detector.detect(frame)
            connection.send(("ok", [asdict(d) for d in found]))
        except Exception as exc:
            connection.send(("error", str(exc)))


class ProcessModel:
    """The model in a child process, with a watchdog on every request."""

    def __init__(
        self, config: DetectionConfig, factory: str, *, timeout: float, load_timeout: float = 60
    ) -> None:
        context = multiprocessing.get_context("spawn")
        self.timeout = timeout
        self.connection, child = context.Pipe()
        self.process = context.Process(
            target=_serve,
            args=(child, factory, config.model_dump(mode="json")),
            name="sentry-mode-inference",
            daemon=True,
        )
        self.process.start()
        child.close()
        try:
            if not self.connection.poll(load_timeout):
                raise InferenceStalled("The object model took too long to load.")
            state, detail = self.connection.recv()
        except (EOFError, OSError) as exc:
            self.close()
            raise HardwareError("The inference process exited while loading.") from exc
        except InferenceStalled:
            self.close()
            raise
        if state != "ready":
            self.close()
            raise HardwareError(detail)

    def detect(self, frame, confidence: float) -> list[Detection]:
        from sentry_mode.vision.detection import Detection

        try:
            self.connection.send((frame, confidence))
            if not self.connection.poll(self.timeout):
                self.close()
                raise InferenceStalled(
                    f"Object detection did not answer within {self.timeout:g} seconds."
                )
            state, detail = self.connection.recv()
        except (EOFError, OSError) as exc:
            self.close()
            raise HardwareError("The inference process exited.") from exc
        if state != "ok":
            raise HardwareError(detail)
        return [Detection(d["label"], d["confidence"], tuple(d["box"])) for d in detail]

    def close(self) -> None:
        if self.process.is_alive():
            try:
                self.connection.send(None)
            except OSError:
                pass
            self.process.join(1)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(5)
        self.connection.close()


class InferenceScheduler:
    FACTORY = "sentry_mode.vision.detection:ObjectDetector"

    def __init__(self, config: DetectionConfig, *, factory: str | None = None) -> None:
        self.config = config
        self.factory = factory or self.FACTORY
        self.condition = threading.Condition()
        self._model_lock = threading.Lock()  # held only by worker threads, never by callers
        self._sources: dict[str, _Source] = {}
        self._model: InProcessModel | ProcessModel | None = None
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.next_slot = 0.0
        self.error: str | None = None
        self.inference_ms: float | None = None
        self.loads = 0

    # -- the model --------------------------------------------------------------------------

    @property
    def detector(self):
        model = self._model
        return model.detector if isinstance(model, InProcessModel) else None

    def load(self) -> None:
        """Load the model now, so that a missing file is an error for the caller."""
        with self.condition:
            if self._model is not None:
                return
            self._model = self._build()
            self.loads += 1
            self.error = None

    def _build(self) -> InProcessModel | ProcessModel:
        if self.config.isolation == "process":
            return ProcessModel(
                self.config, self.factory, timeout=self.config.inference_timeout_seconds
            )
        if self.factory == self.FACTORY:
            # Looked up when it is needed, so that the detection module's name is the one used.
            from sentry_mode.vision import detection

            return InProcessModel(self.config, detection.ObjectDetector)
        return InProcessModel(self.config, _resolve(self.factory))

    # -- demand -----------------------------------------------------------------------------

    def demand(
        self,
        source_id: str,
        owner: object,
        *,
        fps: float,
        confidence: float,
        epoch: int,
        on_result: Callable[[Inference], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.load()
        with self.condition:
            source = self._sources.setdefault(source_id, _Source())
            source.demands[owner] = Demand(fps, confidence, on_result, on_error, epoch)
            self._start()
            self.condition.notify_all()

    def release(self, source_id: str, owner: object) -> None:
        """Withdraw one owner's demand. The worker stops, and is joined, once none is left."""
        with self.condition:
            source = self._sources.get(source_id)
            if source is None or source.demands.pop(owner, None) is None:
                return
            if not source.demands:
                source.pending = None
            idle = not any(s.demands for s in self._sources.values())
            thread = self.thread
            if idle:
                self._stop.set()
            self.condition.notify_all()
        if idle and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)

    def boost(self, source_id: str, factor: float, seconds: float) -> None:
        """Serve one source faster for a while; the boost always ends on its own."""
        with self.condition:
            source = self._sources.get(source_id)
            if source is None or not source.demands:
                return
            source.boost_factor = max(1.0, factor)
            source.boost_until = time.monotonic() + min(max(0.0, seconds), MAX_BOOST_SECONDS)
            source.next_due = min(source.next_due, time.monotonic())
            self.condition.notify_all()

    def offer(self, packet: FramePacket) -> bool:
        """Put a frame in its source's slot, replacing one that was not served yet."""
        with self.condition:
            source = self._sources.get(packet.source_id)
            if source is None or not source.demands:
                return False
            if not any(d.epoch == packet.epoch for d in source.demands.values()):
                return False
            if (packet.epoch, packet.sequence) <= source.last_sequence:
                return False  # a frame already served is not served again
            if source.pending is not None:
                source.skipped += 1
            source.pending = packet
            self.condition.notify_all()
            return True

    def pending(self) -> int:
        with self.condition:
            return sum(source.pending is not None for source in self._sources.values())

    # -- the worker -------------------------------------------------------------------------

    def _start(self) -> None:
        if self.thread is not None and self.thread.is_alive() and not self._stop.is_set():
            return
        self._stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, args=(self._stop,), name="sentry-mode-detection", daemon=True
        )
        self.thread.start()

    def _pick(self, now: float) -> tuple[str, _Source] | None:
        ready = [
            (source.next_due, source_id, source)
            for source_id, source in self._sources.items()
            if source.pending is not None and source.demands
        ]
        if not ready:
            return None
        _, source_id, source = min(ready, key=lambda item: (item[0], item[1]))
        return source_id, source

    def _wait_time(self, now: float) -> float | None:
        chosen = self._pick(now)
        if chosen is None:
            return None
        return max(0.0, chosen[1].next_due - now, self.next_slot - now)

    def _run(self, stop: threading.Event) -> None:
        while True:
            with self.condition:
                while True:
                    if stop.is_set():
                        return
                    now = time.monotonic()
                    delay = self._wait_time(now)
                    if delay == 0.0:
                        break
                    self.condition.wait(delay)
                chosen = self._pick(now)
                assert chosen is not None
                source_id, source = chosen
                packet = source.pending
                assert packet is not None
                source.pending = None
                source.last_sequence = (packet.epoch, packet.sequence)
                rate = source.rate(now)
                source.next_due = now + 1 / rate
                self.next_slot = now + 1 / self.config.budget_fps
                confidence = min(
                    (d.confidence for s in self._sources.values() for d in s.demands.values()),
                    default=self.config.confidence,
                )
                model = self._model
            started = time.monotonic()
            try:
                with self._model_lock:
                    if model is None:
                        raise HardwareError("Object detection is not loaded.")
                    found = model.detect(packet.frame, confidence)
            except Exception as exc:
                self._failed(stop, model, str(exc))
                return
            elapsed = time.monotonic() - started
            with self.condition:
                if stop.is_set():
                    return
                self.inference_ms = round(elapsed * 1000, 1)
                source.served.append(started)
                source.inferences += 1
                listeners = [
                    d.on_result for d in source.demands.values() if d.epoch == packet.epoch
                ]
            for deliver in listeners:
                deliver(Inference(packet, found, elapsed))

    def _failed(self, stop: threading.Event, model, message: str) -> None:
        """Every source hears of a failed inference and loses its demand.

        A child process that failed is gone, and the next demand starts another. A model in
        this process is kept: its failure was one frame's, and loading it again costs more.
        """
        dropped = isinstance(model, ProcessModel)
        with self.condition:
            if stop.is_set():
                return
            stop.set()
            self.error = message
            if dropped and self._model is model:
                self._model = None
            listeners = [d.on_error for s in self._sources.values() for d in s.demands.values()]
            for source in self._sources.values():
                source.demands.clear()
                source.pending = None
        if dropped:
            model.close()
        for tell in listeners:
            tell(message)

    def close(self) -> None:
        with self.condition:
            self._stop.set()
            for source in self._sources.values():
                source.demands.clear()
                source.pending = None
            thread, model, self._model = self.thread, self._model, None
            self.condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        if model is not None:
            model.close()

    def status(self, source_id: str | None = None) -> dict:
        now = time.monotonic()
        with self.condition:
            sources = {
                name: {
                    "requested_fps": round(source.rate(now), 2),
                    "effective_fps": round(source.effective(now), 2),
                    "boosted": now < source.boost_until,
                    "inferences": source.inferences,
                    "skipped_frames": source.skipped,
                }
                for name, source in self._sources.items()
                if source.demands or source.inferences
            }
            shared = {
                "budget_fps": self.config.budget_fps,
                "isolation": self.config.isolation,
                "loaded": self._model is not None,
                "error": self.error,
                "inference_ms": self.inference_ms,
            }
        if source_id is not None:
            return {
                **shared,
                **sources.get(source_id, {"requested_fps": 0.0, "effective_fps": 0.0}),
            }
        return {**shared, "sources": sources}


__all__ = ["FramePacket", "Inference", "InferenceScheduler", "InferenceStalled"]
