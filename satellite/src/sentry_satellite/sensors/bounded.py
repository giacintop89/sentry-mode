"""Reading something that may never answer, without waiting for it forever.

A 1-Wire read takes most of a second when it works and can hang when a wire comes loose;
an I2C bus with a device holding SDA low can do the same. Python cannot interrupt a
blocking read, so the read runs on a worker thread and the caller waits for it with a
deadline. A read that is still running when the next one is due is not doubled up: the
caller is told the bus is busy, and it reports that instead of a value.
"""

from collections.abc import Callable
from threading import Event, Lock, Thread
from typing import Generic, TypeVar

T = TypeVar("T")


class Timeout(RuntimeError):
    """The read did not finish in time; it may still be running."""


class Busy(RuntimeError):
    """An earlier read has not finished, so no new one was started."""


class Bounded(Generic[T]):
    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = Lock()
        self._running: Event | None = None

    def call(self, work: Callable[[], T], timeout: float) -> T:
        with self._lock:
            if self._running is not None and not self._running.is_set():
                raise Busy(f"{self.name}: the previous read has not finished")
            done = Event()
            self._running = done
        outcome: dict[str, object] = {}

        def run() -> None:
            try:
                outcome["value"] = work()
            except BaseException as error:  # noqa: BLE001 - handed back to the caller
                outcome["error"] = error
            finally:
                done.set()

        Thread(target=run, name=f"read:{self.name}", daemon=True).start()
        if not done.wait(timeout):
            raise Timeout(f"{self.name}: no answer within {timeout:g} s")
        if "error" in outcome:
            raise outcome["error"]  # type: ignore[misc]
        return outcome["value"]  # type: ignore[return-value]
