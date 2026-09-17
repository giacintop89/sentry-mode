"""Who is using a camera, and for what, counted so that nobody releases someone else's use.

A camera runs while anyone needs it: a browser watching the preview, Sentry watching for
objects, a recording in progress. Each of those takes a lease, and gives back only that
lease. Giving it back twice changes nothing, so a count can never go below zero and a
late release cannot stop a camera another owner still needs.

The manager is the table of cameras this hub can drive. Each camera has its own
lifecycle lock; starting or stopping one never waits on another. The only lock held
across a thread join is that camera's own, and only a change to that same camera waits
on it.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry

PURPOSES = ("preview", "monitoring", "recording")
_ids = itertools.count(1)


@dataclass(eq=False)
class Lease:
    source_id: str
    purpose: str
    owner: str
    params: dict[str, Any] = field(default_factory=dict)
    id: int = field(default_factory=lambda: next(_ids))
    _release: Callable[[Lease], bool] | None = field(default=None, repr=False)

    def release(self) -> bool:
        """Give this lease back. True the first time, False on every later call."""
        release, self._release = self._release, None
        return release(self) if release is not None else False

    @property
    def active(self) -> bool:
        return self._release is not None

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class DemandTable:
    """The leases held on one source. `on_change` runs after every change, outside the
    table's own lock."""

    def __init__(self, source_id: str, on_change: Callable[[], None] | None = None) -> None:
        self.source_id = source_id
        self.on_change = on_change
        self._lock = threading.Lock()
        self._leases: dict[int, Lease] = {}

    def hold(self, purpose: str, owner: str, **params: Any) -> Lease:
        if purpose not in PURPOSES:
            raise ValueError(f"unknown purpose {purpose!r}")
        lease = Lease(self.source_id, purpose, owner, params)
        lease._release = self._drop
        with self._lock:
            self._leases[lease.id] = lease
        if self.on_change is not None:
            self.on_change()
        return lease

    def _drop(self, lease: Lease) -> bool:
        with self._lock:
            dropped = self._leases.pop(lease.id, None) is not None
        if dropped and self.on_change is not None:
            self.on_change()
        return dropped

    def count(self, purpose: str) -> int:
        with self._lock:
            return sum(lease.purpose == purpose for lease in self._leases.values())

    def leases(self, purpose: str | None = None) -> list[Lease]:
        with self._lock:
            return [
                lease
                for lease in self._leases.values()
                if purpose is None or lease.purpose == purpose
            ]

    def summary(self) -> dict[str, int]:
        return {purpose: self.count(purpose) for purpose in PURPOSES}


class Camera(Protocol):
    """What the manager needs from a camera it drives."""

    source_id: str
    display_name: str
    detection: Any
    """The camera's client of the shared inference scheduler."""

    def hold(self, purpose: str, owner: str, **params: Any) -> Lease: ...

    def status(self) -> dict: ...

    def latest_frame(self) -> Any: ...

    def close(self) -> None: ...


class SourceManager:
    """The cameras this hub drives, by source ID."""

    def __init__(self, registry: SourceRegistry) -> None:
        self.registry = registry
        self._lock = threading.Lock()
        self._cameras: dict[str, Camera] = {}

    def add(self, camera: Camera, *, register: bool = True) -> Camera:
        with self._lock:
            if camera.source_id in self._cameras:
                raise ValueError(f"camera {camera.source_id} is already managed")
            if register:
                self.registry.register(
                    SourceRecord(
                        ref=SourceRef.parse(camera.source_id),
                        kind=SourceKind.CAMERA,
                        display_name=camera.display_name,
                        state=SourceState.READY,
                    )
                )
            self._cameras[camera.source_id] = camera
        return camera

    def camera(self, source_id: str) -> Camera | None:
        with self._lock:
            return self._cameras.get(source_id)

    def __contains__(self, source_id: object) -> bool:
        with self._lock:
            return source_id in self._cameras

    def __iter__(self) -> Iterator[Camera]:
        with self._lock:
            return iter(list(self._cameras.values()))

    def status(self) -> dict[str, dict]:
        return {camera.source_id: camera.status() for camera in self}

    def close(self) -> None:
        for camera in self:
            camera.close()


__all__ = ["PURPOSES", "Camera", "DemandTable", "Lease", "SourceManager"]
