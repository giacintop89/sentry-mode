"""Local network presence does not imply Internet or provider reachability."""

import socket

from sentry_mode.config import Settings
from sentry_mode.core.models import HardwareStatus
from sentry_mode.hardware.camera import Camera
from sentry_mode.hardware.microphone import Microphone
from sentry_mode.hardware.speaker import Speaker


def network_available() -> bool:
    try:
        # UDP connect selects a local route; it does not transmit packets.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("192.0.2.1", 9))
            return not connection.getsockname()[0].startswith("127.")
    except OSError:
        return False


def inspect_hardware(config: Settings) -> HardwareStatus:
    return HardwareStatus(
        Camera(config.camera).is_available(),
        Microphone(config.microphone).is_available(),
        Speaker(config.speaker).is_available(),
        network_available(),
        {"network": "local IPv4 route; Internet access is not tested"},
    )
