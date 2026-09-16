"""Drivers: the only part of the agent that touches anything physical."""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class Reading:
    """One thing a driver saw, before it becomes a message."""

    source_id: str
    kind: str
    value: object
    occurred_at: datetime
    unit: str | None = None
    quality: str = "valid"


@runtime_checkable
class Driver(Protocol):
    """A source of readings.

    `read` blocks until there is something to report or the driver is stopped. It runs on
    its own thread, so a driver that blocks forever costs its own source and nothing else:
    the agent keeps answering health checks and still stops when told to.
    """

    source_id: str

    def read(self) -> Iterator[Reading]: ...

    def stop(self) -> None: ...
