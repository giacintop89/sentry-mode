"""The administrative tool: it signs things, and it refuses to quietly destroy things."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "satellite_admin.py"


@pytest.fixture(scope="module")
def admin():
    spec = importlib.util.spec_from_file_location("satellite_admin", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def where(tmp_path) -> list[str]:
    return ["--directory", str(tmp_path / "pki"), "--nodes-file", str(tmp_path / "nodes.json")]


def test_a_certificate_authority_is_created_once(admin, where, tmp_path, capsys):
    assert admin.main([*where, "init-ca"]) == 0
    key = tmp_path / "pki" / "ca.key"
    assert key.stat().st_mode & 0o777 == 0o600
    assert admin.main([*where, "init-ca"]) == 2
    assert "pass --force" in capsys.readouterr().err


def test_replacing_something_keeps_what_was_there(admin, where, tmp_path):
    admin.main([*where, "init-ca"])
    before = (tmp_path / "pki" / "ca.crt").read_text()
    assert admin.main([*where, "--force", "init-ca"]) == 0
    backups = list((tmp_path / "pki").glob("ca.crt.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text() == before


def test_the_hub_certificate_is_valid_for_the_address_that_is_really_used(admin, where, tmp_path):
    admin.main([*where, "init-ca"])
    assert admin.main([*where, "hub-cert", "--address", "192.168.11.10"]) == 0
    text = subprocess.run(
        ["openssl", "x509", "-in", str(tmp_path / "pki" / "hub.crt"), "-noout", "-text"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "IP Address:192.168.11.10" in text
    assert "TLS Web Server Authentication" in text
    assert not (tmp_path / "pki" / "hub.csr").exists()


def test_a_hub_certificate_needs_an_authority_first(admin, where, capsys):
    assert admin.main([*where, "hub-cert", "--address", "192.168.11.10"]) == 2
    assert "run init-ca first" in capsys.readouterr().err


def test_a_signed_node_certificate_is_the_node_it_says_it_is(admin, where, tmp_path, capsys):
    admin.main([*where, "init-ca"])
    admin.main([*where, "node-key", "--node", "zero-entrance", "--out", str(tmp_path / "zero")])
    assert (
        admin.main([*where, "sign", "--node", "zero-entrance", "--csr", str(tmp_path / "zero.csr")])
        == 0
    )
    printed = capsys.readouterr().out
    assert "fingerprint" in printed
    assert "registered and approved" in printed
    signed = tmp_path / "pki" / "zero-entrance.crt"
    verified = subprocess.run(
        ["openssl", "verify", "-CAfile", str(tmp_path / "pki" / "ca.crt"), str(signed)],
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0


def test_a_request_cannot_be_signed_into_a_different_name(admin, where, tmp_path, capsys):
    admin.main([*where, "init-ca"])
    admin.main([*where, "node-key", "--node", "zero-entrance", "--out", str(tmp_path / "zero")])
    assert (
        admin.main([*where, "sign", "--node", "zero-garage", "--csr", str(tmp_path / "zero.csr")])
        == 2
    )
    assert "cannot be changed here" in capsys.readouterr().err


def test_a_node_is_registered_pending_and_approved_separately(admin, where, tmp_path, capsys):
    admin.main([*where, "init-ca"])
    admin.main([*where, "node-key", "--node", "zero-entrance", "--out", str(tmp_path / "zero")])
    admin.main([*where, "sign", "--node", "zero-entrance", "--csr", str(tmp_path / "zero.csr")])
    certificate = str(tmp_path / "pki" / "zero-entrance.crt")
    assert (
        admin.main([*where, "register", "--node", "zero-entrance", "--certificate", certificate])
        == 0
    )
    assert "pending" in capsys.readouterr().out
    assert admin.main([*where, "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["status"] == "pending"
    assert admin.main([*where, "approve", "--node", "zero-entrance"]) == 0
    assert "may publish" in capsys.readouterr().out


def test_the_access_list_only_names_approved_nodes(admin, where, tmp_path, capsys):
    admin.main([*where, "init-ca"])
    for name in ("zero-entrance", "zero-garage"):
        admin.main([*where, "node-key", "--node", name, "--out", str(tmp_path / name)])
        admin.main([*where, "sign", "--node", name, "--csr", str(tmp_path / f"{name}.csr")])
        admin.main(
            [
                *where,
                "register",
                "--node",
                name,
                "--certificate",
                str(tmp_path / "pki" / f"{name}.crt"),
            ]
        )
    admin.main([*where, "approve", "--node", "zero-entrance"])
    capsys.readouterr()
    assert admin.main([*where, "acl"]) == 0
    printed = capsys.readouterr().out
    assert "user zero-entrance" in printed
    assert "zero-garage" not in printed
    assert "topic write sentry/v1/nodes/zero-entrance/events" in printed
    assert "topic read sentry/v1/nodes/zero-entrance/commands" in printed
    # Nothing a node may write reaches another node.
    assert "topic write sentry/v1/nodes/+/events" not in printed


def test_revoking_says_what_still_has_to_be_done(admin, where, tmp_path, capsys):
    admin.main([*where, "init-ca"])
    admin.main([*where, "node-key", "--node", "zero-entrance", "--out", str(tmp_path / "zero")])
    admin.main([*where, "sign", "--node", "zero-entrance", "--csr", str(tmp_path / "zero.csr")])
    admin.main(
        [
            *where,
            "register",
            "--node",
            "zero-entrance",
            "--certificate",
            str(tmp_path / "pki" / "zero-entrance.crt"),
        ]
    )
    admin.main([*where, "approve", "--node", "zero-entrance"])
    capsys.readouterr()
    assert admin.main([*where, "revoke", "--node", "zero-entrance", "--reason", "sold"]) == 0
    printed = capsys.readouterr().out
    assert "does not close open connections" in printed
    assert admin.main([*where, "acl"]) == 0
    assert "user zero-entrance" not in capsys.readouterr().out


def test_an_unknown_node_is_refused_rather_than_invented(admin, where, capsys):
    assert admin.main([*where, "approve", "--node", "zero-nowhere"]) == 2
    assert "refused" in capsys.readouterr().err


def test_the_journal_can_be_read_copied_and_trimmed_one_part_at_a_time(admin, tmp_path, capsys):
    journal = ["--journal-file", str(tmp_path / "journal.sqlite3")]
    assert admin.main([*journal, "journal"]) == 0
    assert json.loads(capsys.readouterr().out)["available"] is True
    copy = tmp_path / "backup.sqlite3"
    assert admin.main([*journal, "journal", "--snapshot", str(copy)]) == 0
    assert copy.exists()
    assert admin.main([*journal, "journal", "--snapshot", str(copy)]) == 2
    assert admin.main([*journal, "journal", "--forget-events-older-than", "0"]) == 0
    assert "receipts are kept" in capsys.readouterr().out


def test_receipts_that_may_still_stop_a_replay_are_not_forgotten(admin, tmp_path, capsys):
    journal = ["--journal-file", str(tmp_path / "journal.sqlite3")]
    assert admin.main([*journal, "journal", "--forget-receipts-older-than", "0"]) == 2
    assert "replay" in capsys.readouterr().err
    assert admin.main([*journal, "journal", "--forget-receipts-older-than", "30"]) == 0
