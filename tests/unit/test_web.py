import json
import re
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml

from sentry_mode.audio.effects import VoiceEffects
from sentry_mode.audio.monitor import MAX_LISTENERS
from sentry_mode.audio.monitor import RATE as AUDIO_RATE
from sentry_mode.config import Settings
from sentry_mode.core.errors import HardwareError
from sentry_mode.core.models import AudioDevice, HardwareStatus
from sentry_mode.hardware.microphone import Microphone
from sentry_mode.sentry.config import SSHCommand
from sentry_mode.web import NodeControls, make_handler, serve


@pytest.fixture
def web(tmp_path):
    controls = NodeControls(
        Settings(
            sentry_state_file=tmp_path / "sentry.json",
            soundboard_file=tmp_path / "soundboard.json",
            captures_directory=tmp_path / "captures",
            sounds_directory=tmp_path / "sounds",
        )
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(controls))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield controls, base
    finally:
        controls.sentry.disarm()
        controls.video.close()
        server.shutdown()
        server.server_close()
        thread.join()
        controls.stop_runtime()


def request(base, path, method="GET", headers=None, body=None):
    selected = {"X-Sentry-Mode-Control": "1"} if method == "POST" else {}
    if headers is not None:
        selected = headers
    if body is not None:
        selected["Content-Type"] = "application/json"
    req = Request(
        base + path,
        method=method,
        headers=selected,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        response = urlopen(req, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        data = response.read()
        if response.headers.get_content_type() == "application/json":
            data = json.loads(data)
        return response.status, data, response.headers


def test_page_and_config_controls(web):
    controls, base = web
    assert b'<button id="start-video">Start</button>' in request(base, "/")[1]
    assert b"Capture frame" in request(base, "/tests")[1]
    assert b"Start runtime" in request(base, "/tests/")[1]
    assert request(base, "/api/config")[1] == controls.config.model_dump(mode="json")
    assert request(base, "/api/config/validate", "POST")[1]["message"] == "Configuration valid"


def test_only_configured_dashboard_origins_can_embed(web):
    controls, base = web
    assert request(base, "/")[2]["Content-Security-Policy"] == "frame-ancestors 'none'"
    controls.config.web_frame_origins = ["http://127.0.0.1:8092"]
    assert request(base, "/")[2]["Content-Security-Policy"] == (
        "frame-ancestors 'self' http://127.0.0.1:8092"
    )
    # Embedding does not grant the parent direct access to mutation endpoints.
    assert (
        request(
            base,
            "/api/sentry/stop",
            "POST",
            {"X-Sentry-Mode-Control": "1", "Origin": "http://127.0.0.1:8092"},
        )[0]
        == 403
    )


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://*.example.com",
        "http://host/path",
        "http://host:0",
        "http://host:99999",
        "http://host\r\nX-Header:value",
    ],
)
def test_invalid_frame_origins_rejected(origin):
    with pytest.raises(ValueError):
        Settings(web_frame_origins=[origin])


def test_status_is_cached(web):
    _, base = web
    status = HardwareStatus(True, False, True, False)
    with patch("sentry_mode.web.inspect_hardware", return_value=status) as inspect:
        assert request(base, "/api/status")[1] == asdict(status)
        assert request(base, "/api/status")[0] == 200
        inspect.assert_called_once()


def test_device_lists_use_adapters(web):
    _, base = web
    device = AudioDevice("streamcam", "StreamCam", "pulse")
    with (
        patch("sentry_mode.web.list_cameras", return_value=["/dev/video2"]),
        patch("sentry_mode.web.Microphone.list_devices", return_value=[device]),
        patch("sentry_mode.web.Speaker.list_devices", return_value=[]),
    ):
        assert request(base, "/api/camera/list")[1]["devices"] == ["/dev/video2"]
        devices = request(base, "/api/audio/list")[1]
        assert devices["microphones"] == [asdict(device)]
        assert devices["speakers"] == []


def test_camera_test_releases_camera(web):
    _, base = web
    with patch("sentry_mode.web.Camera") as adapter:
        camera = adapter.return_value.__enter__.return_value
        camera.info.return_value = {"width": 640, "height": 480, "fps": 30}
        status, result, _ = request(base, "/api/camera/test", "POST")
        assert status == 200 and result["width"] == 640
        assert camera.capture_frame.call_count == 5
        adapter.return_value.__exit__.assert_called_once()


def test_capture_download_cleans_temporary_file(web):
    _, base = web
    paths = []

    def capture(camera, output):
        paths.append(output)
        output.write_bytes(b"jpeg-content")

    with patch("sentry_mode.web.capture_image", side_effect=capture):
        status, content, headers = request(base, "/api/camera/capture", "POST")
    assert status == 200 and content == b"jpeg-content"
    assert headers.get_content_type() == "image/jpeg"
    assert "sentry-mode-frame.jpg" in headers["Content-Disposition"]
    assert not paths[0].exists()


def test_audio_tests_and_errors(web):
    _, base = web
    with (
        patch("sentry_mode.web.Microphone.test_input", return_value={"seconds": 2}) as record,
        patch("sentry_mode.web.Speaker.test_output") as play,
    ):
        assert request(base, "/api/audio/test-input", "POST")[1]["seconds"] == 2
        assert request(base, "/api/audio/test-output", "POST")[0] == 200
        record.assert_called_once()
        play.assert_called_once()
    with patch("sentry_mode.web.Speaker.test_output", side_effect=HardwareError("disconnected")):
        status, result, _ = request(base, "/api/audio/test-output", "POST")
    assert status == 503 and result["error"] == "disconnected"


def test_busy_hardware_returns_conflict(web):
    controls, base = web
    with controls.hardware_lock:
        assert request(base, "/api/camera/test", "POST")[0] == 409


def test_controls_cannot_be_triggered_by_get_or_cross_site_post(web):
    _, base = web
    with patch("sentry_mode.web.Speaker.test_output") as play:
        assert request(base, "/api/audio/test-output")[0] == 404
        assert request(base, "/api/audio/test-output", "POST", {})[0] == 403
        assert (
            request(
                base,
                "/api/audio/test-output",
                "POST",
                {"X-Sentry-Mode-Control": "1", "Origin": "http://different-host.test"},
            )[0]
            == 403
        )
        play.assert_not_called()
    assert request(base, "/api/not-a-command", "POST")[0] == 404


def test_runtime_start_stop_and_restart(web):
    controls, base = web
    started = threading.Event()

    def runtime(config, stopped, lock):
        started.set()
        stopped.wait(5)

    with patch("sentry_mode.web.run", side_effect=runtime) as run:
        assert request(base, "/api/runtime")[1]["running"] is False
        assert request(base, "/api/runtime/start", "POST")[1]["running"] is True
        assert started.wait(2)
        assert request(base, "/api/runtime/start", "POST")[1]["running"] is True
        assert run.call_count == 1
        assert request(base, "/api/runtime/stop", "POST")[1]["running"] is False
        assert request(base, "/api/runtime/start", "POST")[1]["running"] is True
        assert request(base, "/api/runtime/stop", "POST")[1]["running"] is False
        assert run.call_count == 2
    assert controls.thread is not None and not controls.thread.is_alive()


def test_runtime_failures_are_reported():
    controls = NodeControls(Settings())
    with patch("sentry_mode.web.run", side_effect=RuntimeError("startup failed")):
        controls.start_runtime()
        controls.thread.join(timeout=2)
    assert controls.runtime_status() == {"running": False, "error": "startup failed"}


def test_speech_uses_speaker_even_while_camera_is_busy(web):
    controls, base = web
    with (
        controls.hardware_lock,
        patch("sentry_mode.web.speak", return_value={"message": "played"}) as speech,
    ):
        status, result, _ = request(
            base, "/api/speech", "POST", body={"text": "Hello", "voice": "it", "rate": 160}
        )
    assert status == 200 and result["message"] == "played"
    speech.assert_called_once_with(
        controls.config,
        "Hello",
        "it",
        160,
        stop_event=controls.shutdown_requested,
        effects=VoiceEffects(),
    )


@pytest.mark.parametrize(
    "body",
    [
        {"text": ""},
        {"text": "x" * 1001},
        {"text": "hello", "rate": 2},
        {"text": "hello", "voice": "../../file"},
        {"text": "hello", "command": "ls"},
        [],
    ],
)
def test_invalid_speech_is_rejected_before_synthesis(web, body):
    _, base = web
    with patch("sentry_mode.web.speak") as speech:
        assert request(base, "/api/speech", "POST", body=body)[0] == 400
        speech.assert_not_called()


def test_speech_busy_and_provider_failures(web):
    controls, base = web
    with controls.audio_lock:
        assert request(base, "/api/speech", "POST", body={"text": "hello"})[0] == 409
    with patch("sentry_mode.web.speak", side_effect=HardwareError("speaker disconnected")):
        assert request(base, "/api/speech", "POST", body={"text": "hello"})[0] == 503
    assert not controls.audio_lock.locked()


def test_shared_video_stream_and_snapshot(web):
    controls, base = web
    with patch("sentry_mode.vision.stream.Camera") as adapter:
        adapter.return_value.__enter__.return_value.encode_jpeg.return_value = b"mock-jpeg"
        assert request(base, "/api/video")[0] == 409
        assert request(base, "/api/video/start", "POST")[1]["running"]
        assert request(base, "/api/video/start", "POST")[1]["running"]
        assert controls.hardware_lock.locked()
        with urlopen(base + "/api/video", timeout=5) as response:
            assert response.headers.get_content_type() == "multipart/x-mixed-replace"
            assert response.readline() == b"--frame\r\n"
            assert response.readline() == b"Content-Type: image/jpeg\r\n"
            assert response.readline() == b"Content-Length: 9\r\n"
            assert response.readline() == b"\r\n"
            assert response.read(9) == b"mock-jpeg"
        assert request(base, "/api/camera/capture", "POST")[1] == b"mock-jpeg"
        assert request(base, "/api/camera/test", "POST")[0] == 409
        assert request(base, "/api/runtime/start", "POST")[0] == 409
        with (
            patch("sentry_mode.web.Microphone.is_available", return_value=True),
            patch("sentry_mode.web.Speaker.is_available", return_value=True),
            patch("sentry_mode.web.network_available", return_value=False),
        ):
            assert request(base, "/api/status")[1]["camera_available"]
        assert not request(base, "/api/video/stop", "POST")[1]["running"]
        assert not controls.hardware_lock.locked()
        adapter.return_value.__enter__.assert_called_once()
        adapter.return_value.__exit__.assert_called_once()


def test_live_microphone_streams_to_the_browser(web):
    controls, base = web

    @contextmanager
    def listen(settings):
        assert settings is controls.config
        yield iter([b"pcm-block-one", b"pcm-block-two"])

    with patch.object(controls.monitor, "listen", listen):
        with urlopen(base + "/api/audio/monitor?t=1", timeout=5) as response:
            assert response.headers.get_content_type() == "audio/l16"
            assert response.headers.get_param("rate") == str(AUDIO_RATE)
            assert response.read() == b"pcm-block-onepcm-block-two"
    held = [controls.monitor.listeners.acquire(blocking=False) for _ in range(MAX_LISTENERS)]
    try:
        status, data, _ = request(base, "/api/audio/monitor")
        assert status == 409
        assert "listeners" in data["error"]
    finally:
        for _ in held:
            controls.monitor.listeners.release()
    with patch.object(controls.monitor, "listen", side_effect=HardwareError("no microphone")):
        status, data, _ = request(base, "/api/audio/monitor")
    assert status == 503
    assert data["error"] == "no microphone"


def test_streams_report_live_video_and_audio_for_the_nav_mark(web):
    controls, base = web
    assert request(base, "/api/streams")[1] == {"video": False, "audio": False}

    controls.monitor.listening = 1
    try:
        assert request(base, "/api/streams")[1] == {"video": False, "audio": True}
    finally:
        controls.monitor.listening = 0
    controls.talk.accepting = True
    try:
        assert request(base, "/api/streams")[1] == {"video": False, "audio": True}
    finally:
        controls.talk.accepting = False
    with patch.object(controls.video, "status", return_value={"running": True}):
        assert request(base, "/api/streams")[1] == {"video": True, "audio": False}
    assert b"connection-pulse" in request(base, "/dashboard.css")[1]
    assert b"/api/streams" in request(base, "/dashboard.js")[1]


def test_phone_assets_and_setup(web, tmp_path):
    controls, base = web
    assert b"Hold to talk" in request(base, "/")[1]
    assert request(base, "/talk.js")[2].get_content_type() == "text/javascript"
    assert b"registerProcessor" in request(base, "/pcm-worklet.js")[1]
    assert request(base, "/api/talk/config")[1] == {"https_port": None, "local_ca": False}
    assert request(base, "/local-ca.crt")[0] == 404
    ca = tmp_path / "ca.crt"
    ca.write_bytes(b"public-certificate")
    controls.tls_ca = ca
    assert request(base, "/local-ca.crt")[1] == b"public-certificate"


def test_phone_pcm_endpoint_enforces_limits_and_session(web):
    controls, base = web
    headers = {
        "X-Sentry-Mode-Control": "1",
        "X-Sentry-Mode-Talk": "session",
        "X-Audio-Sequence": "0",
        "Content-Type": "application/octet-stream",
    }

    def send(data, selected=None):
        req = Request(base + "/api/talk/chunk", data=data, headers=selected or headers)
        try:
            with urlopen(req, timeout=3) as response:
                return response.status
        except HTTPError as error:
            error.close()
            return error.code

    with patch.object(controls.talk, "chunk", return_value={"accepted": 0}) as chunk:
        assert send(bytes(9600)) == 200
        chunk.assert_called_once_with("session", 0, bytes(9600))
        assert send(bytes(9602)) == 413
        assert send(bytes(10), {**headers, "Content-Type": "text/plain"}) == 415
        assert send(bytes(10), {**headers, "Origin": "https://another-host"}) == 403
        assert chunk.call_count == 1
    assert send(bytes(10)) == 400


def test_phone_stream_shares_speaker_lock_with_speech(web):
    controls, base = web
    with controls.audio_lock:
        assert request(base, "/api/talk/start", "POST")[0] == 409
    with patch.object(controls.talk, "finish", return_value={"message": "finished"}) as finish:
        headers = {"X-Sentry-Mode-Control": "1", "X-Sentry-Mode-Talk": "token"}
        assert request(base, "/api/talk/stop", "POST", headers)[0] == 200
        finish.assert_called_once_with("token", cancel=False)


def test_detection_toggle_validates_boolean_and_preserves_camera(web):
    controls, base = web
    with patch("sentry_mode.vision.detection.ObjectDetector"):
        status, result, _ = request(base, "/api/video/detection", "POST", body={"enabled": True})
        assert status == 200 and result["detection"]["enabled"]
        assert not result["running"]
        assert not controls.hardware_lock.locked()
        assert request(base, "/api/video/detection", "POST", body={"enabled": False})[0] == 200
    for body in [{"enabled": "false"}, {"enabled": 1}, {}, {"enabled": True, "model": "/tmp"}]:
        assert request(base, "/api/video/detection", "POST", body=body)[0] == 400


@pytest.mark.parametrize(
    "effects", [{"preset": "unknown"}, {"pitch": 13}, {"volume": 101}, {"pitch": float("nan")}]
)
def test_invalid_voice_effects_never_reach_audio(web, effects):
    controls, base = web
    with patch.object(controls.talk, "start") as talk, patch("sentry_mode.web.speak") as speech:
        assert request(base, "/api/talk/start", "POST", body=effects)[0] == 400
        assert (
            request(base, "/api/speech", "POST", body={"text": "test", "effects": effects})[0]
            == 400
        )
        talk.assert_not_called()
        speech.assert_not_called()


def test_sentry_page_config_persistence_and_post_protection(web):
    controls, base = web
    assert b"Sentry rules" in request(base, "/sentry")[1]
    data = request(base, "/api/sentry/config")[1]
    assert data["config"]["test_mode"] is False
    assert not request(base, "/api/sentry/status")[1]["armed"]
    data["config"]["detection_fps"] = 1
    payload = {"config": data["config"], "revision": data["revision"]}
    assert request(base, "/api/sentry/config", "POST", {}, payload)[0] == 403
    assert request(base, "/api/sentry/config", "POST", body=payload)[0] == 200
    assert controls.config.sentry_state_file.is_file()
    assert request(base, "/api/sentry/config", "POST", body=payload)[0] == 409
    assert request(base, "/api/sentry/start")[0] == 404
    assert request(base, "/api/sentry/start", "POST", {})[0] == 403


def test_telegram_config_api_never_returns_token_even_on_validation_error(web):
    controls, base = web
    token = "123456:" + "a" * 35
    data = request(base, "/api/sentry/config")[1]
    data["config"]["telegram"] = {"bot_token": token, "chat_id": "123"}
    payload = {"config": data["config"], "revision": data["revision"]}
    code, result, _ = request(base, "/api/sentry/config", "POST", body=payload)
    assert code == 200
    assert token not in json.dumps(result)
    assert result["telegram_token_configured"]
    assert token not in json.dumps(request(base, "/api/sentry/config")[1])
    payload["revision"] = 1
    payload["config"]["telegram"]["bot_token"] = token + "/invalid"
    code, result, _ = request(base, "/api/sentry/config", "POST", body=payload)
    assert code == 400
    assert token not in json.dumps(result)
    assert controls.sentry.revision == 1


def test_https_bind_address_requires_an_https_port(tmp_path):
    config = Settings(sentry_state_file=tmp_path / "sentry.json")
    with pytest.raises(ValueError, match="require --https-port"):
        serve(config, "127.0.0.1", 0, https_host="0.0.0.0")


def test_soundboard_saves_lists_and_deletes_messages(web, tmp_path):
    controls, base = web
    page = request(base, "/")[1]
    assert b'id="save-message"' in page and b'id="panel-soundboard"' in page
    assert b"soundboard-cards" in request(base, "/soundboard.js")[1]
    # Sentry rules can pick a saved message for their speech action.
    assert b'id="rule-soundboard"' in request(base, "/sentry")[1]
    assert b"loadSoundboard" in request(base, "/sentry.js")[1]
    assert request(base, "/api/soundboard")[1]["messages"] == []
    body = {
        "text": "Intruder alert",
        "voice": "it",
        "rate": 160,
        "effects": {"preset": "demon", "pitch": -7, "volume": 80},
    }
    assert request(base, "/api/soundboard", "POST", {}, body)[0] == 403
    status, saved, _ = request(base, "/api/soundboard", "POST", body=body)
    assert status == 200 and saved["created"]
    # Saving the same message twice keeps one card.
    again = request(base, "/api/soundboard", "POST", body=body)[1]
    assert again == {"saved": saved["saved"], "created": False}
    listing = request(base, "/api/soundboard")[1]["messages"]
    assert listing == [{"id": saved["saved"], **body}]
    # It survives a restart of the dashboard.
    assert NodeControls(controls.config).soundboard.listing()["messages"] == listing
    assert json.loads((tmp_path / "soundboard.json").read_text())["messages"] == listing
    status, result, _ = request(base, "/api/soundboard/delete", "POST", body={"id": saved["saved"]})
    assert status == 200 and result == {"deleted": saved["saved"]}
    assert request(base, "/api/soundboard")[1]["messages"] == []
    assert request(base, "/api/soundboard/delete", "POST", body={"id": saved["saved"]})[0] == 404


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/soundboard", {"text": ""}),
        ("/api/soundboard", {"text": "hi", "voice": "../../file"}),
        ("/api/soundboard", {"text": "hi", "command": "ls"}),
        ("/api/soundboard/play", {"id": "../soundboard"}),
        ("/api/soundboard/delete", {"id": "0123456789abcdef", "extra": 1}),
    ],
)
def test_invalid_soundboard_requests_are_rejected(web, path, body):
    controls, base = web
    with patch("sentry_mode.web.speak") as speech:
        assert request(base, path, "POST", body=body)[0] == 400
        speech.assert_not_called()
    assert not controls.config.soundboard_file.exists()


def test_soundboard_plays_saved_settings_and_shares_the_speaker(web):
    controls, base = web
    body = {"text": "Hello", "voice": "en", "rate": 200, "effects": {"preset": "chipmunk"}}
    message = request(base, "/api/soundboard", "POST", body=body)[1]["saved"]
    with patch("sentry_mode.web.speak", return_value={"message": "played"}) as speech:
        status, result, _ = request(base, "/api/soundboard/play", "POST", body={"id": message})
        assert status == 200 and result["message"] == "played"
        speech.assert_called_once_with(
            controls.config,
            "Hello",
            "en",
            200,
            stop_event=controls.shutdown_requested,
            effects=VoiceEffects(preset="chipmunk"),
        )
        with controls.audio_lock:
            assert request(base, "/api/soundboard/play", "POST", body={"id": message})[0] == 409
        missing = request(base, "/api/soundboard/play", "POST", body={"id": "0" * 16})
        assert missing[0] == 404 and "no longer" in missing[1]["error"]
    assert not controls.audio_lock.locked()


def test_soundboard_limit_and_unreadable_file(tmp_path):
    from sentry_mode.audio.soundboard import MAX_MESSAGES, Soundboard, SoundboardMessage

    board = Soundboard(tmp_path / "board.json")
    for index in range(MAX_MESSAGES):
        board.save(SoundboardMessage(text=f"message {index}"))
    with pytest.raises(BlockingIOError):
        board.save(SoundboardMessage(text="one too many"))
    assert len(Soundboard(tmp_path / "board.json").listing()["messages"]) == MAX_MESSAGES
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    listing = Soundboard(broken).listing()
    assert listing["messages"] == [] and "could not be loaded" in listing["error"]


def test_captures_are_listed_served_in_ranges_and_deleted(web, tmp_path):
    _, base = web
    folder = tmp_path / "captures"
    folder.mkdir()
    video = folder / "20260914-101500-250-front-door.mp4"
    video.write_bytes(bytes(range(100)))
    (folder / "20260914-101400-000-front-door.jpg").write_bytes(b"jpeg")
    (folder / "notes.txt").write_text("ignored")
    listing = request(base, "/api/captures")[1]["captures"]
    assert [(c["kind"], c["rule"]) for c in listing] == [
        ("video", "front-door"),
        ("photo", "front-door"),
    ]
    status, data, headers = request(base, "/captures/" + video.name)
    assert status == 200 and data == bytes(range(100)) and headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Type"] == "video/mp4"
    status, data, headers = request(
        base, "/captures/" + video.name, headers={"Range": "bytes=10-19"}
    )
    assert status == 206 and data == bytes(range(10, 20))
    assert headers["Content-Range"] == "bytes 10-19/100"
    status, data, _ = request(base, "/captures/" + video.name, headers={"Range": "bytes=-5"})
    assert status == 206 and data == bytes(range(95, 100))
    assert request(base, "/captures/" + video.name, headers={"Range": "bytes=200-"})[0] == 416
    for name in ("notes.txt", "..%2Fsentry.json", "../sentry.json"):
        assert request(base, "/captures/" + name)[0] == 404
    assert request(base, "/api/captures/delete", "POST", body={"name": "../sentry.json"})[0] == 404
    remaining = request(base, "/api/captures/delete", "POST", body={"name": video.name})[1]
    assert [c["kind"] for c in remaining["captures"]] == ["photo"] and not video.exists()
    page = request(base, "/sentry")[1]
    # The step menu offers every action the engine runs, plus the wait the sequence needs.
    menu = re.search(rb'<select id="new-step".*?</select>', page).group()
    assert re.findall(rb'value="([a-z]+)"', menu) == [
        b"photo",
        b"audio",
        b"video",
        b"tts",
        b"tune",
        b"sound",
        b"telegram",
        b"ssh",
        b"wait",
    ]
    assert b'id="rule-video-audio" data-f="video-audio" type="checkbox" checked' in page


def upload(base, data, name="Door Bell.wav", content_type="application/octet-stream"):
    req = Request(
        base + "/api/sounds/upload",
        method="POST",
        data=data,
        headers={
            "X-Sentry-Mode-Control": "1",
            "Content-Type": content_type,
            "X-Sound-Name": name,
        },
    )
    try:
        response = urlopen(req, timeout=30)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())


def test_audio_files_upload_convert_play_and_stay_while_rules_use_them(web, tmp_path):
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required to convert audio files")
    from sentry_mode.audio.tunes import generate_tune

    controls, base = web
    assert b'<template id="step-sound">' in request(base, "/sentry")[1]
    assert request(base, "/api/sounds")[1]["sounds"] == []
    source = tmp_path / "chime.wav"
    generate_tune(source, "chime")
    assert upload(base, source.read_bytes(), content_type="text/plain")[0] == 415
    assert upload(base, b"not audio at all")[0] == 400
    status, saved = upload(base, source.read_bytes(), name="Door%20Bell.wav")
    assert status == 200 and saved["saved"].startswith("door-bell-")
    assert [(s["id"], s["name"]) for s in saved["sounds"]] == [(saved["saved"], "door bell")]
    assert abs(saved["sounds"][0]["seconds"] - 1.15) < 0.1
    sound = saved["saved"]

    with patch("sentry_mode.audio.sounds.Speaker") as speaker:
        result = request(base, "/api/sounds/play", "POST", body={"id": sound})[1]
    assert result["seconds"] > 1.1 and speaker.return_value.play_file.call_count == 1
    assert request(base, "/api/sounds/play", "POST", body={"id": "../../etc-00000000"})[0] == 404

    data = request(base, "/api/sentry/config")[1]
    data["config"]["rules"][0]["actions"] = [{"type": "sound", "sound": sound, "repeat": 2}]
    payload = {"config": data["config"], "revision": data["revision"]}
    assert request(base, "/api/sentry/config", "POST", body=payload)[0] == 200
    status, refused, _ = request(base, "/api/sounds/delete", "POST", body={"id": sound})
    assert status == 409 and "Person at entrance" in refused["error"]
    controls.sentry.config.rules[0].actions = [{"type": "tts", "text": "hi"}]
    assert request(base, "/api/sounds/delete", "POST", body={"id": sound})[1]["sounds"] == []


def test_message_test_button_speaks_the_editor_draft_with_its_voice_and_effects(web):
    controls, base = web
    draft = {
        "text": "Hello. Please wait here.",
        "voice": "it",
        "rate": 210,
        "effects": {"preset": "custom", "pitch": -4, "volume": 55},
    }
    with patch("sentry_mode.web.speak", return_value={"message": "played"}) as speech:
        status, data, _ = request(base, "/api/speech", "POST", body=draft)
    assert status == 200 and data["message"] == "played"
    # The rule's own announcement settings are spoken, not the node's defaults.
    speech.assert_called_once_with(
        controls.config,
        "Hello. Please wait here.",
        "it",
        210,
        stop_event=controls.shutdown_requested,
        effects=VoiceEffects(preset="custom", pitch=-4, volume=55),
    )
    with controls.audio_lock:
        assert request(base, "/api/speech", "POST", body=draft)[0] == 409
    assert not controls.audio_lock.locked()
    page = request(base, "/sentry")[1]
    assert b'id="test-message"' in page and b'id="rule-text"' in page


def test_tune_test_button_plays_the_editor_settings_and_shares_the_speaker(web):
    controls, base = web
    played = []

    def play(self, path, *, timeout, stop_event):
        played.append((path.stat().st_size, timeout))
        assert controls.audio_lock.locked()

    with patch("sentry_mode.hardware.speaker.Speaker.play_file", play):
        status, data, _ = request(
            base, "/api/tunes/play", "POST", body={"tune": "doorbell", "repeat": 2, "volume": 40}
        )
        assert status == 200 and data["message"] == "Played Doorbell on the node speaker."
        assert len(played) == 1 and played[0][0] > 44 and not controls.audio_lock.locked()
        # The pitch of the editor's field is played too, and it is bounded like the model.
        assert (
            request(base, "/api/tunes/play", "POST", body={"tune": "chime", "pitch": -5})[0] == 200
        )
        assert len(played) == 2
        for body in (
            {"tune": "nope"},
            {"tune": "chime", "repeat": 9},
            {"tune": "chime", "x": 1},
            {"tune": "chime", "pitch": 25},
        ):
            assert request(base, "/api/tunes/play", "POST", body=body)[0] == 400
        with controls.audio_lock:
            assert request(base, "/api/tunes/play", "POST", body={"tune": "chime"})[0] == 409
    assert len(played) == 2
    page = request(base, "/sentry")[1]
    assert b'id="test-tune"' in page and b'id="rule-tune-pitch"' in page


def test_rule_test_button_runs_the_editor_draft_without_saving_it(web):
    controls, base = web
    spoken = threading.Event()
    draft = {
        "name": "Draft",
        "object": "person",
        "actions": [{"type": "tts", "text": "Testing this rule"}],
    }
    with patch("sentry_mode.sentry.engine.speak", side_effect=lambda *a, **k: spoken.set()):
        status, data, _ = request(base, "/api/sentry/rules/test", "POST", body=draft)
        assert status == 200 and data["message"] == "Running the actions on the node."
        assert spoken.wait(5)
        controls.sentry.thread.join(5)
    assert [rule.name for rule in controls.sentry.config.rules] == ["Person at entrance"]
    assert not controls.sentry.armed
    assert request(base, "/api/sentry/rules/test", "POST", body={"name": "Draft"})[0] == 400
    assert b'id="test-rule"' in request(base, "/sentry")[1]


def test_the_actions_tab_is_an_ordered_list_of_steps_the_editor_can_rearrange(web):
    _, base = web
    page = request(base, "/sentry")[1].decode()
    assert '<ol id="steps">' in page and 'id="add-step"' in page
    # Each step carries its own order controls and the flag that starts it with the one above.
    assert 'class="step-together"' in page
    assert 'data-move="-1"' in page and 'data-move="1"' in page
    assert 'class="danger step-remove"' in page
    assert '<template id="step-wait">' in page and 'data-f="wait-seconds"' in page
    # A step closes into its header, which then says what the step does.
    assert 'class="step-toggle" aria-expanded="true"' in page
    assert 'class="step-summary"' in page
    script = request(base, "/sentry.js")[1].decode()
    # Steps are copies of the templates, so no field id is shared between two steps.
    assert "makeStep(" in script and "$('use-tts')" not in script and "$('rule-text')" not in script
    # A closed step hides fields the browser cannot focus, so an invalid one opens itself.
    assert "openStep(" in script and "'invalid'" in script


def test_editor_confirms_in_the_page_because_a_framed_dashboard_ignores_dialogs(web):
    _, base = web
    script = request(base, "/sentry.js")[1].decode()
    # window.confirm returns false without asking inside a cross-origin frame, which is how
    # the dashboard embeds this app, so a button that acts for real must not depend on it.
    assert re.search(r"\bconfirm\(", script) is None
    assert "armButton(" in script


def test_per_action_test_buttons_run_one_action_through_the_rule_test_endpoint(web):
    controls, base = web
    ran = threading.Event()
    controls.sentry.config.ssh_commands["gate"] = SSHCommand(host="gate.local", command="uptime")
    draft = {
        "name": "Draft",
        "object": "person",
        "actions": [{"type": "ssh", "command_id": "gate"}],
    }
    with patch.object(controls.sentry, "_ssh", side_effect=lambda command: ran.set()) as ssh:
        status, data, _ = request(base, "/api/sentry/rules/test", "POST", body=draft)
        assert status == 200 and data["message"] == "Running the actions on the node."
        assert ran.wait(5)
        controls.sentry.thread.join(5)
    assert ssh.call_args.args[0].host == "gate.local"
    # What the buttons cannot run, they refuse before anything is queued.
    for actions in ([{"type": "ssh", "command_id": "gone"}], [{"type": "telegram", "text": "hi"}]):
        assert (
            request(base, "/api/sentry/rules/test", "POST", body={**draft, "actions": actions})[0]
            == 400
        )
    assert [rule.name for rule in controls.sentry.config.rules] == ["Person at entrance"]
    page = request(base, "/sentry")[1]
    assert b'id="test-telegram"' in page and b'id="test-ssh"' in page


def post_message(base, data, content_type="application/octet-stream"):
    req = Request(
        base + "/api/captures/message",
        method="POST",
        headers={"X-Sentry-Mode-Control": "1", "Content-Type": content_type},
        data=data,
    )
    try:
        response = urlopen(req, timeout=30)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())


def test_recorded_messages_are_converted_and_kept_with_the_captures(web):
    _, base = web
    silence = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
         "-t", "1", "-c:a", "libopus", "-f", "webm", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    status, data = post_message(base, silence)
    assert status == 200 and data["name"].endswith("-phone-message.m4a")
    listing = request(base, "/api/captures")[1]["captures"]
    assert [(c["kind"], c["rule"]) for c in listing] == [("audio", "phone-message")]
    assert request(base, "/captures/" + data["name"])[2]["Content-Type"] == "audio/mp4"
    assert post_message(base, b"not audio")[0] == 400
    assert post_message(base, silence, "application/json")[0] == 415
    page = request(base, "/")[1]
    assert b'id="record-message"' in page and b'id="record-video"' in page


def test_the_video_view_records_to_the_captures_tab(web):
    controls, base = web
    assert request(base, "/api/video/status")[1]["recording"] == {
        "active": False,
        "started": None,
        "max_seconds": 60,
        "result": None,
    }
    # Recording follows the live preview, so it needs video running first.
    assert request(base, "/api/video/record/start", "POST")[0] == 409
    saved = threading.Event()

    def record(frames, seconds, rule, stop_event, size, microphone):
        assert (seconds, rule) == (60, "manual recording")
        stop_event.wait(5)
        saved.set()
        return "20260914-101500-250-manual-recording.mp4", None

    with (
        patch.object(controls.video, "status", return_value={"running": True}),
        patch.object(controls.video, "recording_size", return_value=(1280, 720)),
        patch.object(controls.video, "add_recording") as add_recording,
        patch.object(controls.sentry.captures, "record_video", side_effect=record),
        patch.object(Microphone, "ffmpeg_input", return_value=["-f", "pulse", "-i", "default"]),
    ):
        state = request(base, "/api/video/record/start", "POST")[1]
        assert state["recording"]["active"] and state["recording"]["started"]
        assert request(base, "/api/video/record/start", "POST")[0] == 409
        state = request(base, "/api/video/record/stop", "POST")[1]
    assert saved.is_set() and not state["recording"]["active"]
    assert state["recording"]["result"] == {
        "message": "Saved · 20260914-101500-250-manual-recording.mp4",
        "error": False,
    }
    assert [c.args for c in add_recording.call_args_list] == [(1,), (-1,)]


def test_hardware_page_lists_devices_and_saves_the_choice(web, tmp_path, monkeypatch):
    from sentry_mode.core.models import AudioDevice

    controls, base = web
    page = request(base, "/hardware")[1]
    assert b'id="device-form"' in page and b"Hardware settings" in page
    assert request(base, "/tests")[1] == page
    speaker = AudioDevice("bluez_output.aukey", "Aukey SK-M7", "pipewire")
    microphone = AudioDevice("alsa_input.cam", "StreamCam", "pipewire")
    with (
        patch("sentry_mode.web.list_cameras", return_value=["/dev/video0"]),
        patch("sentry_mode.web.Microphone.list_devices", return_value=[microphone]),
        patch("sentry_mode.web.Speaker.list_devices", return_value=[speaker]),
    ):
        listing = request(base, "/api/hardware")[1]
    assert listing["cameras"] == ["/dev/video0", "0"]
    assert listing["camera"] == "0" and listing["microphone"] == "auto"
    assert listing["speakers"][0]["description"] == "Aukey SK-M7"
    assert listing["saved_to"] is None

    config_file = tmp_path / "config.yaml"
    config_file.write_text("node:\n  name: doorstep\nspeaker:\n  volume: 70\n")
    monkeypatch.setenv("SENTRY_MODE_CONFIG", str(config_file))
    choice = {
        "camera": "/dev/video0",
        "microphone": microphone.name,
        "speaker": speaker.name,
        "speaker_volume": 45,
    }
    status, saved, _ = request(base, "/api/hardware", "POST", body=choice)
    assert status == 200 and saved["saved_to"] == str(config_file)
    written = yaml.safe_load(config_file.read_text())
    assert written["node"] == {"name": "doorstep"}
    assert written["speaker"] == {"volume": 45, "device": speaker.name}
    assert written["camera"] == {"device": "/dev/video0"}
    # The running dashboard uses the new devices without a restart.
    assert controls.config.speaker.volume == 45
    assert controls.config.camera.device == "/dev/video0"
    assert request(base, "/api/hardware", "POST", body={**choice, "speaker_volume": 120})[0] == 400
    with patch.object(controls.video, "status", return_value={"capture_running": True}):
        busy = request(base, "/api/hardware", "POST", body={**choice, "camera": "1"})
    assert busy[0] == 409 and "Stop video" in busy[1]["error"]
