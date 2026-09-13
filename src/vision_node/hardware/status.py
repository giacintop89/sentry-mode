"""Local network presence does not imply Internet or provider reachability."""

import socket

from vision_node.config import Settings
from vision_node.core.models import HardwareStatus
from vision_node.hardware.camera import Camera
from vision_node.hardware.microphone import Microphone
from vision_node.hardware.speaker import Speaker


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
