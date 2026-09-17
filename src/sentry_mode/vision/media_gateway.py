"""The one door video from a satellite comes in through.

A satellite never streams unasked. The hub opens a stream here first, which gives it a
stream id and a one-off token, and sends both to the node in a `video_start` command. The
node then connects to this port with its own certificate, says which stream it is, and
sends H.264 until the hub stops renewing.

Everything has to agree before a byte of video is read: the certificate is from the
satellites CA, it is the one the node was approved with, the node is the one the stream was
opened for, the source is the one named, the token matches, and the stream has not run
out. A stream has one publisher at a time. Nothing a node sends here reaches a browser
directly; the hub decodes it and serves its own MJPEG, as for its own camera.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sentry_mode.satellites.config import MediaConfig, MqttConfig
from sentry_mode.satellites.identity import NodeRegistry
from sentry_mode.vision.network import keyframe_offset

log = logging.getLogger(__name__)

MAX_HEADER = 1024
READ_CHUNK = 64 * 1024
MAX_WAITING_FOR_KEYFRAME = 4 * 1024 * 1024


class Sink(Protocol):
    """Where a connected stream's bytes go. `feed` raising ends the connection."""

    def feed(self, data: bytes) -> None: ...

    def close(self) -> object: ...


class GatewayError(RuntimeError):
    """A stream could not be opened."""


@dataclass
class Ticket:
    stream_id: str
    token: str
    port: int
    expires_at: float


@dataclass(eq=False)
class _Stream:
    stream_id: str
    node_id: str
    source_id: str
    digest: bytes
    expires_at: float
    connect: Callable[[], Sink]
    on_end: Callable[[str, bool], None] | None = None
    """Told why a connection ended, and whether the stream itself is over."""
    connection: socket.socket | None = None
    connections: int = 0
    bytes_in: int = 0
    last_data: float | None = None
    ended: str | None = None


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def server_context(tls: MqttConfig) -> ssl.SSLContext:
    """Mutual TLS 1.3 with the hub's certificate, trusting only the satellites CA."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=str(tls.tls_ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(certfile=str(tls.tls_cert_file), keyfile=str(tls.tls_key_file))
    return context


class MediaGateway:
    def __init__(
        self,
        config: MediaConfig,
        context: ssl.SSLContext,
        nodes: NodeRegistry,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.context = context
        self.nodes = nodes
        self.clock = clock
        self._lock = threading.Lock()
        self._streams: dict[str, _Stream] = {}
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._handlers: set[threading.Thread] = set()
        self._stopping = threading.Event()
        self.port = 0
        self.refused: dict[str, int] = {}
        self.error: str | None = None

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        listener = socket.create_server(
            (self.config.bind_host, self.config.port), reuse_port=False, backlog=8
        )
        listener.settimeout(0.5)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._stopping.clear()
        for name, target in (("accept", self._accept), ("expiry", self._expire)):
            thread = threading.Thread(target=target, name=f"media-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("waiting for satellite video on port %d", self.port)

    def stop(self) -> None:
        self._stopping.set()
        if self._listener is not None:
            self._listener.close()
        with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            self._hang_up(stream, "the hub is stopping")
        for thread in [*self._threads, *list(self._handlers)]:
            thread.join(timeout=5)
        self._threads.clear()

    # -- what the hub asks -------------------------------------------------------------

    def open(
        self,
        node_id: str,
        source_id: str,
        connect: Callable[[], Sink],
        *,
        on_end: Callable[[str, bool], None] | None = None,
        seconds: float | None = None,
    ) -> Ticket:
        """Expect one stream from one node's source, for a while."""
        if self._listener is None or self._stopping.is_set():
            raise GatewayError("the media port is not open")
        with self._lock:
            live = [s for s in self._streams.values() if s.ended is None]
            if len(live) >= self.config.max_streams:
                raise GatewayError(f"already receiving {len(live)} streams, the most allowed")
            token = secrets.token_urlsafe(32)
            stream = _Stream(
                stream_id=secrets.token_hex(8),
                node_id=node_id,
                source_id=source_id,
                digest=_digest(token),
                expires_at=self.clock() + (seconds or self.config.stream_seconds),
                connect=connect,
                on_end=on_end,
            )
            self._streams[stream.stream_id] = stream
        return Ticket(stream.stream_id, token, self.port, stream.expires_at)

    def renew(self, stream_id: str, seconds: float | None = None) -> bool:
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream.ended is not None:
                return False
            stream.expires_at = self.clock() + (seconds or self.config.stream_seconds)
            return True

    def close(self, stream_id: str, reason: str = "closed by the hub") -> None:
        with self._lock:
            stream = self._streams.pop(stream_id, None)
        if stream is not None:
            self._hang_up(stream, reason)

    def close_node(self, node_id: str, reason: str) -> int:
        """End every stream from one node: it was revoked, or its session ended."""
        with self._lock:
            ending = [s for s in self._streams.values() if s.node_id == node_id]
            for stream in ending:
                del self._streams[stream.stream_id]
        for stream in ending:
            self._hang_up(stream, reason)
        return len(ending)

    def status(self) -> dict:
        now = self.clock()
        with self._lock:
            streams = list(self._streams.values())
        return {
            "listening": self._listener is not None and not self._stopping.is_set(),
            "port": self.port,
            "error": self.error,
            "refused": dict(self.refused),
            "streams": [
                {
                    "stream_id": stream.stream_id,
                    "source_id": f"{stream.node_id}.{stream.source_id}",
                    "connected": stream.connection is not None,
                    "connections": stream.connections,
                    "bytes_in": stream.bytes_in,
                    "expires_in_seconds": round(max(0.0, stream.expires_at - now), 1),
                    "silent_for_seconds": None
                    if stream.last_data is None
                    else round(now - stream.last_data, 1),
                }
                for stream in streams
            ],
        }

    # -- the port --------------------------------------------------------------------

    def _accept(self) -> None:
        listener = self._listener
        assert listener is not None
        while not self._stopping.is_set():
            try:
                raw, address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self._handlers = {t for t in self._handlers if t.is_alive()}
            if len(self._handlers) >= self.config.max_streams * 2:
                self._refuse("too_many_connections")
                raw.close()
                continue
            thread = threading.Thread(
                target=self._serve, args=(raw, address), name="media-stream", daemon=True
            )
            self._handlers.add(thread)
            thread.start()

    def _expire(self) -> None:
        while not self._stopping.wait(0.5):
            now = self.clock()
            with self._lock:
                ended = [s for s in self._streams.values() if s.expires_at <= now]
                for stream in ended:
                    del self._streams[stream.stream_id]
            for stream in ended:
                self._hang_up(stream, "the stream was not renewed")

    def _refuse(self, reason: str) -> None:
        with self._lock:
            self.refused[reason] = self.refused.get(reason, 0) + 1

    def _hang_up(self, stream: _Stream, reason: str) -> None:
        with self._lock:
            stream.ended = stream.ended or reason
            connection = stream.connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        elif stream.on_end is not None:
            stream.on_end(reason, True)

    def _serve(self, raw: socket.socket, address) -> None:
        raw.settimeout(self.config.handshake_seconds)
        try:
            connection = self.context.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError) as exc:
            self._refuse("tls")
            log.info("a media connection from %s failed TLS: %s", address[0], exc)
            raw.close()
            return
        with connection:
            try:
                stream = self._admit(connection)
            except (OSError, ValueError) as exc:
                self._refuse("header")
                log.info("a media connection from %s sent no usable header: %s", address[0], exc)
                return
            if stream is None:
                return
            self._carry(connection, stream)

    def _admit(self, connection: ssl.SSLSocket) -> _Stream | None:
        der = connection.getpeercert(binary_form=True)
        names: Any = (connection.getpeercert() or {}).get("subject", ())
        node_id = next((v for rdn in names for k, v in rdn if k == "commonName"), None)
        header = _line(connection)
        document = json.loads(header)
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            return self._answer(connection, "bad_header", "unsupported header")
        if not der or not isinstance(node_id, str):
            return self._answer(connection, "no_certificate", "no client certificate")
        try:
            self.nodes.authenticate(
                node_id=node_id, certificate_fingerprint=hashlib.sha256(der).hexdigest()
            )
        except Exception:  # noqa: BLE001 - every refusal from the registry is a refusal
            return self._answer(connection, "not_trusted", "this node is not trusted")
        stream_id, token = document.get("stream_id"), document.get("token")
        with self._lock:
            stream = self._streams.get(stream_id) if isinstance(stream_id, str) else None
            if stream is None or stream.node_id != node_id:
                reason = "unknown_stream"
            elif stream.source_id != document.get("source_id"):
                reason = "wrong_source"
            elif not isinstance(token, str) or not hmac.compare_digest(
                _digest(token), stream.digest
            ):
                reason = "wrong_token"
            elif stream.ended is not None or stream.expires_at <= self.clock():
                reason = "expired"
            elif stream.connection is not None:
                reason = "already_connected"
            else:
                reason = None
                stream.connection = connection
                stream.connections += 1
                stream.last_data = self.clock()
        if reason is not None:
            return self._answer(connection, reason, reason.replace("_", " "))
        self._answer(connection, None, None)
        return stream

    def _answer(self, connection: ssl.SSLSocket, reason: str | None, detail: str | None):
        if reason is not None:
            self._refuse(reason)
            log.warning("refused a media connection: %s", reason)
        body = {"ok": reason is None} if reason is None else {"ok": False, "detail": detail}
        try:
            connection.sendall(json.dumps(body).encode() + b"\n")
        except OSError:
            pass
        return None

    def _carry(self, connection: ssl.SSLSocket, stream: _Stream) -> None:
        reason = "the node closed the connection"
        sink: Sink | None = None
        try:
            sink = stream.connect()
            connection.settimeout(self.config.stall_seconds)
            waiting: bytes | None = b""
            while not self._stopping.is_set():
                try:
                    data = connection.recv(READ_CHUNK)
                except TimeoutError:
                    reason = f"no video for {self.config.stall_seconds:g} s"
                    break
                if not data:
                    break
                stream.bytes_in += len(data)
                stream.last_data = self.clock()
                if waiting is not None:
                    waiting += data
                    start = keyframe_offset(waiting)
                    if start is None:
                        if len(waiting) > MAX_WAITING_FOR_KEYFRAME:
                            reason = "the stream never started with a keyframe"
                            break
                        continue
                    data, waiting = waiting[start:], None
                sink.feed(data)
        except (OSError, ssl.SSLError) as exc:
            reason = f"the connection failed: {exc}"
        except Exception as exc:  # noqa: BLE001 - the sink refused: end this stream only
            reason = str(exc) or type(exc).__name__
        finally:
            with self._lock:
                stream.connection = None
                ended = stream.ended
            if sink is not None:
                sink.close()
            final = ended or reason
            log.info("video from %s.%s ended: %s", stream.node_id, stream.source_id, final)
            if stream.on_end is not None:
                stream.on_end(final, ended is not None)


def _line(connection: ssl.SSLSocket) -> bytes:
    data = b""
    while not data.endswith(b"\n"):
        chunk = connection.recv(1)
        if not chunk:
            raise ValueError("the connection closed before the header ended")
        data += chunk
        if len(data) > MAX_HEADER:
            raise ValueError("the header is too long")
    return data


__all__ = ["GatewayError", "MediaGateway", "Sink", "Ticket", "server_context"]
