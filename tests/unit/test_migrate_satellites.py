"""The script that moves a node's rules to the second version, and back."""

import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = ROOT / "tests/fixtures/satellites/v1"
SCRIPT = ROOT / "scripts/migrate_satellites.py"
TOKEN = "123456:" + "s" * 35


def load_script():
    spec = importlib.util.spec_from_file_location("migrate_satellites", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def node(tmp_path):
    rules = tmp_path / "state" / "sentry.json"
    rules.parent.mkdir()
    saved = json.loads((FIXTURES / "sentry-v1-state.json").read_text())
    saved["config"]["telegram"]["bot_token"] = TOKEN
    rules.write_text(json.dumps(saved), encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"sentry_state_file": str(rules)}), encoding="utf-8")
    return config, rules


def run(capsys, *arguments):
    code = load_script().main(list(arguments))
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if code == 0 else captured.err


def test_a_dry_run_reports_without_writing_or_revealing_the_token(node, capsys):
    config, rules = node
    before = rules.read_bytes()
    code, report = run(capsys, "--config", str(config))
    assert code == 0
    assert report["dry_run"] is True and report["changed"] is False
    assert report["schema_version"] == 1 and report["action"] == "convert to version 2"
    assert [rule["id"] for rule in report["rules"]] == ["person-at-entrance", "vehicle-at-gate"]
    assert report["fault_policy"] == "global"
    assert report["telegram_token_configured"] is True
    assert TOKEN not in json.dumps(report)
    assert rules.read_bytes() == before
    assert not (rules.parent / "backups").exists()


def test_apply_keeps_a_private_backup_and_is_idempotent(node, capsys):
    config, rules = node
    original = rules.read_bytes()
    code, result = run(capsys, "--config", str(config), "--apply")
    assert code == 0 and result["changed"] is True and result["schema_version"] == 2
    saved = json.loads(rules.read_text())
    assert saved["schema_version"] == 2 and saved["revision"] == 39
    assert saved["config"]["fault_policy"] == "global"
    assert saved["config"]["telegram"]["bot_token"] == TOKEN
    assert stat.S_IMODE(rules.stat().st_mode) == 0o600

    backup = Path(result["backup"])
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    manifest = json.loads(backup.with_name(backup.name + ".manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["original"] == str(rules.resolve())
    assert len(manifest["sha256"]) == 64

    after = rules.read_bytes()
    code, again = run(capsys, "--config", str(config), "--apply")
    assert code == 0 and again["changed"] is False
    assert again["action"] == "already version 2; nothing to do"
    assert rules.read_bytes() == after
    assert len(list(backup.parent.glob("*.manifest.json"))) == 1


def test_restore_checks_the_backup_and_keeps_what_it_replaces(node, capsys):
    config, rules = node
    original = rules.read_bytes()
    _, result = run(capsys, "--config", str(config), "--apply")
    backup = Path(result["backup"])
    code, restored = run(capsys, "--config", str(config), "--restore", str(backup))
    assert code == 0 and restored["schema_version"] == 1
    assert rules.read_bytes() == original
    kept = Path(restored["previous_kept_at"])
    assert json.loads(kept.read_text())["schema_version"] == 2

    backup.write_bytes(backup.read_bytes() + b" ")
    code, error = run(capsys, "--config", str(config), "--restore", str(backup))
    assert code == 2 and "does not match its manifest" in error


def test_a_backup_of_another_file_is_not_restored_here(node, tmp_path, capsys):
    config, rules = node
    _, result = run(capsys, "--config", str(config), "--apply")
    other = tmp_path / "other.yaml"
    other.write_text(
        yaml.safe_dump({"sentry_state_file": str(tmp_path / "elsewhere.json")}), encoding="utf-8"
    )
    code, error = run(capsys, "--config", str(other), "--restore", result["backup"])
    assert code == 2 and "was taken from" in error
    assert not (tmp_path / "elsewhere.json").exists()


def test_an_unreadable_file_is_refused_and_left_alone(node, capsys):
    config, rules = node
    rules.write_text("{not json", encoding="utf-8")
    code, error = run(capsys, "--config", str(config), "--apply")
    assert code == 2 and "cannot be read" in error
    assert rules.read_text() == "{not json"


def test_a_node_without_a_rules_file_has_nothing_to_convert(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"sentry_state_file": str(tmp_path / "missing.json")}), encoding="utf-8"
    )
    code, result = run(capsys, "--config", str(config), "--apply")
    assert code == 0 and result["exists"] is False and result["changed"] is False
    assert not (tmp_path / "missing.json").exists()


def test_the_script_opens_no_hardware_or_broker(node):
    config, _ = node
    probe = (
        "import runpy, sys\n"
        f"sys.argv = ['migrate', '--config', {str(config)!r}]\n"
        "try:\n"
        f"    runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "banned = ('cv2', 'paho', 'sounddevice', 'sentry_mode.hardware',\n"
        "          'sentry_mode.vision.stream')\n"
        "print(sorted(m for m in sys.modules if m.startswith(banned)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, cwd=ROOT
    )
    assert result.stdout.strip().splitlines()[-1] == "[]"
