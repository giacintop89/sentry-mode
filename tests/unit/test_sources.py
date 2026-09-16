"""Names for the things that produce data, and the table that keeps them straight."""

import pytest
from pydantic import ValidationError

from sentry_mode.config import CameraConfig, Settings
from sentry_mode.sources.legacy import (
    PRIMARY_CAMERA,
    PRIMARY_MICROPHONE,
    PRIMARY_SPEAKER,
    legacy_sources,
    register_legacy,
)
from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry, UnknownSource


def camera(name: str = "cam-1", node: str | None = None, **changes) -> SourceRecord:
    values = {
        "ref": SourceRef(node=node, name=name),
        "kind": SourceKind.CAMERA,
        "display_name": "A camera",
        "origin": "local" if node is None else "satellite",
        **changes,
    }
    return SourceRecord(**values)


@pytest.mark.parametrize(
    "text,node,name",
    [
        ("legacy-primary", None, "legacy-primary"),
        ("zero-entrance.pir-1", "zero-entrance", "pir-1"),
        ("a.b", "a", "b"),
    ],
)
def test_an_identifier_reads_back_as_it_was_written(text, node, name):
    ref = SourceRef.parse(text)
    assert (ref.node, ref.name) == (node, name)
    assert ref.id == text


@pytest.mark.parametrize(
    "name",
    ["Entrance", "pir_1", "-pir", "pir-", "", "pir 1", "pîr", "p" * 41, "a.b.c"],
)
def test_a_name_that_would_not_survive_a_log_line_is_refused(name):
    with pytest.raises(ValidationError):
        SourceRef(name=name)


def test_an_identifier_cannot_be_changed_once_it_is_issued():
    record = camera()
    with pytest.raises(ValidationError):
        record.ref = SourceRef(name="cam-2")
    with pytest.raises(ValidationError):
        record.ref.name = "cam-2"


def test_where_a_source_lives_has_to_match_what_it_is_called():
    with pytest.raises(ValidationError):
        SourceRecord(
            ref=SourceRef(name="cam-1"),
            kind=SourceKind.CAMERA,
            display_name="A camera",
            origin="satellite",
        )
    with pytest.raises(ValidationError):
        SourceRecord(
            ref=SourceRef(node="zero-entrance", name="cam-1"),
            kind=SourceKind.CAMERA,
            display_name="A camera",
            origin="local",
        )


def test_a_display_name_is_free_text():
    assert camera(display_name="Ingresso, sopra il cancello").display_name


def test_the_registry_answers_for_a_source_it_was_given():
    registry = SourceRegistry()
    record = registry.register(camera())
    assert registry.get("cam-1") == record
    assert registry.get(record.ref) == record
    assert registry.require("cam-1") == record
    assert "cam-1" in registry
    assert len(registry) == 1
    assert registry.of_kind(SourceKind.CAMERA) == [record]
    assert registry.of_kind(SourceKind.SENSOR) == []


def test_the_registry_says_so_instead_of_inventing_a_source():
    registry = SourceRegistry()
    assert registry.get("nothing") is None
    assert "nothing" not in registry
    with pytest.raises(UnknownSource):
        registry.require("nothing")


def test_registering_the_same_source_twice_is_not_an_error():
    registry = SourceRegistry()
    first = registry.register(camera())
    assert registry.register(camera()) == first
    assert len(registry) == 1


def test_one_identifier_cannot_mean_two_different_things():
    registry = SourceRegistry()
    registry.register(camera())
    with pytest.raises(ValueError):
        registry.register(camera(kind=SourceKind.MICROPHONE))


def test_how_a_source_is_doing_changes_without_its_identity_changing():
    registry = SourceRegistry()
    record = registry.register(camera())
    updated = registry.set_state("cam-1", SourceState.FAILED)
    assert updated.state is SourceState.FAILED
    assert updated.ref == record.ref
    assert registry.require("cam-1").state is SourceState.FAILED


@pytest.mark.parametrize(
    "device", [0, 2, "/dev/video0", "http://satellite.invalid:8080/?action=stream"]
)
def test_the_camera_keeps_the_device_it_was_configured_with(device):
    settings = Settings(camera=CameraConfig(device=device))
    record = next(r for r in legacy_sources(settings) if r.id == PRIMARY_CAMERA)
    assert record.address == device
    assert type(record.address) is type(device)


def test_the_devices_this_node_already_had_are_named_and_nothing_else():
    registry = register_legacy(SourceRegistry(), Settings())
    assert {record.id for record in registry} == {
        PRIMARY_CAMERA,
        PRIMARY_MICROPHONE,
        PRIMARY_SPEAKER,
    }
    assert all(record.origin == "local" for record in registry)
    assert all(record.ref.is_local for record in registry)
    assert registry.require(PRIMARY_CAMERA).kind is SourceKind.CAMERA
    assert registry.require(PRIMARY_MICROPHONE).kind is SourceKind.MICROPHONE
    assert registry.require(PRIMARY_SPEAKER).kind is SourceKind.SPEAKER


def test_a_device_that_is_switched_off_is_still_a_source():
    settings = Settings(camera=CameraConfig(enabled=False))
    registry = register_legacy(SourceRegistry(), settings)
    assert registry.require(PRIMARY_CAMERA).state is SourceState.DISABLED
    assert registry.require(PRIMARY_MICROPHONE).state is SourceState.READY
