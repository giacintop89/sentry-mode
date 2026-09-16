"""The agent installs on a board that has nothing else on it.

A satellite is a 32-bit single-core machine with 426 MiB of memory. Every dependency is a
package that has to exist for ARMv6, compile there, and be kept up to date by somebody.
The rule is therefore the standard library and Paho, and this is where that rule is
enforced rather than hoped for.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SATELLITE, SOURCE

ALLOWED = {"paho", "sentry_satellite"}

pytestmark = pytest.mark.skipif(not SOURCE.is_dir(), reason="installed without its source")


def modules() -> list[Path]:
    return sorted(SOURCE.rglob("*.py"))


def imported_by(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_there_is_something_to_check():
    assert len(modules()) > 5


@pytest.mark.parametrize("path", modules(), ids=lambda path: path.stem)
def test_the_agent_imports_nothing_it_would_have_to_install(path):
    outside = imported_by(path) - ALLOWED - set(sys.stdlib_module_names)
    assert not outside, f"{path.name} imports {', '.join(sorted(outside))}"


def test_the_agent_knows_nothing_about_the_hub():
    for path in modules():
        assert "sentry_mode" not in path.read_text(encoding="utf-8")


def test_everything_but_the_link_works_without_paho_installed():
    """`validate`, `doctor` and the whole agent have to run before Paho is ever installed.

    On a fresh board the package manager is the next step, not the first one, and a doctor
    that cannot run until its own diagnosis is fixed is no use to anybody.
    """
    program = (
        "import sys;"
        "sys.modules['paho'] = None;"
        "import sentry_satellite.cli, sentry_satellite.agent, sentry_satellite.mqtt;"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SOURCE), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_the_examples_and_the_unit_agree_on_where_things_live():
    unit = (SATELLITE / "systemd" / "sentry-satellite.service").read_text(encoding="utf-8")
    assert "/etc/sentry-satellite/node.toml" in unit
    assert "/etc/sentry-satellite/identity.json" in unit
    assert "NoNewPrivileges=yes" in unit
