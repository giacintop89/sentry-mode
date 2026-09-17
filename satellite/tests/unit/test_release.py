"""A release, an install that can be run twice, and a way back to the one before it.

The scripts are exercised against a staged prefix rather than against `/`: they write the
same layout, minus the parts that need root and systemd, so what is tested here is the
part that can go wrong quietly — what is written, what is left alone, and what is refused.
"""

import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from sentry_satellite import release

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def script(name, *arguments):
    return subprocess.run(
        ["bash", str(SCRIPTS / name), *map(str, arguments)],
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One release of this very tree, built once for every test below."""
    into = tmp_path_factory.mktemp("dist")
    return release.package(ROOT, into)


# -- what a release is -------------------------------------------------------------------


def test_the_same_tree_makes_the_same_release_twice(tmp_path):
    first = release.package(ROOT, tmp_path / "one")
    second = release.package(ROOT, tmp_path / "two")
    assert first.read_bytes() == second.read_bytes()


def test_a_release_names_every_file_by_its_checksum(built, tmp_path):
    listing = json.loads(built.with_suffix("").with_suffix(".manifest.json").read_text())
    assert listing["name"] == "sentry-satellite"
    assert "src/sentry_satellite/agent.py" in listing["files"]
    assert "systemd/sentry-satellite.service" in listing["files"]
    assert listing["release_id"] == release.release_id(listing)
    assert all(len(entry["sha256"]) == 64 for entry in listing["files"].values())


def test_a_release_holds_no_identity_and_no_configuration_of_a_node(built):
    with tarfile.open(built) as archive:
        names = archive.getnames()
    assert not [name for name in names if name.endswith(("identity.json", "node.toml"))]
    assert not [name for name in names if name.endswith((".key", ".crt", ".sqlite3"))]
    assert "config/sensor-presence.example.toml" in names


def test_a_tree_that_is_not_one_is_refused(tmp_path):
    with pytest.raises(release.ReleaseError, match="not a release"):
        release.package(tmp_path, tmp_path / "dist")


def unpacked(built, into):
    with tarfile.open(built) as archive:
        archive.extractall(into, filter="data")
    return into


def test_an_intact_release_checks_out_and_a_changed_one_does_not(built, tmp_path):
    where = unpacked(built, tmp_path / "release")
    assert release.verify(where) == []
    (where / "src/sentry_satellite/agent.py").write_text("# not what was packaged\n")
    assert release.verify(where) == [
        "src/sentry_satellite/agent.py is not the file that was packaged"
    ]


def test_a_file_that_went_missing_and_one_that_turned_up_are_both_said(built, tmp_path):
    where = unpacked(built, tmp_path / "release")
    (where / "src/sentry_satellite/names.py").unlink()
    (where / "src/sentry_satellite/extra.py").write_text("import os\n")
    problems = release.verify(where)
    assert "src/sentry_satellite/names.py is missing" in problems
    assert "src/sentry_satellite/extra.py is not part of this release" in problems


def test_without_a_manifest_nothing_can_be_said(tmp_path):
    assert "nothing to check this against" in release.verify(tmp_path)[0]
    (tmp_path / "manifest.json").write_text("{")
    assert "cannot be read as a manifest" in release.verify(tmp_path)[0]


def test_a_manifest_that_was_edited_does_not_match_its_own_id(built, tmp_path):
    where = unpacked(built, tmp_path / "release")
    listing = json.loads((where / "manifest.json").read_text())
    listing["files"].pop("README.md")
    (where / "manifest.json").write_text(json.dumps(listing))
    problems = release.verify(where)
    assert any("does not match its own release id" in problem for problem in problems)


# -- installing it -----------------------------------------------------------------------


def test_an_install_writes_the_layout_and_leaves_the_rest_of_the_board_alone(built, tmp_path):
    staged = tmp_path / "staged"
    done = script("install.sh", "--release", built, "--prefix", staged)
    assert done.returncode == 0, done.stderr
    current = staged / "opt/sentry-satellite/current"
    assert release.verify(current.resolve()) == []
    assert (staged / "etc/systemd/system/sentry-satellite.service").is_file()
    assert (staged / "etc/sentry-satellite/node.toml").is_file()
    assert (staged / "var/lib/sentry-satellite").is_dir()
    assert oct((staged / "var/lib/sentry-satellite").stat().st_mode)[-3:] == "700"
    assert oct((staged / "etc/sentry-satellite").stat().st_mode)[-3:] == "750"
    # Nothing outside its own three directories was written.
    assert sorted(p.name for p in staged.iterdir()) == ["etc", "opt", "var"]


def test_installing_the_same_release_again_changes_nothing(built, tmp_path):
    staged = tmp_path / "staged"
    script("install.sh", "--release", built, "--prefix", staged)
    node = staged / "etc/sentry-satellite/node.toml"
    node.write_text("# this node's own file\n")
    before = {p: p.stat().st_mtime_ns for p in (staged / "opt/sentry-satellite").rglob("*")}
    again = script("install.sh", "--release", built, "--prefix", staged)
    assert "already installed" in again.stdout
    assert node.read_text() == "# this node's own file\n"  # never written over
    assert {p: p.stat().st_mtime_ns for p in (staged / "opt/sentry-satellite").rglob("*")} == before


def test_a_release_whose_files_do_not_match_it_is_not_installed(built, tmp_path):
    where = unpacked(built, tmp_path / "loose")
    (where / "src/sentry_satellite/agent.py").write_text("# swapped out\n")
    tampered = tmp_path / "sentry-satellite-0.1.0.tar.gz"
    with tarfile.open(tampered, "w:gz") as archive:
        for path in sorted(where.rglob("*")):
            if path.is_file():
                archive.add(path, path.relative_to(where).as_posix())
    staged = tmp_path / "staged"
    refused = script("install.sh", "--release", tampered, "--prefix", staged)
    assert refused.returncode == 1
    assert "does not match its own manifest" in refused.stderr
    assert not (staged / "opt/sentry-satellite/current").exists()


def test_an_install_needs_a_release_to_install(tmp_path):
    assert script("install.sh", "--prefix", tmp_path).returncode == 2
    assert script("install.sh", "--release", tmp_path / "nothing.tar.gz").returncode == 2


# -- going back --------------------------------------------------------------------------


@pytest.fixture
def board(built, tmp_path):
    """A staged board with two releases installed, the newer one current."""
    staged = tmp_path / "staged"
    script("install.sh", "--release", built, "--prefix", staged)
    newer = release.package(ROOT, tmp_path / "dist", version="0.1.1")
    script("install.sh", "--release", newer, "--prefix", staged)
    return staged


def test_the_board_says_which_releases_it_has(board):
    listed = script("rollback.sh", "--prefix", board, "--list").stdout.split()
    assert "0.1.0" in listed and "(current)" in listed


def test_going_back_moves_the_link_and_nothing_else(board):
    done = script("rollback.sh", "--prefix", board, "--to", "0.1.0")
    assert done.returncode == 0, done.stderr
    assert (board / "opt/sentry-satellite/current").resolve().name == "0.1.0"
    assert (board / "opt/sentry-satellite/releases/0.1.1").is_dir()
    assert (board / "etc/sentry-satellite/node.toml").is_file()


def test_a_release_that_is_not_on_the_board_is_refused(board):
    refused = script("rollback.sh", "--prefix", board, "--to", "9.9.9")
    assert refused.returncode == 1
    assert "not on this board" in refused.stderr
    assert (board / "opt/sentry-satellite/current").resolve().name == "0.1.1"


def test_a_release_on_the_board_that_was_tampered_with_is_not_made_current(board):
    (board / "opt/sentry-satellite/releases/0.1.0/src/sentry_satellite/agent.py").write_text("#\n")
    refused = script("rollback.sh", "--prefix", board, "--to", "0.1.0")
    assert refused.returncode == 1
    assert "does not match its manifest" in refused.stderr
    assert (board / "opt/sentry-satellite/current").resolve().name == "0.1.1"


# -- the data beside it ------------------------------------------------------------------


def test_a_backup_holds_what_makes_this_node_this_node(board, tmp_path):
    (board / "etc/sentry-satellite/identity.json").write_text('{"node_id": "zero-entrance"}')
    (board / "var/lib/sentry-satellite/sources.json").write_text('{"revision": 4}')
    done = script("backup.sh", "--prefix", board, "--into", tmp_path / "backups")
    assert done.returncode == 0, done.stderr
    written = Path(done.stdout.splitlines()[0])
    assert oct(written.stat().st_mode)[-3:] == "600"
    with tarfile.open(written) as archive:
        names = archive.getnames()
    assert "etc/sentry-satellite/identity.json" in names
    assert "var/lib/sentry-satellite/sources.json" in names
    assert not [name for name in names if name.startswith("opt/")]


def test_a_rollback_can_bring_the_data_back_with_the_release(board, tmp_path):
    (board / "etc/sentry-satellite/identity.json").write_text('{"node_id": "zero-entrance"}')
    (board / "var/lib/sentry-satellite/sources.json").write_text('{"revision": 4}')
    done = script("backup.sh", "--prefix", board, "--into", tmp_path / "backups")
    taken = Path(done.stdout.splitlines()[0])
    (board / "var/lib/sentry-satellite/sources.json").write_text('{"revision": 5}')
    (board / "etc/sentry-satellite/identity.json").unlink()
    done = script("rollback.sh", "--prefix", board, "--to", "0.1.0", "--data", taken)
    assert done.returncode == 0, done.stderr
    assert (board / "var/lib/sentry-satellite/sources.json").read_text() == '{"revision": 4}'
    assert (board / "etc/sentry-satellite/identity.json").is_file()
    assert (board / "opt/sentry-satellite/current").resolve().name == "0.1.0"


def test_a_backup_of_a_board_that_has_nothing_installed_is_refused(tmp_path):
    refused = script("backup.sh", "--prefix", tmp_path, "--into", tmp_path / "backups")
    assert refused.returncode == 1
    assert "does not exist" in refused.stderr


def test_the_scripts_are_runnable_files():
    for name in ("install.sh", "rollback.sh", "backup.sh"):
        assert os.access(SCRIPTS / name, os.X_OK), name
