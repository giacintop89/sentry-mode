import logging
import signal
import threading
from dataclasses import asdict

from vision_node.config import Settings
from vision_node.hardware.status import inspect_hardware

logger = logging.getLogger(__name__)


def run(config: Settings) -> None:
    stopped = threading.Event()
    previous = {}

    def stop(signum, frame):
        stopped.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stop)
        logger.info("Starting Vision Node: %s", config.node.name)
        # All inspection resources are released before the idle service loop.
        status = inspect_hardware(config)
        logger.info("Hardware status: %s", asdict(status))
        logger.info("Vision Node ready")
        while not stopped.wait(1):
            pass
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        logger.info("Vision Node stopped")
        logging.shutdown()
