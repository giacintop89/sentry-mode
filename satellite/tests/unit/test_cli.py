"""The four commands, their names frozen, and what they refuse to print."""

import pytest

from sentry_satellite import cli

SECRET = "-----BEGIN PRIVATE KEY-----\nNOT-A-REAL-KEY-JUST-A-MARKER\n-----END PRIVATE KEY-----\n"


@pytest.fixture
def installation(tmp_path):
    """A configuration whose TLS files exist but are not certificates."""
    for name in ("ca.crt", "node.crt", "node.key"):
        (tmp_path / name).write_text(SECRET)
    config = tmp_path / "node.toml"
    config.write_text(
        f"""
[node]
id = "zero-entrance"
profile = "sensor-presence"

[hub]
mqtt_host = "192.168.11.10"

[tls]
ca_file = "{tmp_path / "ca.crt"}"
cert_file = "{tmp_path / "node.crt"}"
key_file = "{tmp_path / "node.key"}"

[[sources]]
id = "pir-1"
kind = "dummy"
"""
    )
    return config


def test_the_command_names_are_the_ones_that_were_agreed():
    parser = cli.build_parser()
    actions = [action for action in parser._actions if action.choices and action.dest == "command"]
    assert set(actions[0].choices) == {"run", "validate", "doctor", "identity"}


def test_validate_accepts_a_node_it_could_run(installation, capsys, tmp_path):
    assert (
        cli.main(
            ["--config", str(installation), "--identity", str(tmp_path / "id.json"), "validate"]
        )
        == 0
    )
    assert "zero-entrance" in capsys.readouterr().out


def test_validate_says_which_driver_does_not_exist_yet(installation, capsys, tmp_path):
    installation.write_text(
        installation.read_text()
        .replace('profile = "sensor-presence"', 'profile = "camera-sensor"')
        .replace('kind = "dummy"', 'kind = "uvc"')
    )
    assert (
        cli.main(
            ["--config", str(installation), "--identity", str(tmp_path / "id.json"), "validate"]
        )
        == 1
    )
    assert "USB camera" in capsys.readouterr().err


def test_validate_refuses_a_file_it_cannot_use(tmp_path, capsys):
    broken = tmp_path / "node.toml"
    broken.write_text("[node]\n")
    assert cli.main(["--config", str(broken), "validate"]) == 2
    assert "refused" in capsys.readouterr().err


def test_doctor_reports_without_contacting_anything(installation, capsys, tmp_path):
    code = cli.main(
        ["--config", str(installation), "--identity", str(tmp_path / "id.json"), "doctor"]
    )
    printed = capsys.readouterr().out
    assert code == 1  # there is no identity yet, and these certificates are not real
    assert "python" in printed
    assert "clock" in printed


def test_doctor_says_whether_the_adapter_a_scanner_wants_is_there(installation, capsys, tmp_path):
    installation.write_text(
        installation.read_text().replace(
            'kind = "dummy"', 'kind = "ble"\naddress = "AA:BB:CC:DD:EE:FF"'
        )
    )
    cli.main(["--config", str(installation), "--identity", str(tmp_path / "id.json"), "doctor"])
    printed = [line for line in capsys.readouterr().out.splitlines() if " hci0 " in line]
    assert len(printed) == 1
    # Either answer is the truth about the machine running the tests; a guess is not.
    assert "present" in printed[0] or "not found" in printed[0] or "blocked" in printed[0]


def test_nothing_ever_prints_the_key_itself(installation, capsys, tmp_path):
    cli.main(["--config", str(installation), "--identity", str(tmp_path / "id.json"), "doctor"])
    cli.main(["--config", str(installation), "--identity", str(tmp_path / "id.json"), "validate"])
    captured = capsys.readouterr()
    assert "NOT-A-REAL-KEY-JUST-A-MARKER" not in captured.out + captured.err
    assert "BEGIN PRIVATE KEY" not in captured.out + captured.err
    printed = captured.out + captured.err
    # Naming the file is the whole value of the message; reading it out loud is the risk.
    assert any(name in printed for name in ("ca.crt", "node.crt", "node.key"))


def test_a_node_can_be_provisioned_once_from_the_command_line(tmp_path, capsys):
    path = tmp_path / "identity.json"
    assert cli.main(["--identity", str(path), "identity", "--create", "zero-entrance"]) == 0
    assert "provisioned zero-entrance" in capsys.readouterr().out
    assert cli.main(["--identity", str(path), "identity"]) == 0
    assert cli.main(["--identity", str(path), "identity", "--create", "zero-garage"]) == 2


def test_running_a_node_that_is_not_this_node_is_refused(installation, tmp_path, capsys):
    path = tmp_path / "identity.json"
    cli.main(["--identity", str(path), "identity", "--create", "zero-garage"])
    assert cli.main(["--config", str(installation), "--identity", str(path), "run"]) == 2
    assert "provisioned as zero-garage" in capsys.readouterr().err


def test_doctor_says_whether_each_bus_is_there(tmp_path, monkeypatch):
    from sentry_satellite import config as configuration

    node = tmp_path / "node.toml"
    node.write_text(
        '[node]\nid = "zero-entrance"\nprofile = "sensor-presence"\n'
        '[hub]\nmqtt_host = "192.168.11.10"\n'
        '[tls]\nca_file = "a"\ncert_file = "b"\nkey_file = "c"\n'
        '[[sources]]\nid = "t"\nkind = "bme280"\nbus = 7\nmeasure = "temperature"\n'
        '[[sources]]\nid = "probe"\nkind = "onewire"\ndevice = "28-0123456789ab"\n'
        '[[sources]]\nid = "off"\nkind = "adc"\nbus = 9\nchannel = 0\nenabled = false\n'
    )
    monkeypatch.setattr(cli, "DEVICES", tmp_path)
    (tmp_path / "28-0123456789ab").mkdir()
    loaded = configuration.load(node, identity_file=tmp_path / "missing.json")
    found = dict(cli._buses(loaded))
    assert found["1-wire 28-0123456789ab"].endswith("ready")
    assert found["i2c-7"].startswith("/dev/i2c-7 missing")
    assert "i2c-9" not in found and "libgpiod" not in found


def camera_config(tmp_path):
    from sentry_satellite import config as configuration

    node = tmp_path / "node.toml"
    node.write_text(
        '[node]\nid = "zero-gate"\nprofile = "camera-sensor"\n'
        '[hub]\nmqtt_host = "192.168.11.10"\n'
        '[tls]\nca_file = "a"\ncert_file = "b"\nkey_file = "c"\n'
        '[[sources]]\nid = "camera-1"\nkind = "csi"\n'
    )
    return configuration.load(node, identity_file=tmp_path / "missing.json")


def test_doctor_asks_the_encoder_which_camera_is_attached(tmp_path, monkeypatch):
    import subprocess

    listing = (
        "Available cameras\n-----------------\n"
        "0 : imx219 [3280x2464 10-bit RGGB] (/base/soc/i2c0mux/i2c@1/imx219@10)\n"
        "    Modes: 'SRGGB10_CSI2P' : 640x480 [200.16 fps - (1000, 752)/1280x960 crop]\n"
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=listing, stderr=""),
    )
    assert cli._cameras(camera_config(tmp_path)) == [
        ("camera", "0 : imx219 [3280x2464 10-bit RGGB]")
    ]


def test_doctor_says_when_no_camera_answers(tmp_path, monkeypatch):
    import subprocess

    config = camera_config(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert "rpicam-vid missing" in cli._cameras(config)[0][1]
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="No cameras available!\n"),
    )
    assert "no camera found" in cli._cameras(config)[0][1]
