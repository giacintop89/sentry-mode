import signal
from unittest.mock import patch

import pytest

from vision_node.app import run
from vision_node.config import Settings


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_run_stops_and_restores_handlers(signum):
    callbacks = {}

    def register(number, handler):
        callbacks[number] = handler
        return signal.SIG_DFL

    def inspect(config):
        callbacks[signum](signum, None)
        return "mock status"

    with (
        patch("vision_node.app.signal.signal", side_effect=register) as signals,
        patch("vision_node.app.inspect_hardware", side_effect=inspect),
        patch("vision_node.app.asdict", return_value={}),
    ):
        run(Settings())
    assert signals.call_count == 4
    assert callbacks[signal.SIGINT] == signal.SIG_DFL
    assert callbacks[signal.SIGTERM] == signal.SIG_DFL
