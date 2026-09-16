"""The devices this node already had, described the way a satellite device will be.

Nothing about the camera, the microphone or the speaker changes here. They are simply
given the names the rest of the system will use from now on, so that a later rule can
say which camera it means once there is more than one.
"""

from __future__ import annotations

from sentry_mode.config import Settings
from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry

PRIMARY_CAMERA = "legacy-primary"
PRIMARY_MICROPHONE = "legacy-microphone"
PRIMARY_SPEAKER = "legacy-speaker"


def _state(enabled: bool) -> SourceState:
    return SourceState.READY if enabled else SourceState.DISABLED


def legacy_sources(settings: Settings) -> list[SourceRecord]:
    """Describe the node's own devices, keeping each device address exactly as configured."""
    return [
        SourceRecord(
            ref=SourceRef(name=PRIMARY_CAMERA),
            kind=SourceKind.CAMERA,
            display_name="Primary camera",
            state=_state(settings.camera.enabled),
            address=settings.camera.device,
        ),
        SourceRecord(
            ref=SourceRef(name=PRIMARY_MICROPHONE),
            kind=SourceKind.MICROPHONE,
            display_name="Primary microphone",
            state=_state(settings.microphone.enabled),
            address=settings.microphone.device,
        ),
        SourceRecord(
            ref=SourceRef(name=PRIMARY_SPEAKER),
            kind=SourceKind.SPEAKER,
            display_name="Primary speaker",
            state=_state(settings.speaker.enabled),
            address=settings.speaker.device,
        ),
    ]


def register_legacy(registry: SourceRegistry, settings: Settings) -> SourceRegistry:
    for record in legacy_sources(settings):
        registry.register(record)
    return registry
