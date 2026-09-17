"""Just enough of the D-Bus wire protocol to talk to BlueZ, in the standard library.

The bindings Debian ships for this either need a GLib main loop to receive signals
(`python3-dbus`) or depend on `python3-gi` all the same (`python3-dbus-next`). The agent
needs a few method calls and the signals BlueZ sends about devices, on one thread, so it
speaks the protocol itself: EXTERNAL authentication on the system bus socket, messages in
little-endian, and the basic and container types. No file descriptors, no introspection,
no exported objects.
"""

from __future__ import annotations

import os
import select
import socket
import struct
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SYSTEM_BUS = "/run/dbus/system_bus_socket"
BUS_NAME = "org.freedesktop.DBus"
BUS_PATH = "/org/freedesktop/DBus"
MAX_MESSAGE = 1 << 20
"""Far more than anything BlueZ sends about a handful of devices."""

METHOD_CALL, METHOD_RETURN, ERROR, SIGNAL = 1, 2, 3, 4
NO_REPLY_EXPECTED = 0x1
FIELDS = {1: "path", 2: "interface", 3: "member", 4: "error_name", 5: "reply_serial",
          6: "destination", 7: "sender", 8: "signature"}  # fmt: skip
FIELD_TYPES = {"path": "o", "interface": "s", "member": "s", "error_name": "s",
               "reply_serial": "u", "destination": "s", "sender": "s",
               "signature": "g"}  # fmt: skip
FIELD_CODES = {name: code for code, name in FIELDS.items()}

FIXED = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d"}
ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8,
         "s": 4, "o": 4, "g": 1, "a": 4, "(": 8, "{": 8, "v": 1}  # fmt: skip


class DBusError(RuntimeError):
    """The bus, or the service at the other end, said no; or the connection is gone."""

    def __init__(self, message: str, name: str | None = None) -> None:
        super().__init__(message)
        self.name = name


@dataclass(frozen=True)
class Variant:
    signature: str
    value: Any


@dataclass
class Message:
    type: int
    serial: int
    fields: dict[str, Any] = field(default_factory=dict)
    body: tuple = ()
    raw: bytes = b""

    @property
    def path(self) -> str | None:
        return self.fields.get("path")

    @property
    def interface(self) -> str | None:
        return self.fields.get("interface")

    @property
    def member(self) -> str | None:
        return self.fields.get("member")

    @property
    def sender(self) -> str | None:
        return self.fields.get("sender")


# -- signatures --------------------------------------------------------------------------


def split_signature(signature: str) -> list[str]:
    """The complete types a signature is made of, in order."""
    types = []
    at = 0
    while at < len(signature):
        end = _complete(signature, at)
        types.append(signature[at:end])
        at = end
    return types


def _complete(signature: str, at: int) -> int:
    if at >= len(signature):
        raise DBusError(f"signature {signature!r} ends too early")
    code = signature[at]
    if code in FIXED or code in "sogv":
        return at + 1
    if code == "a":
        return _complete(signature, at + 1)
    if code in "({":
        close = ")" if code == "(" else "}"
        at += 1
        while at < len(signature) and signature[at] != close:
            at = _complete(signature, at)
        if at >= len(signature):
            raise DBusError(f"signature {signature!r} is not closed")
        return at + 1
    raise DBusError(f"signature {signature!r} has a type this client does not speak")


# -- writing -----------------------------------------------------------------------------


class _Writer:
    def __init__(self, offset: int = 0) -> None:
        self.data = bytearray()
        self.offset = offset

    def pad(self, alignment: int) -> None:
        extra = (self.offset + len(self.data)) % alignment
        if extra:
            self.data += bytes(alignment - extra)

    def write(self, signature: str, value: Any) -> None:
        code = signature[0]
        self.pad(ALIGN[code])
        if code in FIXED:
            if code == "b":
                value = 1 if value else 0
            self.data += struct.pack("<" + FIXED[code], value)
        elif code in "so":
            encoded = value.encode()
            self.data += struct.pack("<I", len(encoded)) + encoded + b"\0"
        elif code == "g":
            encoded = value.encode()
            self.data += struct.pack("<B", len(encoded)) + encoded + b"\0"
        elif code == "v":
            if not isinstance(value, Variant):
                raise DBusError("a variant has to be given as Variant(signature, value)")
            self.write("g", value.signature)
            self.write(value.signature, value.value)
        elif code == "a":
            self._array(signature[1:], value)
        elif code in "({":
            inner = split_signature(signature[1:-1])
            if len(inner) != len(value):
                raise DBusError(f"{signature} needs {len(inner)} values")
            for part, item in zip(inner, value, strict=True):
                self.write(part, item)
        else:
            raise DBusError(f"cannot write {signature}")

    def _array(self, element: str, value: Any) -> None:
        at = len(self.data)
        self.data += bytes(4)
        self.pad(ALIGN[element[0]])
        start = len(self.data)
        items = value.items() if element[0] == "{" else value
        for item in items:
            self.write(element, item)
        struct.pack_into("<I", self.data, at, len(self.data) - start)


def encode(message: Message) -> bytes:
    signature = message.fields.get("signature", "")
    body = _Writer()
    for part, value in zip(split_signature(signature), message.body, strict=True):
        body.write(part, value)
    header = _Writer()
    header.write("y", ord("l"))
    header.write("y", message.type)
    header.write("y", message.fields.pop("_flags", 0))
    header.write("y", 1)
    header.write("u", len(body.data))
    header.write("u", message.serial)
    fields = [
        (FIELD_CODES[name], Variant(FIELD_TYPES[name], value))
        for name, value in message.fields.items()
        if value is not None and (name != "signature" or value)
    ]
    header.write("a(yv)", fields)
    header.pad(8)
    return bytes(header.data + body.data)


# -- reading -----------------------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes, at: int = 0) -> None:
        self.data = data
        self.at = at

    def pad(self, alignment: int) -> None:
        self.at += -self.at % alignment

    def take(self, count: int) -> bytes:
        if self.at + count > len(self.data):
            raise DBusError("a message ends in the middle of a value")
        chunk = self.data[self.at : self.at + count]
        self.at += count
        return chunk

    def read(self, signature: str) -> Any:
        code = signature[0]
        self.pad(ALIGN[code])
        if code in FIXED:
            fmt = "<" + FIXED[code]
            (value,) = struct.unpack(fmt, self.take(struct.calcsize(fmt)))
            return bool(value) if code == "b" else value
        if code in "so":
            (length,) = struct.unpack("<I", self.take(4))
            text = self.take(length + 1)[:-1]
            return text.decode("utf-8", "replace")
        if code == "g":
            (length,) = struct.unpack("<B", self.take(1))
            return self.take(length + 1)[:-1].decode()
        if code == "v":
            inner = self.read("g")
            if len(split_signature(inner)) != 1:
                raise DBusError("a variant holds more than one value")
            return Variant(inner, self.read(inner))
        if code == "a":
            return self._array(signature[1:])
        if code == "(":
            return tuple(self.read(part) for part in split_signature(signature[1:-1]))
        if code == "{":
            key, value = split_signature(signature[1:-1])
            return self.read(key), self.read(value)
        raise DBusError(f"cannot read {signature}")

    def _array(self, element: str) -> Any:
        (length,) = struct.unpack("<I", self.take(4))
        if length > MAX_MESSAGE:
            raise DBusError("an array longer than any message")
        self.pad(ALIGN[element[0]])
        end = self.at + length
        if element == "y":
            return self.take(length)
        items = []
        while self.at < end:
            items.append(self.read(element))
        if self.at != end:
            raise DBusError("an array's contents overrun its length")
        return dict(items) if element[0] == "{" else items


def message_size(head: bytes) -> int:
    """The full length of a message from its first 16 bytes."""
    if head[:1] != b"l":
        raise DBusError("a big-endian message; this client only reads little-endian")
    body, _, fields = struct.unpack_from("<III", head, 4)
    return 16 + fields + (-fields % 8) + body


def decode(data: bytes) -> Message:
    reader = _Reader(data)
    _, kind, _, version = struct.unpack("<BBBB", reader.take(4))
    if version != 1:
        raise DBusError(f"protocol version {version}")
    length = reader.read("u")
    serial = reader.read("u")
    fields = {}
    for code, variant in reader.read("a(yv)"):
        if code in FIELDS:
            fields[FIELDS[code]] = variant.value
    reader.pad(8)
    body_reader = _Reader(data, reader.at)
    signature = fields.get("signature", "")
    body = tuple(body_reader.read(part) for part in split_signature(signature))
    if body_reader.at - reader.at != length:
        raise DBusError("a message body does not match its length")
    return Message(kind, serial, fields, body)


def plain(value: Any) -> Any:
    """A decoded value with every variant replaced by what it holds."""
    if isinstance(value, Variant):
        return plain(value.value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, tuple):
        return tuple(plain(item) for item in value)
    return value


# -- the connection ----------------------------------------------------------------------


class Connection:
    """One connection to the bus, used from one thread."""

    def __init__(self, address: str = SYSTEM_BUS, *, timeout: float = 5.0) -> None:
        self.timeout = timeout
        self.signals: deque[Message] = deque()
        self.skipped = 0
        """Signals dropped unread because nobody wanted them."""
        self._serial = 0
        self._buffer = b""
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.settimeout(timeout)
        try:
            self._socket.connect(address)
            self._authenticate()
            self.unique_name = self.call(BUS_NAME, BUS_PATH, BUS_NAME, "Hello")[0]
        except (OSError, DBusError) as error:
            self._socket.close()
            raise DBusError(f"no system bus: {error}") from error

    def _authenticate(self) -> None:
        uid = str(os.geteuid()).encode().hex().encode()
        self._socket.sendall(b"\0AUTH EXTERNAL " + uid + b"\r\n")
        reply = b""
        while not reply.endswith(b"\r\n"):
            chunk = self._socket.recv(256)
            if not chunk:
                raise DBusError("the bus closed the connection while authenticating")
            reply += chunk
        if not reply.startswith(b"OK "):
            raise DBusError(f"the bus refused this user: {reply.strip().decode(errors='replace')}")
        self._socket.sendall(b"BEGIN\r\n")

    def close(self) -> None:
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()

    def fileno(self) -> int:
        return self._socket.fileno()

    def send(
        self,
        destination: str | None,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: tuple = (),
        *,
        reply: bool = True,
    ) -> int:
        self._serial += 1
        message = Message(
            METHOD_CALL,
            self._serial,
            {
                "path": path,
                "interface": interface,
                "member": member,
                "destination": destination,
                "signature": signature,
                "_flags": 0 if reply else NO_REPLY_EXPECTED,
            },
            body,
        )
        try:
            self._socket.sendall(encode(message))
        except OSError as error:
            raise DBusError(f"the bus connection is gone: {error}") from error
        return self._serial

    def call(
        self,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: tuple = (),
    ) -> tuple:
        """Call a method and wait for its answer; signals that arrive meanwhile are kept."""
        serial = self.send(destination, path, interface, member, signature, body)
        while True:
            message = self.receive(self.timeout)
            if message is None:
                raise DBusError(f"{destination} did not answer {member} in time")
            if message.type == SIGNAL:
                self.signals.append(message)
                continue
            if message.fields.get("reply_serial") != serial:
                continue
            if message.type == ERROR:
                name = message.fields.get("error_name")
                detail = message.body[0] if message.body else name
                raise DBusError(f"{member}: {detail}", name)
            return plain(message.body)

    def add_match(self, rule: str) -> None:
        self.call(BUS_NAME, BUS_PATH, BUS_NAME, "AddMatch", "s", (rule,))

    def next_signal(
        self, timeout: float, keep: Callable[[bytes], bool] | None = None
    ) -> Message | None:
        """The next signal, or None if none came in `timeout` seconds.

        `keep` sees each signal as it arrived, before it is decoded; one it turns down is
        dropped unread. Decoding is most of the cost of a busy bus on a Zero W, and most
        of what BlueZ says is about devices nobody asked about.
        """
        while self.signals:
            queued = self.signals.popleft()
            if keep is None or keep(queued.raw):
                return queued
        message = self.receive(timeout, keep)
        while message is not None and message.type != SIGNAL:
            message = self.receive(0, keep)
        return message

    def receive(
        self, timeout: float, keep: Callable[[bytes], bool] | None = None
    ) -> Message | None:
        """The next message, or None if nothing came in `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while True:
            message = self._take(keep)
            if message is not None:
                return message
            timeout = max(0.0, deadline - time.monotonic())
            try:
                ready, _, _ = select.select([self._socket], [], [], timeout)
            except (OSError, ValueError) as error:
                raise DBusError(f"the bus connection is gone: {error}") from error
            if not ready:
                return None
            try:
                chunk = self._socket.recv(65536)
            except OSError as error:
                raise DBusError(f"the bus connection is gone: {error}") from error
            if not chunk:
                raise DBusError("the bus closed the connection")
            self._buffer += chunk

    def _take(self, keep: Callable[[bytes], bool] | None) -> Message | None:
        while len(self._buffer) >= 16:
            size = message_size(self._buffer[:16])
            if size > MAX_MESSAGE:
                raise DBusError(f"a message of {size} bytes")
            if len(self._buffer) < size:
                return None
            data, self._buffer = self._buffer[:size], self._buffer[size:]
            if data[1] == SIGNAL and keep is not None and not keep(data):
                self.skipped += 1
                continue
            message = decode(data)
            message.raw = data
            return message
        return None


__all__ = ["Connection", "DBusError", "Message", "Variant", "plain", "split_signature"]
