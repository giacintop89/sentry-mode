import logging
import signal
import threading
from _thread import LockType
from dataclasses import asdict

from sentry_mode.config import Settings
from sentry_mode.hardware.status import inspect_hardware

logger = logging.getLogger(__name__)


def run(
    config: Settings,
    stopped: threading.Event | None = None,
    hardware_lock: LockType | None = None,
) -> None:
    standalone = stopped is None
    stopped = stopped if stopped is not None else threading.Event()
    previous = {}

    def stop(signum, frame):
        stopped.set()

    try:
        if standalone:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, stop)
        logger.info("Starting Sentry Mode: %s", config.node.name)
        # All inspection resources are released before the idle service loop.
        if hardware_lock is None:
            status = inspect_hardware(config)
        else:
            while not hardware_lock.acquire(timeout=0.2):
                if stopped.is_set():
                    return
            try:
                status = inspect_hardware(config)
            finally:
                hardware_lock.release()
        logger.info("Hardware status: %s", asdict(status))
        logger.info("Sentry Mode ready")
        while not stopped.wait(1):
            pass
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        logger.info("Sentry Mode stopped")
        if standalone:
            logging.shutdown()
