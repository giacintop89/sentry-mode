"""A node is named once, and every run of it is told apart from the last."""

import json

import pytest

from sentry_satellite import identity


def test_an_identity_is_issued_once(tmp_path):
    path = tmp_path / "identity.json"
    issued = identity.create(path, "zero-entrance", "2026-09-16T00:00:00Z")
    assert issued.node_id == "zero-entrance"
    assert json.loads(path.read_text())["node_id"] == "zero-entrance"
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_node_does_not_rename_itself_by_restarting(tmp_path):
    path = tmp_path / "identity.json"
    identity.create(path, "zero-entrance", "2026-09-16T00:00:00Z")
    with pytest.raises(identity.IdentityError, match="already holds an identity"):
        identity.create(path, "zero-garage", "2026-09-17T00:00:00Z")


def test_every_run_is_a_new_life_of_the_same_node(tmp_path):
    path = tmp_path / "identity.json"
    identity.create(path, "zero-entrance", "2026-09-16T00:00:00Z")
    first, second = identity.load(path), identity.load(path)
    assert first.node_id == second.node_id
    assert first.boot_id != second.boot_id


def test_a_node_that_was_never_provisioned_says_so(tmp_path):
    with pytest.raises(identity.IdentityError, match="has not been provisioned"):
        identity.load(tmp_path / "missing.json")


@pytest.mark.parametrize("stored", ["{", '{"provisioned_at": "now"}', '{"node_id": "NOPE"}'])
def test_an_identity_that_cannot_be_trusted_is_refused(tmp_path, stored):
    path = tmp_path / "identity.json"
    path.write_text(stored)
    with pytest.raises((identity.IdentityError, Exception)):
        identity.load(path)
