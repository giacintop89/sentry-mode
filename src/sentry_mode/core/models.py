from dataclasses import dataclass, field


@dataclass(frozen=True)
class AudioDevice:
    name: str
    description: str
    backend: str


@dataclass
class HardwareStatus:
    camera_available: bool
    microphone_available: bool
    speaker_available: bool
    network_available: bool
    diagnostics: dict[str, str] = field(default_factory=dict)
