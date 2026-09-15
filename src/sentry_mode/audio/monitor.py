"""Live microphone audio for the dashboard, streamed as raw PCM.

A compressed container (Opus in WebM) plays through an <audio> element, but the element
buffers on its own schedule, which shows up as a delay of seconds and as stutter when the
buffer runs dry. Raw 16-bit mono PCM goes straight to the Web Audio API instead, where the
page schedules every block itself and can drop ahead whenever it falls behind the picture.
At 16 kHz that costs 32 KB/s, which a local network carries comfortably.
"""

import logging
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sentry_node.config import Settings
from sentry_node.core.errors import HardwareError
from sentry_node.hardware.microphone import Microphone

RATE = 16000
MAX_LISTENERS = 2
# 20 ms of audio: small enough that a block never holds the stream back noticeably.
CHUNK = RATE // 50 * 2

logger = logging.getLogger(__name__)


class AudioMonitor:
    """Bounded fan-out of the node microphone; each listener gets its own capture."""

    def __init__(self):
        self.listeners = threading.Semaphore(MAX_LISTENERS)
        self.guard = threading.Lock()
        self.listening = 0

    @contextmanager
    def listen(self, settings: Settings) -> Iterator[Iterator[bytes]]:
        if not self.listeners.acquire(blocking=False):
            raise BlockingIOError(f"Only {MAX_LISTENERS} listeners can hear the node at once.")
        with self.guard:
            self.listening += 1
        capture = None
        try:
            device = Microphone(settings.microphone).device()
            if not shutil.which("pw-record"):
                raise HardwareError("pw-record is not installed")
            if device.backend != "pipewire":
                raise HardwareError("Live listening needs a PipeWire microphone.")
            capture = subprocess.Popen(
                [
                    "pw-record",
                    "--target",
                    device.name,
                    # A short capture buffer; the default 100 ms is most of the budget.
                    "--latency",
                    "20ms",
                    "--rate",
                    str(RATE),
                    "--channels",
                    "1",
                    "--format",
                    "s16",
                    "--raw",  # Headerless samples, so the browser can play the first block.
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            # read1 hands over whatever has been captured instead of waiting for a full
            # chunk, so a block never sits in the pipe once the microphone has spoken.
            yield iter(lambda: capture.stdout.read1(CHUNK), b"")
        finally:
            if capture is not None:
                if capture.poll() is None:
                    capture.terminate()
                    try:
                        capture.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        capture.kill()
                        capture.wait()
                capture.stdout.close()
            with self.guard:
                self.listening -= 1
            self.listeners.release()
