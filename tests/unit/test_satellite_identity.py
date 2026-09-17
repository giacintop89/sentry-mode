"""Who the hub is willing to believe, and what it takes to change that."""

import json
import subprocess
from datetime import UTC, datetime

import pytest

from sentry_mode.satellites.identity import (
    NodeRegistry,
    NotApproved,
    UnknownNode,
    WrongCertificate,
    fingerprint,
)


def test_a_node_approved_from_the_shell_is_seen_by_a_hub_that_is_already_running(tmp_path):
    """`satellite_admin.py` writes the registry while the hub holds it open.

    A hub that only read the file when it started would refuse the node for as long as it
    ran, and the person who approved it would be looking at a node that never arrives.
    """
    path = tmp_path / "nodes.json"
    running = NodeRegistry(path)
    with pytest.raises(UnknownNode):
        running.authenticate(node_id="zero-w")

    from_the_shell = NodeRegistry(path)
    crt = certificate(tmp_path, "zero-w")
    from_the_shell.register(node_id="zero-w", certificate=crt)
    from_the_shell.approve("zero-w")

    record = running.authenticate(node_id="zero-w", certificate_fingerprint=fingerprint(crt))
    assert record.status == "approved"


def test_a_running_hub_writing_the_registry_does_not_undo_what_the_shell_approved(tmp_path):
    """The hub persists the registry too, whenever it stamps a new connection.

    Writing out the document it happens to be holding would put the file back as it was
    before the node was approved: the node would arrive, be refused, and the record would
    be gone from the file as well.
    """
    path = tmp_path / "nodes.json"
    running = NodeRegistry(path)
    running.next_epoch()

    from_the_shell = NodeRegistry(path)
    from_the_shell.register(node_id="zero-w", certificate=certificate(tmp_path, "zero-w"))
    from_the_shell.approve("zero-w")

    running.next_epoch()
    assert [record.node_id for record in NodeRegistry(path).all()] == ["zero-w"]


@pytest.fixture
def registry(tmp_path) -> NodeRegistry:
    return NodeRegistry(tmp_path / "nodes.json")


def certificate(tmp_path, name: str) -> str:
    """A real self-signed certificate, because a fingerprint of a fake one proves nothing."""
    key, crt = tmp_path / f"{name}.key", tmp_path / f"{name}.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "1",
            "-subj",
            f"/CN={name}",
        ],
        check=True,
        capture_output=True,
    )
    return crt.read_text()


def test_a_node_is_registered_before_it_is_trusted(registry):
    record = registry.register("zero-entrance")
    assert record.status == "pending"
    assert record.may_publish is False
    with pytest.raises(NotApproved):
        registry.authenticate(node_id="zero-entrance")


def test_approval_pins_the_certificate_it_was_given(registry, tmp_path):
    pem = certificate(tmp_path, "zero-entrance")
    registry.register("zero-entrance", certificate=pem)
    record = registry.approve("zero-entrance")
    assert record.fingerprint == fingerprint(pem)
    assert registry.authenticate(
        node_id="zero-entrance", certificate_fingerprint=fingerprint(pem)
    ).may_publish


def test_another_certificate_from_the_same_authority_is_still_not_this_node(registry, tmp_path):
    registry.register("zero-entrance", certificate=certificate(tmp_path, "zero-entrance"))
    registry.approve("zero-entrance")
    other = fingerprint(certificate(tmp_path, "zero-entrance-again"))
    with pytest.raises(WrongCertificate):
        registry.authenticate(node_id="zero-entrance", certificate_fingerprint=other)


def test_a_node_cannot_be_approved_without_a_certificate_on_file(registry):
    registry.register("zero-entrance")
    with pytest.raises(ValueError, match="no certificate on file"):
        registry.approve("zero-entrance")


def test_the_username_and_the_client_id_have_to_be_the_node(registry, tmp_path):
    registry.register("zero-entrance", certificate=certificate(tmp_path, "zero-entrance"))
    registry.approve("zero-entrance")
    with pytest.raises(WrongCertificate, match="username"):
        registry.authenticate(node_id="zero-entrance", username="zero-garage")
    with pytest.raises(WrongCertificate, match="client id"):
        registry.authenticate(node_id="zero-entrance", client_id="zero-garage")


def test_a_name_nobody_registered_is_not_a_node(registry):
    with pytest.raises(UnknownNode):
        registry.authenticate(node_id="zero-nowhere")


def test_revoking_keeps_the_record_and_takes_the_permission(registry, tmp_path):
    registry.register("zero-entrance", certificate=certificate(tmp_path, "zero-entrance"))
    registry.approve("zero-entrance")
    record = registry.revoke("zero-entrance", reason="sold the house")
    assert record.status == "revoked"
    assert record.revoked_reason == "sold the house"
    assert registry.get("zero-entrance") is not None
    with pytest.raises(NotApproved):
        registry.authenticate(node_id="zero-entrance")


def test_a_node_can_be_approved_again_with_a_new_certificate(registry, tmp_path):
    registry.register("zero-entrance", certificate=certificate(tmp_path, "zero-entrance"))
    registry.approve("zero-entrance")
    registry.revoke("zero-entrance")
    replacement = certificate(tmp_path, "zero-entrance")
    record = registry.approve("zero-entrance", certificate=replacement)
    assert record.status == "approved"
    assert record.fingerprint == fingerprint(replacement)
    assert record.revoked_at is None


def test_renaming_a_node_does_not_rename_it(registry):
    registry.register("zero-entrance")
    record = registry.describe("zero-entrance", display_name="Ingresso", zone="entrance")
    assert record.node_id == "zero-entrance"
    assert record.display_name == "Ingresso"
    with pytest.raises(ValueError, match="not a description"):
        registry.describe("zero-entrance", node_id="zero-garage")
    with pytest.raises(ValueError, match="not a description"):
        registry.describe("zero-entrance", status="approved")


def test_the_same_name_cannot_be_registered_twice(registry):
    registry.register("zero-entrance")
    with pytest.raises(ValueError, match="already registered"):
        registry.register("zero-entrance")


def test_the_file_survives_a_restart_and_is_private(registry, tmp_path):
    registry.register("zero-entrance", display_name="Ingresso")
    path = tmp_path / "nodes.json"
    assert path.stat().st_mode & 0o777 == 0o600
    again = NodeRegistry(path)
    assert again.require("zero-entrance").display_name == "Ingresso"
    assert again.revision == registry.revision


def test_an_epoch_is_never_handed_out_twice_even_across_restarts(tmp_path):
    path = tmp_path / "nodes.json"
    first = NodeRegistry(path)
    issued = [first.next_epoch() for _ in range(3)]
    second = NodeRegistry(path)
    issued.append(second.next_epoch())
    assert issued == sorted(set(issued)) == [1, 2, 3, 4]


def test_a_fingerprint_is_of_the_certificate_not_of_the_text(tmp_path):
    pem = certificate(tmp_path, "zero-entrance")
    spaced = pem.replace("-----BEGIN CERTIFICATE-----\n", "-----BEGIN CERTIFICATE-----\n\n")
    assert fingerprint(pem) == fingerprint(spaced)
    with pytest.raises(ValueError, match="not a PEM certificate"):
        fingerprint("hello")


def test_a_node_file_written_by_hand_has_to_make_sense(tmp_path):
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps({"revision": 1, "nodes": {"x": {"node_id": "x"}}}))
    with pytest.raises(ValueError):
        NodeRegistry(path)


def test_times_are_recorded_in_utc(registry, tmp_path):
    registry.register("zero-entrance", certificate=certificate(tmp_path, "zero-entrance"))
    record = registry.approve("zero-entrance")
    assert record.approved_at.tzinfo is not None
    assert record.registered_at <= datetime.now(UTC)


def test_a_node_revoked_from_the_shell_stops_being_believed_by_a_hub_that_is_running(tmp_path):
    """The other direction of the same file, and the one that matters more.

    `satellite_admin.py revoke` says the hub refuses the node either way, because a broker
    that reloads its access control does not close the connections it already has. That is
    only true if the hub notices: a registry read once and cached would leave a revoked
    node publishing into the journal for as long as the hub stayed up.
    """
    path = tmp_path / "nodes.json"
    crt = certificate(tmp_path, "zero-w")
    from_the_shell = NodeRegistry(path)
    from_the_shell.register(node_id="zero-w", certificate=crt)
    from_the_shell.approve("zero-w")

    running = NodeRegistry(path)
    assert running.authenticate(node_id="zero-w").status == "approved"

    from_the_shell.revoke("zero-w", reason="sold the house")
    with pytest.raises(NotApproved, match="revoked"):
        running.authenticate(node_id="zero-w")
