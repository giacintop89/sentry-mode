"""Watching one camera from one browser, without switching it off for anybody else.

Each page that shows a camera holds a preview session: a random token, the camera it
shows, and an expiry the page keeps moving forward. A session holds its own lease, so a
page that closes, or just stops renewing, gives back only its own. Another page watching
the same camera, Sentry watching it, or a recording keep it running.

This node's own camera predates leases: its preview is one switch for everybody. Sessions
on it switch the preview on when the first one opens and off when the last one closes,
but only when a session was what switched it on. A preview started from the older
controls stays theirs, and the older `/api/video/stop` still stops it for everybody.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sentry_mode.core.errors import HardwareError
from sentry_mode.sources.manager import Lease, SourceManager
from sentry_mode.sources.models import PRIMARY_CAMERA
from sentry_mode.sources.registry import SourceRegistry

SESSION_SECONDS = 30.0
MAX_SESSIONS = 16
PREVIEW_FPS = 10.0
MAX_WIDTH = 960
LIVE_SECONDS = 2.0
"""A camera whose newest frame is older than this is shown as stale, not live."""
QUIET_SECONDS = 10.0
"""A stream with no new frame for this long ends, so the page notices and asks again."""


@dataclass(eq=False)
class Session:
    token: str
    source_id: str
    expires_at: float
    lease: Lease | None
    streams: int = 0
    closed: threading.Event = field(default_factory=threading.Event)


def encode(frame: Any, max_width: int = MAX_WIDTH) -> bytes:
    import cv2

    height, width = frame.shape[:2]
    if width > max_width:
        frame = cv2.resize(frame, (max_width, max(1, round(height * max_width / width))))
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise HardwareError("the preview frame could not be encoded")
    return encoded.tobytes()


class PreviewSessions:
    def __init__(
        self,
        cameras: SourceManager,
        primary: Any,
        sources: SourceRegistry,
        *,
        seconds: float = SESSION_SECONDS,
        max_sessions: int = MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cameras = cameras
        self.primary = primary
        self.sources = sources
        self.seconds = seconds
        self.max_sessions = max_sessions
        self.clock = clock
        self._lock = threading.Lock()
        self._lifecycle = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._primary_started = False

    # -- sessions ------------------------------------------------------------------------

    def open(self, source_id: str) -> dict:
        self.expire()
        camera = self.cameras.camera(source_id)
        if camera is None:
            raise LookupError(f"{source_id} is not a camera on this hub.")
        token = secrets.token_urlsafe(18)
        with self._lifecycle:
            with self._lock:
                if len(self._sessions) >= self.max_sessions:
                    raise BlockingIOError("Too many previews are open; close one first.")
                others = any(s.source_id == PRIMARY_CAMERA for s in self._sessions.values())
            lease = None
            if source_id == PRIMARY_CAMERA:
                if not others and not self.primary.status()["running"]:
                    self.primary.start()
                    self._primary_started = True
            else:
                lease = camera.hold("preview", f"preview:{token[:8]}")
            session = Session(token, source_id, self.clock() + self.seconds, lease)
            with self._lock:
                self._sessions[token] = session
        return self._describe(session)

    def _get(self, token: str) -> Session:
        with self._lock:
            session = self._sessions.get(token)
        if session is None:
            raise LookupError("That preview has ended; open it again.")
        return session

    def renew(self, token: str) -> dict:
        self.expire()
        session = self._get(token)
        session.expires_at = self.clock() + self.seconds
        return self._describe(session)

    def close(self, token: str) -> dict:
        with self._lifecycle:
            with self._lock:
                session = self._sessions.pop(token, None)
                primary_left = any(s.source_id == PRIMARY_CAMERA for s in self._sessions.values())
            if session is None:
                return {"closed": False}
            session.closed.set()
            if session.lease is not None:
                session.lease.release()
            if session.source_id == PRIMARY_CAMERA and not primary_left and self._primary_started:
                self._primary_started = False
                self.primary.stop()
        return {"closed": True}

    def expire(self) -> None:
        now = self.clock()
        with self._lock:
            over = [
                token
                for token, session in self._sessions.items()
                if session.expires_at <= now and not session.streams
            ]
        for token in over:
            self.close(token)

    def close_all(self) -> None:
        with self._lock:
            tokens = list(self._sessions)
        for token in tokens:
            self.close(token)

    def _describe(self, session: Session) -> dict:
        return {
            "session": session.token,
            "source_id": session.source_id,
            "expires_in_seconds": round(max(0.0, session.expires_at - self.clock()), 1),
            "stream": f"/api/cameras/preview/stream?session={session.token}",
        }

    # -- pictures ------------------------------------------------------------------------

    def stream(self, token: str) -> Iterator[bytes]:
        """JPEG frames for one session, until it closes or its camera goes quiet."""
        session = self._get(token)
        with self._lock:
            session.streams += 1
        try:
            if session.source_id == PRIMARY_CAMERA:
                yield from self._primary_frames(session)
            else:
                yield from self._camera_frames(session)
        finally:
            with self._lock:
                session.streams -= 1
            session.expires_at = max(session.expires_at, self.clock() + self.seconds)

    def _primary_frames(self, session: Session) -> Iterator[bytes]:
        self.primary.add_viewer(1)
        try:
            sequence = -1
            while not session.closed.is_set():
                frame = self.primary.next_frame(sequence)
                if frame is None:
                    return
                sequence, jpeg = frame
                session.expires_at = max(session.expires_at, self.clock() + self.seconds)
                yield jpeg
        finally:
            self.primary.add_viewer(-1)

    def _camera_frames(self, session: Session) -> Iterator[bytes]:
        camera = self.cameras.camera(session.source_id)
        if camera is None:
            return
        last = None
        quiet_since = self.clock()
        while not session.closed.wait(1 / PREVIEW_FPS):
            got = camera.latest_capture()
            now = self.clock()
            if got is None or got[1] == last:
                if now - quiet_since > QUIET_SECONDS:
                    return
                continue
            frame, last = got
            quiet_since = now
            session.expires_at = max(session.expires_at, now + self.seconds)
            yield encode(camera.detection.overlay(frame))

    def snapshot(self, source_id: str) -> bytes:
        camera = self.cameras.camera(source_id)
        if camera is None:
            raise LookupError(f"{source_id} is not a camera on this hub.")
        got = camera.latest_capture()
        if got is None or self.clock() - got[1] > LIVE_SECONDS:
            raise BlockingIOError(f"{source_id} has no recent picture; open its preview first.")
        return encode(camera.detection.overlay(got[0]))

    # -- what the page lists -------------------------------------------------------------

    def listing(self) -> dict:
        self.expire()
        now = self.clock()
        with self._lock:
            previews: dict[str, int] = {}
            for session in self._sessions.values():
                previews[session.source_id] = previews.get(session.source_id, 0) + 1
        cameras = []
        for camera in self.cameras:
            source_id = camera.source_id
            status = camera.status()
            record = self.sources.get(source_id)
            got = camera.latest_capture()
            age = None if got is None else round(now - got[1], 1)
            cameras.append(
                {
                    "source_id": source_id,
                    "display_name": record.display_name if record else camera.display_name,
                    "origin": record.origin if record else "local",
                    "zone": record.zone if record else None,
                    "primary": source_id == PRIMARY_CAMERA,
                    "state": _state(status, age),
                    "frame_age_seconds": age,
                    "fps": status.get("fps"),
                    "error": status.get("error"),
                    "previews": previews.get(source_id, 0),
                }
            )
        cameras.sort(key=lambda c: (not c["primary"], c["origin"] != "local", c["source_id"]))
        return {"cameras": cameras, "session_seconds": self.seconds}


def _state(status: dict, age: float | None) -> str:
    """`idle`, `starting`, `live`, `stale` or `offline`, the same words for every camera."""
    if isinstance(status.get("state"), str):
        return status["state"]
    if not status.get("capture_running"):
        return "offline" if status.get("error") else "idle"
    if age is None:
        return "starting"
    return "live" if age <= LIVE_SECONDS else "stale"


__all__ = ["PreviewSessions", "Session", "encode"]
