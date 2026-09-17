"""What a release of the Pico firmware says about itself.

`firmware/pico/release.json` is the half of a manifest nothing can measure: how far each
board has been taken, and what this firmware does not do. Because it is written by hand it
is the half that can quietly stop being true, so what can be tied to something is tied
here — the platforms to the hub's catalogue, the boards to the workflow that builds them,
and the refusals in `tools/release_manifest.py` to the boards they are about.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from sentry_mode.satellites import platforms

REPO = Path(__file__).resolve().parents[2]
PICO = REPO / "firmware" / "pico"
RELEASE = json.loads((PICO / "release.json").read_text())
STATES = {"run on hardware", "built only", "experimental", "not built"}


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location(
        "release_manifest", PICO / "tools" / "release_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_manifest"] = module
    spec.loader.exec_module(module)
    return module


def test_every_board_named_is_a_kind_of_node_the_hub_knows():
    for board, said in RELEASE["boards"].items():
        assert said["platform"] in platforms.CATALOGUE, board
        assert said["state"] in STATES, board
        assert said["note"].strip()


def test_every_microcontroller_the_hub_offers_is_a_board_somebody_can_build():
    # The other direction, which is the one that catches a platform added to the hub's
    # catalogue and offered to people before any firmware was ever built for it.
    offered = {
        name for name, one in platforms.CATALOGUE.items() if one.architecture.startswith("rp")
    }
    claimed = {said["platform"] for said in RELEASE["boards"].values()}
    assert offered == claimed


def test_the_boards_in_the_file_are_the_boards_the_workflow_builds():
    workflow = (REPO / ".github" / "workflows" / "pico.yml").read_text()
    line = next(one for one in workflow.splitlines() if one.strip().startswith("board: ["))
    built = {one.strip() for one in line.split("[", 1)[1].rstrip("]").split(",")}
    assert built == set(RELEASE["boards"])


def test_nothing_is_published_as_qualified_that_the_hub_shows_as_experimental():
    # Both ways round: a board this file says was run is still experimental in the hub's
    # catalogue until something qualifies it, and nothing here may say otherwise.
    for said in RELEASE["boards"].values():
        assert platforms.CATALOGUE[said["platform"]].experimental
    assert any("experimental" in one for one in RELEASE["limitations"])


def test_what_is_not_built_says_so_rather_than_being_left_out():
    states = RELEASE["capabilities"]
    assert set(states) >= {"sensors", "presence", "audio", "bridge", "snapshot"}
    for name, said in states.items():
        assert said["state"] in STATES, name
        assert said["note"].strip()
    # The snapshot is the one PICO-10 asks for, and there is no camera on any of this.
    assert states["snapshot"]["state"] == "not built"


def test_a_manifest_is_refused_when_its_two_halves_say_different_things(tool):
    wired = platforms.CATALOGUE["pico-2-wired"]
    radio = platforms.CATALOGUE["pico-2w-sensor"]
    says_wired = {"radio": "no", "gpio": "0-29", "claims": "32", "reserved": "23,24,25,29"}
    limits = {"sources": "8", "options": "8", "configuration": "2560", "adc": "26-28"}
    no_radio = {"network": "none: this board is reached over its USB cable"}
    # The image and the board agreeing is the whole point of the check.
    tool.agrees("pico2", no_radio, says_wired, limits, wired)
    with pytest.raises(SystemExit, match="radio=no"):
        tool.agrees("pico2", {"network": "wifi, over TLS"}, says_wired, limits, wired)
    # A board with no radio that the hub would offer Bluetooth to, or sound it has no
    # second connection to send: two different mistakes, and both are refused by name.
    with pytest.raises(SystemExit, match="whose radio is no"):
        tool.agrees("pico2", no_radio, says_wired, limits, radio)
    listening = wired.model_copy(update={"streams": ("audio",)})
    with pytest.raises(SystemExit, match="no second connection"):
        tool.agrees("pico2", no_radio, says_wired, limits, listening)


def test_a_hub_that_allows_more_than_the_firmware_holds_is_refused(tool):
    wired = platforms.CATALOGUE["pico-2-wired"]
    says_wired = {"radio": "no", "gpio": "0-29", "claims": "32", "reserved": "23,24,25,29"}
    no_radio = {"network": "none: this board is reached over its USB cable"}
    with pytest.raises(SystemExit, match="this firmware plans 4"):
        tool.agrees("pico2", no_radio, says_wired, {"sources": "4", "configuration": "2560"}, wired)
    with pytest.raises(SystemExit, match="a slot on this board holds 512"):
        tool.agrees("pico2", no_radio, says_wired, {"sources": "8", "configuration": "512"}, wired)
