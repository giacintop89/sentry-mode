"""What the agent holds on to while the hub is not listening.

A satellite that cannot reach the hub keeps reading, and the readings have to go
somewhere. That somewhere is bounded three ways — how many, how big, how old — because the
board has 426 MiB and no swap worth the name, and a queue that grows until the kernel
intervenes loses everything instead of losing the oldest thing.
"""

from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable


@dataclass
class Drops:
    """Why things were thrown away. Reported in health, never silent."""

    count: int = 0
    bytes: int = 0
    age: int = 0

    @property
    def total(self) -> int:
        return self.count + self.bytes + self.age

    def as_dict(self) -> dict:
        return {"count": self.count, "bytes": self.bytes, "age": self.age, "total": self.total}


@dataclass(frozen=True)
class Held:
    """Something waiting to be sent, and how much room it is taking up.

    What is held is the event itself, not the finished message: how long it waited is part
    of the message, and that is only known at the moment it finally goes out.
    """

    item: dict
    size: int
    queued_at: float
    topic: str
    initial: bool = False


@dataclass
class Spool:
    """A bounded queue of encoded messages, oldest dropped first."""

    max_count: int
    max_bytes: int
    max_age_seconds: float
    clock: Callable[[], float]
    _items: deque[Held] = field(default_factory=deque, init=False, repr=False)
    _bytes: int = field(default=0, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    drops: Drops = field(default_factory=Drops, init=False)

    def put(self, topic: str, item: dict, size: int, *, initial: bool = False) -> None:
        with self._lock:
            self._items.append(
                Held(item=item, size=size, queued_at=self.clock(), topic=topic, initial=initial)
            )
            self._bytes += size
            self._expire()
            while len(self._items) > self.max_count:
                self._discard("count")
            while self._bytes > self.max_bytes and self._items:
                self._discard("bytes")

    def take(self, limit: int = 1) -> list[Held]:
        """Hand out the oldest messages that are still worth sending."""
        with self._lock:
            self._expire()
            taken: list[Held] = []
            while self._items and len(taken) < limit:
                held = self._items.popleft()
                self._bytes -= held.size
                taken.append(held)
            return taken

    def queued_ms(self, held: Held) -> int:
        return max(0, int((self.clock() - held.queued_at) * 1000))

    def _expire(self) -> None:
        deadline = self.clock() - self.max_age_seconds
        while self._items and self._items[0].queued_at < deadline:
            self._discard("age")

    def requeue(self, held: Held) -> None:
        """Put back something that could not be sent, without losing its place in time."""
        with self._lock:
            self._items.appendleft(held)
            self._bytes += held.size
            while len(self._items) > self.max_count:
                self._drop_newest()

    def _drop_newest(self) -> None:
        held = self._items.pop()
        self._bytes -= held.size
        self.drops.count += 1

    def _discard(self, reason: str) -> None:
        held = self._items.popleft()
        self._bytes -= held.size
        setattr(self.drops, reason, getattr(self.drops, reason) + 1)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def nbytes(self) -> int:
        with self._lock:
            return self._bytes
