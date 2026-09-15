import signal
from unittest.mock import patch

import pytest

from sentry_mode.app import run
from sentry_mode.config import Settings


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
        patch("sentry_mode.app.signal.signal", side_effect=register) as signals,
        patch("sentry_mode.app.inspect_hardware", side_effect=inspect),
        patch("sentry_mode.app.asdict", return_value={}),
    ):
        run(Settings())
    assert signals.call_count == 4
    assert callbacks[signal.SIGINT] == signal.SIG_DFL
    assert callbacks[signal.SIGTERM] == signal.SIG_DFL


def test_embedded_runtime_does_not_install_signals_or_shutdown_server_logging():
    import threading

    stopped = threading.Event()
    stopped.set()
    with (
        patch("sentry_mode.app.signal.signal") as signals,
        patch("sentry_mode.app.logging.shutdown") as shutdown,
        patch("sentry_mode.app.inspect_hardware"),
        patch("sentry_mode.app.asdict", return_value={}),
    ):
        run(Settings(), stopped, threading.Lock())
    signals.assert_not_called()
    shutdown.assert_not_called()


def test_runtime_can_stop_while_waiting_for_stream_camera():
    import threading

    stopped = threading.Event()
    lock = threading.Lock()
    with patch("sentry_mode.app.inspect_hardware") as inspect, lock:
        thread = threading.Thread(target=run, args=(Settings(), stopped, lock))
        thread.start()
        stopped.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        inspect.assert_not_called()
