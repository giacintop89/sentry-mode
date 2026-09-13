import pytest

from sentry_node.config import load_config
from sentry_node.hardware.microphone import Microphone
from sentry_node.hardware.speaker import Speaker

pytestmark = pytest.mark.hardware


def test_speaker_output():
    Speaker(load_config().speaker).test_output()


def test_microphone_input():
    assert Microphone(load_config().microphone).test_input()["seconds"] > 0
