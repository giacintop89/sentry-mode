import json
import threading
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from sentry_node.audio.effects import VoiceEffects
from sentry_node.config import Settings
from sentry_node.core.errors import HardwareError
from sentry_node.core.models import AudioDevice, HardwareStatus
from sentry_node.web import NodeControls, make_handler


@pytest.fixture
def web(tmp_path):
    controls = NodeControls(Settings(sentry_state_file=tmp_path / "sentry.json"))
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
    selected = {"X-Sentry-Node-Control": "1"} if method == "POST" else {}
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
    assert b"Start video" in request(base, "/")[1]
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
            {"X-Sentry-Node-Control": "1", "Origin": "http://127.0.0.1:8092"},
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
    with patch("sentry_node.web.inspect_hardware", return_value=status) as inspect:
        assert request(base, "/api/status")[1] == asdict(status)
        assert request(base, "/api/status")[0] == 200
        inspect.assert_called_once()


def test_device_lists_use_adapters(web):
    _, base = web
    device = AudioDevice("streamcam", "StreamCam", "pulse")
    with (
        patch("sentry_node.web.list_cameras", return_value=["/dev/video2"]),
        patch("sentry_node.web.Microphone.list_devices", return_value=[device]),
        patch("sentry_node.web.Speaker.list_devices", return_value=[]),
    ):
        assert request(base, "/api/camera/list")[1]["devices"] == ["/dev/video2"]
        devices = request(base, "/api/audio/list")[1]
        assert devices["microphones"] == [asdict(device)]
        assert devices["speakers"] == []


def test_camera_test_releases_camera(web):
    _, base = web
    with patch("sentry_node.web.Camera") as adapter:
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

    with patch("sentry_node.web.capture_image", side_effect=capture):
        status, content, headers = request(base, "/api/camera/capture", "POST")
    assert status == 200 and content == b"jpeg-content"
    assert headers.get_content_type() == "image/jpeg"
    assert "sentry-node-frame.jpg" in headers["Content-Disposition"]
    assert not paths[0].exists()


def test_audio_tests_and_errors(web):
    _, base = web
    with (
        patch("sentry_node.web.Microphone.test_input", return_value={"seconds": 2}) as record,
        patch("sentry_node.web.Speaker.test_output") as play,
    ):
        assert request(base, "/api/audio/test-input", "POST")[1]["seconds"] == 2
        assert request(base, "/api/audio/test-output", "POST")[0] == 200
        record.assert_called_once()
        play.assert_called_once()
    with patch("sentry_node.web.Speaker.test_output", side_effect=HardwareError("disconnected")):
        status, result, _ = request(base, "/api/audio/test-output", "POST")
    assert status == 503 and result["error"] == "disconnected"


def test_busy_hardware_returns_conflict(web):
    controls, base = web
    with controls.hardware_lock:
        assert request(base, "/api/camera/test", "POST")[0] == 409


def test_controls_cannot_be_triggered_by_get_or_cross_site_post(web):
    _, base = web
    with patch("sentry_node.web.Speaker.test_output") as play:
        assert request(base, "/api/audio/test-output")[0] == 404
        assert request(base, "/api/audio/test-output", "POST", {})[0] == 403
        assert (
            request(
                base,
                "/api/audio/test-output",
                "POST",
                {"X-Sentry-Node-Control": "1", "Origin": "http://different-host.test"},
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

    with patch("sentry_node.web.run", side_effect=runtime) as run:
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
    with patch("sentry_node.web.run", side_effect=RuntimeError("startup failed")):
        controls.start_runtime()
        controls.thread.join(timeout=2)
    assert controls.runtime_status() == {"running": False, "error": "startup failed"}


def test_speech_uses_speaker_even_while_camera_is_busy(web):
    controls, base = web
    with (
        controls.hardware_lock,
        patch("sentry_node.web.speak", return_value={"message": "played"}) as speech,
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
    with patch("sentry_node.web.speak") as speech:
        assert request(base, "/api/speech", "POST", body=body)[0] == 400
        speech.assert_not_called()


def test_speech_busy_and_provider_failures(web):
    controls, base = web
    with controls.audio_lock:
        assert request(base, "/api/speech", "POST", body={"text": "hello"})[0] == 409
    with patch("sentry_node.web.speak", side_effect=HardwareError("speaker disconnected")):
        assert request(base, "/api/speech", "POST", body={"text": "hello"})[0] == 503
    assert not controls.audio_lock.locked()


def test_shared_video_stream_and_snapshot(web):
    controls, base = web
    with patch("sentry_node.vision.stream.Camera") as adapter:
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
            patch("sentry_node.web.Microphone.is_available", return_value=True),
            patch("sentry_node.web.Speaker.is_available", return_value=True),
            patch("sentry_node.web.network_available", return_value=False),
        ):
            assert request(base, "/api/status")[1]["camera_available"]
        assert not request(base, "/api/video/stop", "POST")[1]["running"]
        assert not controls.hardware_lock.locked()
        adapter.return_value.__enter__.assert_called_once()
        adapter.return_value.__exit__.assert_called_once()


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
        "X-Sentry-Node-Control": "1",
        "X-Sentry-Node-Talk": "session",
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
        headers = {"X-Sentry-Node-Control": "1", "X-Sentry-Node-Talk": "token"}
        assert request(base, "/api/talk/stop", "POST", headers)[0] == 200
        finish.assert_called_once_with("token", cancel=False)


def test_detection_toggle_validates_boolean_and_preserves_camera(web):
    controls, base = web
    with patch("sentry_node.vision.detection.ObjectDetector"):
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
    with patch.object(controls.talk, "start") as talk, patch("sentry_node.web.speak") as speech:
        assert request(base, "/api/talk/start", "POST", body=effects)[0] == 400
        assert (
            request(base, "/api/speech", "POST", body={"text": "test", "effects": effects})[0]
            == 400
        )
        talk.assert_not_called()
        speech.assert_not_called()


def test_sentry_page_config_persistence_and_post_protection(web):
    controls, base = web
    assert b"Sentry mode" in request(base, "/sentry")[1]
    data = request(base, "/api/sentry/config")[1]
    assert data["config"]["test_mode"]
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
