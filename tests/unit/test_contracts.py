"""The wire format both sides of the link are held to.

The hub validates with Pydantic and the satellite, which has no Pydantic, validates
against the generated schema. These tests are what keeps the two from drifting: the same
fixtures, the same verdicts, and a schema that is never edited by hand.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sentry_mode.satellites.protocol import SCHEMA_VERSION, EventEnvelope, contract_schema

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts/satellite/v1"
VALID = sorted((CONTRACTS / "fixtures/valid").glob("*.json"))
INVALID = sorted((CONTRACTS / "fixtures/invalid").glob("*.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_there_are_fixtures_to_check():
    assert VALID and INVALID


@pytest.mark.parametrize("path", VALID, ids=lambda path: path.stem)
def test_a_message_both_sides_agreed_on_is_accepted(path):
    envelope = EventEnvelope.model_validate(load(path))
    assert envelope.schema_version == SCHEMA_VERSION


@pytest.mark.parametrize("path", INVALID, ids=lambda path: path.stem)
def test_a_message_neither_side_should_accept_is_refused(path):
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(load(path))


@pytest.mark.parametrize("path", VALID, ids=lambda path: path.stem)
def test_reading_a_message_and_writing_it_back_changes_nothing(path):
    envelope = EventEnvelope.model_validate(load(path))
    again = EventEnvelope.model_validate(envelope.model_dump(mode="json"))
    assert again == envelope


def test_the_hub_decides_which_node_a_source_belongs_to():
    envelope = EventEnvelope.model_validate(load(CONTRACTS / "fixtures/valid/sensor-motion.json"))
    assert envelope.event.ref.id == "zero-entrance.pir-1"
    assert not envelope.event.ref.is_local


def test_the_published_schema_still_matches_the_models_it_came_from():
    committed = json.loads((CONTRACTS / "event.schema.json").read_text(encoding="utf-8"))
    assert committed == contract_schema(), (
        "run scripts/generate_contracts.py: the models and the published schema disagree"
    )


def test_a_reading_taken_at_an_unreal_number_is_refused():
    message = load(CONTRACTS / "fixtures/valid/threshold-temperature.json")
    message["event"]["value"] = float("nan")
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(message)
