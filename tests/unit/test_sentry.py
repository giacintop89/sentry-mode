import json
import struct
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from sentry_node.config import Settings
from sentry_node.core.errors import HardwareError
from sentry_node.sentry.config import (
    AudioAction,
    PhotoAction,
    Rule,
    SentryConfig,
    SSHAction,
    SSHCommand,
    TelegramAction,
    TelegramConfig,
    TTSAction,
    TuneAction,
    VideoAction,
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
    settings = Settings(
        sentry_state_file=tmp_path / "sentry.json",
        captures_directory=tmp_path / "captures",
        sounds_directory=tmp_path / "sounds",
    )
    engine = Sentry(settings, video, threading.Lock())
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
    sentry.config.test_mode = True
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


def test_rule_test_runs_the_editor_draft_once_without_arming_or_saving(sentry):
    sentry.config.test_mode = False
    draft = Rule(name="Draft", object="person", actions=[TTSAction(text="Testing one two")])
    spoken = threading.Event()
    with patch(
        "sentry_node.sentry.engine.speak", side_effect=lambda *a, **k: spoken.set()
    ) as speech:
        # The notice is short on purpose: the toolbar keeps it to one line.
        assert sentry.test(draft)["message"] == "Running the actions on the node."
        assert spoken.wait(2)
        sentry.thread.join(2)
    speech.assert_called_once()
    assert not sentry.armed and not sentry.audio_lock.locked()
    assert [rule.name for rule in sentry.config.rules] == ["Person at entrance"]
    assert [e["message"] for e in events(sentry, "tested")] == [
        "Test run finished.",
        "Test run of Draft; running its actions.",
    ]
    sentry.video.set_sentry.assert_not_called()


def test_rule_test_runs_for_real_in_test_mode_and_refuses_unrunnable_actions(sentry):
    from sentry_node.sentry.config import SoundAction

    # Test mode holds back detections, not a button the user pressed.
    sentry.config.test_mode = True
    draft = Rule(
        name="Draft",
        object="person",
        actions=[TTSAction(text="Hello"), TuneAction(tune="doorbell")],
    )
    spoken = threading.Event()
    with (
        patch("sentry_node.sentry.engine.speak", side_effect=lambda *a, **k: spoken.set()),
        patch("sentry_node.sentry.engine.play_tune"),
    ):
        assert "Running the actions" in sentry.test(draft)["message"]
        assert spoken.wait(5)
        sentry.thread.join(5)
    assert not events(sentry, "would_run")
    assert [e["message"] for e in events(sentry, "action_started")] == [
        "Playing tune: Doorbell",
        "Playing announcement.",
    ]

    # What a test cannot run, it refuses -- Telegram credentials included, which arming
    # itself lets pass while test mode would only log the message.
    for actions, complaint in [
        ([TelegramAction(text="hi")], "bot token and chat ID"),
        ([SoundAction(sound="door-bell-0123abcd")], "audio file that was deleted"),
        ([SSHAction(command_id="missing")], "Unknown SSH command"),
    ]:
        with pytest.raises(ValueError, match=complaint):
            sentry.test(Rule(name="Draft", object="person", actions=actions))
    sentry.config.rules = [
        Rule(name="Telegram", object="person", actions=[TelegramAction(text="hi")])
    ]
    sentry.arm()
    with pytest.raises(BlockingIOError, match="Disarm"):
        sentry.test(draft)


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
    sentry.config.test_mode = True
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


def test_rule_accepts_one_of_every_action_and_bounds_video_duration():
    actions = [PhotoAction(), VideoAction(duration_seconds=30), TTSAction(text="Hi")]
    assert Rule(name="all", object="person", actions=actions).actions[1].duration_seconds == 30
    for seconds in (0, 61):
        with pytest.raises(ValueError):
            VideoAction(duration_seconds=seconds)
    assert PhotoAction().count == 1
    for invalid in (
        {"count": 0},
        {"count": 21},
        {"interval_seconds": 0.4},
        {"interval_seconds": 61},
    ):
        with pytest.raises(ValueError):
            PhotoAction(**invalid)
    with pytest.raises(ValueError, match="one action of each type"):
        Rule(name="twice", object="person", actions=[PhotoAction(), PhotoAction()])


def test_test_mode_logs_photo_and_video_without_touching_the_camera(sentry):
    sentry.config.test_mode = True
    sentry.config.rules[0].actions = [
        PhotoAction(count=3, interval_seconds=1.5),
        VideoAction(duration_seconds=5),
        AudioAction(duration_seconds=4),
    ]
    direct_arm(sentry)
    for timestamp in (100, 100.5, 101):
        sample(sentry, timestamp)
    assert [e["message"] for e in events(sentry, "would_run")] == [
        "Audio: 4 s",
        "Video: 5 s with sound",
        "Photos: 3 every 1.5 s",
    ]
    sentry.video.latest_frame.assert_not_called()
    sentry.video.add_recording.assert_not_called()


def test_photo_is_saved_and_video_records_beside_the_announcement(sentry):
    import numpy as np

    sentry.config.test_mode = False
    sentry.config.rules[0].actions = [
        PhotoAction(),
        VideoAction(duration_seconds=20),
        TTSAction(text="Hello"),
    ]
    sentry.video.latest_frame.return_value = np.zeros((72, 128, 3), np.uint8)
    sentry.video.recording_size.return_value = (1280, 720)
    recording, spoken = threading.Event(), threading.Event()

    def record(frames, seconds, rule, stop_event, size, microphone):
        recording.set()
        assert (seconds, rule, size) == (20, "Person at entrance", (1280, 720))
        assert microphone == ["-f", "pulse", "-i", "default"]
        stop_event.wait(5)  # Keeps recording until disarm, like a long video.
        return "20260101-000000-000-person-at-entrance.mp4", None

    with (
        patch.object(sentry.captures, "record_video", side_effect=record),
        patch.object(Sentry, "_microphone", return_value=["-f", "pulse", "-i", "default"]),
        patch("sentry_node.sentry.engine.speak", side_effect=lambda *a, **k: spoken.set()),
    ):
        sentry.arm()
        for _ in range(3):
            sentry.observe([PERSON], time.monotonic())
        # The announcement plays while the video is still recording.
        assert recording.wait(2) and spoken.wait(2)
        sentry.disarm()
    assert not any(r.is_alive() for r in sentry.recorders)
    finished = [e["message"] for e in events(sentry, "action_finished")]
    assert any(m.startswith("Photo saved: ") for m in finished)
    assert "Video saved: 20260101-000000-000-person-at-entrance.mp4" in finished
    [photo] = sentry.captures.listing()["captures"]
    assert photo["kind"] == "photo" and photo["rule"] == "person-at-entrance"
    assert [c.args for c in sentry.video.add_recording.call_args_list] == [(1,), (-1,)]


def test_photo_series_is_spaced_in_the_background_and_stops_on_disarm(sentry):
    import numpy as np

    sentry.config.test_mode = False
    sentry.config.rules[0].actions = [
        PhotoAction(count=3, interval_seconds=0.5),
        TTSAction(text="Hello"),
    ]
    sentry.video.latest_frame.return_value = np.zeros((72, 128, 3), np.uint8)
    spoken = threading.Event()
    with patch("sentry_node.sentry.engine.speak", side_effect=lambda *a, **k: spoken.set()):
        sentry.arm()
        started = time.monotonic()
        for _ in range(3):
            sentry.observe([PERSON], time.monotonic())
        assert spoken.wait(2)
        deadline = time.monotonic() + 5
        while len(sentry.captures.listing()["captures"]) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        elapsed = time.monotonic() - started
        sentry.disarm()
    assert len(sentry.captures.listing()["captures"]) == 3 and elapsed >= 1
    assert len([e for e in events(sentry, "action_finished") if "Photo saved" in e["message"]]) == 3

    sentry.config.rules[0].actions = [PhotoAction(count=5, interval_seconds=60)]
    with patch("sentry_node.sentry.engine.speak"):
        sentry.arm()
        for _ in range(3):
            sentry.observe([PERSON], time.monotonic())
        deadline = time.monotonic() + 2
        while len(sentry.captures.listing()["captures"]) < 4 and time.monotonic() < deadline:
            time.sleep(0.05)
        sentry.disarm()
    assert len(sentry.captures.listing()["captures"]) == 4
    assert any("stopped after 1" in e["message"] for e in events(sentry, "cancelled"))


def test_tune_action_validates_logs_in_test_mode_and_plays_on_the_speaker(sentry, tmp_path):
    with pytest.raises(ValueError, match="supported tune"):
        TuneAction(tune="not-a-tune")
    with pytest.raises(ValueError):
        TuneAction(repeat=6)
    sentry.config.test_mode = True
    sentry.config.rules[0].actions = [TuneAction(tune="doorbell", repeat=2)]
    direct_arm(sentry)
    for timestamp in [100, 100.5, 101]:
        sample(sentry, timestamp)
    assert [e["message"] for e in events(sentry, "would_run")] == ["Tune: Doorbell x2"]
    sentry.disarm()

    sentry.config.test_mode = False
    played = threading.Event()
    lengths = []

    def play(self, path, *, timeout, stop_event):
        lengths.append(path.stat().st_size)
        assert sentry.audio_lock.locked() and timeout > 10
        played.set()

    with patch("sentry_node.hardware.speaker.Speaker.play_file", play):
        sentry.arm()
        sentry.jobs.put(Job("tune", TuneAction(tune="chime"), time.monotonic()))
        assert played.wait(2)
        deadline = time.monotonic() + 2
        while not events(sentry, "action_finished") and time.monotonic() < deadline:
            time.sleep(0.01)
        sentry.disarm()
    assert lengths[0] > 44 and not sentry.audio_lock.locked()
    assert events(sentry, "action_started")[0]["message"] == "Playing tune: Chime"


def test_every_tune_renders_and_the_editor_offers_each_one(tmp_path):
    import re
    import wave
    from importlib.resources import files

    from sentry_node.audio.tunes import TUNES, generate_tune, tune_seconds

    for name in TUNES:
        path = tmp_path / f"{name}.wav"
        seconds = generate_tune(path, name, repeat=2, volume=100)
        assert abs(seconds - tune_seconds(name, 2)) < 0.01
        with wave.open(str(path)) as stream:
            assert stream.getnframes() > 0
    page = files("sentry_node").joinpath("sentry.html").read_text()
    offered = re.search(r'<select id="rule-tune">(.*?)</select>', page).group(1)
    assert re.findall(r'value="([a-z]+)"', offered) == list(TUNES)


def test_tune_pitch_shifts_every_note_without_changing_the_tune(sentry, tmp_path):
    import wave

    from sentry_node.audio.tunes import generate_tune

    with pytest.raises(ValueError):
        TuneAction(pitch=25)

    def crossings(pitch):
        path = tmp_path / f"pitch{pitch}.wav"
        seconds = generate_tune(path, "siren", pitch=pitch)
        with wave.open(str(path)) as stream:
            samples = struct.unpack(
                f"<{stream.getnframes()}h", stream.readframes(stream.getnframes())
            )
        changes = sum(1 for a, b in zip(samples, samples[1:], strict=False) if (a < 0) != (b < 0))
        return seconds, changes

    plain_seconds, plain = crossings(0)
    octave_seconds, octave = crossings(12)
    down_seconds, down = crossings(-12)
    # An octave up is twice the frequency and an octave down half it; the tune keeps
    # its own length either way, so a rule's timing does not change with its pitch.
    assert 1.9 < octave / plain < 2.1
    assert 0.45 < down / plain < 0.55
    assert plain_seconds == octave_seconds == down_seconds

    sentry.config.test_mode = True
    sentry.config.rules[0].actions = [TuneAction(tune="doorbell", repeat=2, pitch=-5)]
    direct_arm(sentry)
    for timestamp in [100, 100.5, 101]:
        sample(sentry, timestamp)
    assert [e["message"] for e in events(sentry, "would_run")] == ["Tune: Doorbell x2 at -5 st"]
    sentry.disarm()


def test_sound_action_needs_its_file_and_plays_it_under_the_audio_lock(sentry):
    from sentry_node.sentry.config import SoundAction

    missing = SoundAction(sound="door-bell-0123abcd")
    sentry.config.rules[0].actions = [missing]
    with pytest.raises(ValueError, match="audio file that was deleted"):
        sentry.arm()
    path = sentry.settings.sounds_directory / "door-bell-0123abcd.wav"
    path.parent.mkdir(parents=True)
    from sentry_node.audio.tunes import generate_tune

    generate_tune(path, "chime")
    sentry.config.test_mode = True
    direct_arm(sentry)
    for timestamp in [100, 100.5, 101]:
        sample(sentry, timestamp)
    assert [e["message"] for e in events(sentry, "would_run")] == ["Audio file: door-bell-0123abcd"]
    sentry.disarm()

    sentry.config.test_mode = False
    calls = []

    def play(settings, sound, *, repeat, volume, stop_event):
        calls.append((sound, repeat, volume, sentry.audio_lock.locked()))

    with patch.object(sentry.sounds, "play", side_effect=play):
        sentry.arm()
        sentry.jobs.put(Job("r", SoundAction(sound=missing.sound, repeat=3), time.monotonic()))
        deadline = time.monotonic() + 2
        while not events(sentry, "action_finished") and time.monotonic() < deadline:
            time.sleep(0.01)
        sentry.disarm()
    assert calls == [("door-bell-0123abcd", 3, 80, True)]


def test_audio_action_records_from_the_microphone_in_the_background(sentry):
    sentry.config.test_mode = False
    sentry.config.rules[0].actions = [AudioAction(duration_seconds=6), TTSAction(text="Hello")]
    started, spoken = threading.Event(), threading.Event()

    def record(microphone, seconds, rule, stop_event):
        started.set()
        assert (microphone, seconds, rule) == (
            ["-f", "pulse", "-i", "mic"],
            6,
            "Person at entrance",
        )
        stop_event.wait(5)  # Keeps recording until disarm, like a long message.
        return "20260101-000000-000-person-at-entrance.m4a"

    with (
        patch.object(sentry.captures, "record_audio", side_effect=record),
        patch.object(Sentry, "_microphone", return_value=["-f", "pulse", "-i", "mic"]),
        patch("sentry_node.sentry.engine.speak", side_effect=lambda *a, **k: spoken.set()),
    ):
        sentry.arm()
        for _ in range(3):
            sentry.observe([PERSON], time.monotonic())
        assert started.wait(2) and spoken.wait(2)
        sentry.disarm()
    assert not any(r.is_alive() for r in sentry.recorders)
    finished = [e["message"] for e in events(sentry, "action_finished")]
    assert "Audio saved: 20260101-000000-000-person-at-entrance.m4a" in finished
    sentry.video.add_recording.assert_not_called()


def test_a_silent_video_is_kept_when_the_microphone_is_missing(sentry):
    import numpy as np

    sentry.config.test_mode = False
    sentry.config.rules[0].actions = [VideoAction(duration_seconds=1, audio=True)]
    sentry.video.latest_frame.return_value = np.zeros((72, 128, 3), np.uint8)
    sentry.video.recording_size.return_value = (128, 72)
    saved = threading.Event()

    def record(frames, seconds, rule, stop_event, size, microphone):
        assert microphone is None
        saved.set()
        return "20260101-000000-000-person-at-entrance.mp4", None

    with (
        patch.object(sentry.captures, "record_video", side_effect=record),
        patch.object(Sentry, "_microphone", side_effect=HardwareError("microphone is disabled")),
    ):
        sentry.arm()
        for _ in range(3):
            sentry.observe([PERSON], time.monotonic())
        assert saved.wait(2)
        sentry.disarm()
    finished = [e["message"] for e in events(sentry, "action_finished")]
    assert finished[0] == (
        "Video saved: 20260101-000000-000-person-at-entrance.mp4 (no sound: microphone is disabled)"
    )
