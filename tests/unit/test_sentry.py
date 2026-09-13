import json
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from sentry_node.config import Settings
from sentry_node.core.errors import HardwareError
from sentry_node.sentry.config import (
    Rule,
    SentryConfig,
    SSHAction,
    SSHCommand,
    TelegramAction,
    TelegramConfig,
    TTSAction,
)
from sentry_node.sentry.engine import Job, RuleState, Sentry
from sentry_node.vision.detection import Detection

PERSON = Detection("person", 0.9, (0.2, 0.2, 0.8, 0.8))


@pytest.fixture
def sentry(tmp_path):
    video = MagicMock()
    video.status.return_value = {
        "capture_running": True,
        "error": None,
        "detection": {"enabled": True, "error": None},
    }
    engine = Sentry(Settings(sentry_state_file=tmp_path / "sentry.json"), video, threading.Lock())
    try:
        yield engine
    finally:
        engine.disarm()


def direct_arm(engine):
    engine.armed = True
    engine.armed_at = 99
    engine.cancelled.clear()
    engine.states = {r.name: RuleState() for r in engine.config.rules}


def sample(engine, timestamp, detections=None):
    with patch("sentry_node.sentry.engine.time.monotonic", return_value=timestamp):
        engine.observe([PERSON] if detections is None else detections, timestamp)


def events(engine, kind):
    return [e for e in engine.status()["events"] if e["kind"] == kind]


def test_confirmation_test_mode_and_no_retrigger_while_present(sentry):
    direct_arm(sentry)
    with patch("sentry_node.sentry.engine.speak") as speech, patch("subprocess.Popen") as ssh:
        sample(sentry, 100)
        sample(sentry, 100.5)
        assert not events(sentry, "triggered")
        sample(sentry, 101)
        assert len(events(sentry, "triggered")) == 1
        assert len(events(sentry, "would_run")) == 1
        for index in range(150):
            sample(sentry, 101.5 + index * 0.5)
        assert len(events(sentry, "triggered")) == 1
        speech.assert_not_called()
        ssh.assert_not_called()
        assert sentry.jobs.empty()


def test_real_absence_rearms_and_cooldown_is_independent(sentry):
    sentry.config.rules[0].rearm_after_absence_seconds = 2
    sentry.config.rules[0].cooldown_seconds = 10
    direct_arm(sentry)
    for t in [100, 100.5, 101]:
        sample(sentry, t)
    for t in [101.5, 102, 102.5, 103, 103.5]:
        sample(sentry, t, [])
    assert len(events(sentry, "rearmed")) == 1
    for t in [104, 104.5, 105, 106, 107, 108, 109, 110]:
        sample(sentry, t)
    assert len(events(sentry, "triggered")) == 1
    sample(sentry, 111)
    assert len(events(sentry, "triggered")) == 2


def test_duplicate_stale_and_missing_frames_cannot_confirm_or_fake_absence(sentry):
    direct_arm(sentry)
    sample(sentry, 100)
    sample(sentry, 100)
    assert sentry.states["Person at entrance"].hits == 1
    with patch("sentry_node.sentry.engine.time.monotonic", return_value=120):
        sentry.observe([PERSON], 100.5)
    assert sentry.states["Person at entrance"].hits == 1
    sample(sentry, 120)
    assert sentry.states["Person at entrance"].hits == 1
    sample(sentry, 120.5)
    sample(sentry, 121)
    sample(sentry, 122, [])
    sample(sentry, 200, [])
    assert not events(sentry, "rearmed")  # Missing samples are not observed absence.


def test_confidence_region_count_and_class_filter(sentry):
    rule = sentry.config.rules[0]
    rule.region = (0.4, 0.4, 0.6, 0.6)
    rule.min_count = 2
    direct_arm(sentry)
    invalid = [
        Detection("dog", 0.99, PERSON.box),
        Detection("person", 0.2, PERSON.box),
        Detection("person", 0.99, (0, 0, 0.1, 0.1)),
    ]
    for t in [100, 100.5, 101]:
        sample(sentry, t, invalid + [PERSON])
    assert not events(sentry, "triggered")
    for t in [102, 102.5, 103]:
        sample(sentry, t, [PERSON, PERSON])
    assert len(events(sentry, "triggered")) == 1


def test_configuration_is_atomic_revision_checked_and_never_auto_arms(sentry):
    config = SentryConfig(test_mode=False)
    sentry.update(config, 0)
    assert sentry.revision == 1
    saved = json.loads(sentry.settings.sentry_state_file.read_text())
    assert saved["config"]["test_mode"] is False
    assert sentry.settings.sentry_state_file.stat().st_mode & 0o077 == 0
    with pytest.raises(BlockingIOError, match="another tab"):
        sentry.update(config, 0)
    restarted = Sentry(sentry.settings, sentry.video, threading.Lock())
    assert not restarted.status()["armed"]
    assert restarted.configuration()["config"]["test_mode"] is False
    assert restarted.revision == 1
    sentry.arm()
    with pytest.raises(BlockingIOError, match="Disarm"):
        sentry.update(config, 1)


def test_invalid_saved_configuration_prevents_arming(tmp_path):
    path = tmp_path / "sentry.json"
    path.write_text("{broken")
    engine = Sentry(Settings(sentry_state_file=path), MagicMock(), threading.Lock())
    with pytest.raises(ValueError, match="could not be loaded"):
        engine.arm()
    assert not engine.armed
    engine.update(SentryConfig(), 0)
    assert engine.config_error is None


def test_disarm_cancels_running_tts_discards_queue_and_releases_audio(sentry):
    sentry.config.test_mode = False
    started = threading.Event()

    def speak(*args, stop_event, **kwargs):
        started.set()
        assert stop_event.wait(2)
        raise HardwareError("Cancelled")

    with patch("sentry_node.sentry.engine.speak", side_effect=speak):
        sentry.arm()
        sentry.jobs.put(Job("first", TTSAction(text="hello"), time.monotonic()))
        assert started.wait(2)
        sentry.jobs.put(Job("pending", TTSAction(text="must not run"), time.monotonic()))
        sentry.disarm()
    assert not sentry.thread.is_alive()
    assert not sentry.audio_lock.locked()
    assert sentry.jobs.empty()
    assert len(events(sentry, "cancelled")) == 2


def test_sensor_fault_cancels_actions_and_disarms(sentry):
    sentry.arm()
    sentry.fault("camera disconnected")
    sentry.thread.join(2)
    assert not sentry.armed
    assert sentry.cancelled.is_set()
    assert sentry.status()["error"] == "camera disconnected"
    sentry.video.set_sentry.assert_called_with(False)


def test_expired_jobs_do_not_speak_and_backlog_is_bounded(sentry):
    sentry.config.test_mode = False
    with patch("sentry_node.sentry.engine.speak") as speech:
        sentry.arm()
        sentry.jobs.put(Job("old", TTSAction(text="old"), time.monotonic() - 100))
        deadline = time.monotonic() + 2
        while not events(sentry, "expired") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert events(sentry, "expired")
        speech.assert_not_called()
    assert sentry.jobs.maxsize == 16


def test_ssh_uses_saved_command_no_shell_and_reports_failure(sentry):
    sentry.cancelled.clear()
    process = MagicMock()
    process.poll.return_value = 1
    process.returncode = 1
    command = SSHCommand(host="test-host", user="operator", command='echo "$HOME"; false')
    with patch("sentry_node.sentry.engine.subprocess.Popen", return_value=process) as popen:
        with pytest.raises(HardwareError, match="SSH exited"):
            sentry._ssh(command)
    args = popen.call_args.args[0]
    assert args[-3:] == ["--", "operator@test-host", 'echo "$HOME"; false']
    assert "StrictHostKeyChecking=yes" in args and "BatchMode=yes" in args
    assert not popen.call_args.kwargs.get("shell", False)
    popen.assert_called_once()


def test_ssh_timeout_reaps_local_client_without_retry(sentry):
    sentry.cancelled.clear()
    real_popen = subprocess.Popen
    children = []

    def sleeping_client(args, **kwargs):
        child = real_popen([sys.executable, "-c", "import time;time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    with patch("sentry_node.sentry.engine.subprocess.Popen", side_effect=sleeping_client):
        with pytest.raises(HardwareError, match="timed out"):
            sentry._ssh(SSHCommand(host="unused-host", command="unused", timeout_seconds=1))
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.parametrize(
    "change",
    [
        {"object": "unknown"},
        {"min_confidence": float("nan")},
        {"region": [0.8, 0, 0.2, 1]},
        {"consecutive_detections": 0},
    ],
)
def test_invalid_rule_criteria_rejected(change):
    with pytest.raises(ValueError):
        Rule.model_validate(
            {
                "name": "test",
                "object": "person",
                "actions": [{"type": "tts", "text": "hello"}],
                **change,
            }
        )


def test_unknown_ssh_reference_and_option_like_host_rejected():
    with pytest.raises(ValueError):
        SentryConfig(
            rules=[Rule(name="test", object="person", actions=[SSHAction(command_id="missing")])]
        )
    with pytest.raises(ValueError):
        SSHCommand(host="-oProxyCommand=bad", command="test")


def test_confirmed_appearance_executes_one_announcement_with_effects(sentry):
    from sentry_node.audio.effects import VoiceEffects

    sentry.config.test_mode = False
    sentry.config.rules[0].actions[0].effects = VoiceEffects(preset="demon", volume=40)
    spoken = threading.Event()
    with patch(
        "sentry_node.sentry.engine.speak", side_effect=lambda *a, **k: spoken.set()
    ) as speech:
        sentry.arm()
        for _ in range(3):
            sentry.observe([PERSON], time.monotonic())
        assert spoken.wait(2)
        for _ in range(10):
            sentry.observe([PERSON], time.monotonic())
        sentry.disarm()
    speech.assert_called_once_with(
        sentry.settings,
        "Hello. Please wait here.",
        "en",
        175,
        effects=VoiceEffects(preset="demon", volume=40),
        stop_event=sentry.cancelled,
    )
    assert not sentry.audio_lock.locked()


def test_duplicate_action_type_cannot_be_silently_lost_by_editor():
    with pytest.raises(ValueError, match="one action of each type"):
        Rule(name="test", object="person", actions=[TTSAction(text="one"), TTSAction(text="two")])


TOKEN = "123456:" + "a" * 35  # Deliberately fake; tests never contact Telegram.


def test_telegram_token_private_persisted_preserved_replaced_and_removed(sentry):
    config = SentryConfig(telegram=TelegramConfig(bot_token=TOKEN, chat_id="-100123"))
    result = sentry.update(config, 0)
    assert result["telegram_token_configured"]
    assert result["config"]["telegram"]["bot_token"] == ""
    assert TOKEN not in json.dumps(result)
    assert TOKEN not in repr(config)
    assert TOKEN not in config.model_dump_json()
    assert TOKEN in sentry.settings.sentry_state_file.read_text()
    assert sentry.settings.sentry_state_file.stat().st_mode & 0o077 == 0
    result = sentry.update(SentryConfig.model_validate(result["config"]), 1)
    assert sentry.config.telegram.bot_token.get_secret_value() == TOKEN
    restarted = Sentry(sentry.settings, sentry.video, threading.Lock())
    assert restarted.config.telegram.bot_token.get_secret_value() == TOKEN
    assert not restarted.armed
    config.telegram.bot_token = TelegramConfig(bot_token=TOKEN + "b").bot_token
    sentry.update(config, 2)
    assert sentry.config.telegram.bot_token.get_secret_value() == TOKEN + "b"
    sentry.update(SentryConfig.model_validate(result["config"]), 3, clear_telegram_token=True)
    assert not sentry.configuration()["telegram_token_configured"]
    assert TOKEN not in sentry.settings.sentry_state_file.read_text()


def test_telegram_dry_run_needs_no_credentials_and_never_sends(sentry):
    sentry.config.rules[0].actions = [TelegramAction(text="Person at entrance")]
    direct_arm(sentry)
    with patch.object(sentry, "_telegram") as send:
        for timestamp in [100, 100.5, 101, 101.5]:
            sample(sentry, timestamp)
    send.assert_not_called()
    assert sentry.jobs.empty()
    assert [e["message"] for e in events(sentry, "would_run")] == ["Telegram: Person at entrance"]


def test_telegram_live_arm_requires_credentials_before_camera_start(sentry):
    sentry.config.test_mode = False
    sentry.config.rules[0].actions = [TelegramAction(text="hello")]
    with pytest.raises(ValueError, match="bot token and chat ID"):
        sentry.arm()
    sentry.video.set_sentry.assert_not_called()


def test_confirmed_appearance_sends_telegram_once_without_audio_lock(sentry):
    sentry.config.test_mode = False
    sentry.config.telegram = TelegramConfig(bot_token=TOKEN, chat_id="123")
    action = TelegramAction(text="A person arrived", silent=True)
    sentry.config.rules[0].actions = [action]
    sent = threading.Event()
    sentry.audio_lock.acquire()
    try:
        with patch.object(sentry, "_telegram", side_effect=lambda a: sent.set()) as send:
            sentry.arm()
            for _ in range(6):
                sentry.observe([PERSON], time.monotonic())
            assert sent.wait(2)
            sentry.disarm()
            send.assert_called_once_with(action)
        assert events(sentry, "action_finished")
    finally:
        sentry.audio_lock.release()


def test_telegram_child_token_on_stdin_only(sentry):
    sentry.cancelled.clear()
    sentry.config.telegram = TelegramConfig(bot_token=TOKEN, chat_id="123")
    process = MagicMock()
    process.communicate.return_value = (b"", None)
    process.returncode = 0
    process.poll.return_value = 0
    with patch("sentry_node.sentry.engine.subprocess.Popen", return_value=process) as popen:
        sentry._telegram(TelegramAction(text="Hello", silent=True))
    assert TOKEN not in repr(popen.call_args)
    payload = json.loads(process.communicate.call_args.kwargs["input"])
    assert payload == {
        "token": TOKEN,
        "chat_id": "123",
        "text": "Hello",
        "disable_notification": True,
    }


@pytest.mark.parametrize("cancel", [True, False])
def test_telegram_child_is_reaped_on_disarm_or_absolute_timeout(sentry, cancel):
    sentry.cancelled.clear()
    real_popen = subprocess.Popen
    processes = []

    def sleeping_child(*args, **kwargs):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        return process

    with patch("sentry_node.sentry.engine.subprocess.Popen", side_effect=sleeping_child):
        if cancel:
            timer = threading.Timer(0.2, sentry.cancelled.set)
            timer.start()
            try:
                with pytest.raises(HardwareError, match="cancelled"):
                    sentry._telegram(TelegramAction(text="hello"))
            finally:
                timer.join()
        else:
            with patch("sentry_node.sentry.engine.time.monotonic", side_effect=[0, 6]):
                with pytest.raises(HardwareError, match="timed out"):
                    sentry._telegram(TelegramAction(text="hello"))
    assert processes[0].poll() is not None


@pytest.mark.parametrize("text", [" ", "x" * 4097])
def test_invalid_telegram_message(text):
    with pytest.raises(ValueError):
        TelegramAction(text=text)
