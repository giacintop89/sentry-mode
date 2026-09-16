"""One GPIO input line, through the kernel's character device and nothing else.

The agent never calls the GPIO library directly. It asks this module for a line and gets
back something with four methods, which is what the tests replace. The implementation
uses libgpiod 2 (`python3-libgpiod` on Raspberry Pi OS trixie), which talks to
`/dev/gpiochipN`: no root, no sysfs, and the line goes back to the kernel when it is
released, or when the process dies.

Line numbers are BCM numbers, the offsets on the chip that carries the header. On the
Zero W that is `/dev/gpiochip0`.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

BIASES = ("disabled", "pull_up", "pull_down", "as_is")


class LineError(RuntimeError):
    """The line cannot be used: missing chip, bad offset, or somebody else holds it."""


@dataclass(frozen=True)
class Edge:
    rising: bool
    timestamp_ns: int


class Line(Protocol):
    def level(self) -> bool:
        """The physical level now: True for high."""
        ...

    def wait(self, timeout: float) -> bool:
        """Block up to `timeout` seconds for an edge. True if there is at least one."""
        ...

    def edges(self) -> list[Edge]:
        """Everything that happened since the last call."""
        ...

    def release(self) -> None: ...


class GpiodLine:
    """A requested input line with edge detection on both edges."""

    def __init__(self, chip: str, offset: int, *, consumer: str, bias: str) -> None:
        try:
            import gpiod
            from gpiod.line import Bias, Direction
            from gpiod.line import Edge as GpiodEdge
        except ImportError as error:
            raise LineError("libgpiod is not installed (apt install python3-libgpiod)") from error
        self._offset = offset
        settings = gpiod.LineSettings(
            direction=Direction.INPUT,
            edge_detection=GpiodEdge.BOTH,
            bias={
                "disabled": Bias.DISABLED,
                "pull_up": Bias.PULL_UP,
                "pull_down": Bias.PULL_DOWN,
                "as_is": Bias.AS_IS,
            }[bias],
            debounce_period=timedelta(0),
        )
        try:
            self._request = gpiod.request_lines(chip, consumer=consumer, config={offset: settings})
        except FileNotFoundError as error:
            raise LineError(f"{chip} does not exist") from error
        except OSError as error:
            if error.errno == errno.EBUSY:
                holder = _holder(chip, offset)
                raise LineError(f"line {offset} on {chip} is in use by {holder}") from error
            if error.errno == errno.EINVAL:
                raise LineError(f"{chip} has no line {offset}") from error
            raise LineError(f"line {offset} on {chip}: {error}") from error
        except ValueError as error:
            raise LineError(f"{chip} has no line {offset}: {error}") from error

    def level(self) -> bool:
        from gpiod.line import Value

        try:
            return self._request.get_value(self._offset) == Value.ACTIVE
        except OSError as error:
            raise LineError(f"line {self._offset} could not be read: {error}") from error

    def wait(self, timeout: float) -> bool:
        try:
            return bool(self._request.wait_edge_events(timedelta(seconds=timeout)))
        except OSError as error:
            raise LineError(f"line {self._offset} stopped reporting: {error}") from error

    def edges(self) -> list[Edge]:
        from gpiod.edge_event import EdgeEvent

        # read_edge_events() blocks when nothing is queued, so only read what is ready.
        events = []
        try:
            while self._request.wait_edge_events(timedelta(0)):
                events.extend(self._request.read_edge_events())
        except OSError as error:
            raise LineError(f"line {self._offset} stopped reporting: {error}") from error
        return [
            Edge(
                rising=event.event_type == EdgeEvent.Type.RISING_EDGE,
                timestamp_ns=event.timestamp_ns,
            )
            for event in events
        ]

    def release(self) -> None:
        try:
            self._request.release()
        except Exception:  # noqa: BLE001 - releasing twice, or after the chip went away
            pass


def _holder(chip: str, offset: int) -> str:
    try:
        import gpiod

        with gpiod.Chip(chip) as handle:
            return handle.get_line_info(offset).consumer or "another process"
    except Exception:  # noqa: BLE001 - only used to make an error message better
        return "another process"


def open_input(chip: str, offset: int, *, consumer: str, bias: str) -> Line:
    return GpiodLine(chip, offset, consumer=consumer, bias=bias)
