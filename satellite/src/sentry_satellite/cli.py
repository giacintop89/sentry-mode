"""The four things a person does to a satellite: check it, look at it, name it, run it."""

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from sentry_satellite import __version__, drivers, health, identity, remote
from sentry_satellite import config as configuration
from sentry_satellite.agent import Agent
from sentry_satellite.mqtt import MqttTransport, TransportError, media_connector, tls_context
from sentry_satellite.sensors.onewire import DEVICES

log = logging.getLogger("sentry_satellite")

DEFAULT_CONFIG = Path("/etc/sentry-satellite/node.toml")
DEFAULT_IDENTITY = Path("/etc/sentry-satellite/identity.json")
DEFAULT_STATE = Path("/var/lib/sentry-satellite")


def _load(path: Path, identity_file: Path) -> configuration.Config:
    return configuration.load(path, identity_file=identity_file)


def validate(arguments) -> int:
    """Read the configuration and say, in full, whether it could be run."""
    try:
        config = _load(arguments.config, arguments.identity)
    except configuration.ConfigError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    problems = []
    for source in config.sources:
        try:
            drivers.build_one(source)
        except drivers.UnsupportedSource as error:
            problems.append(str(error))
    print(json.dumps(configuration.summary(config), indent=2))
    for problem in problems:
        print(f"not yet: {problem}", file=sys.stderr)
    overlay = remote.Overlay(arguments.state_dir / "sources.json")
    applied = overlay.load(config, kinds=drivers.SUPPORTED)
    if applied.revision:
        print(
            f"note: the hub has replaced the sources (revision {applied.revision}):"
            f" {', '.join(configuration.summary(applied.config)['sources']) or 'none'}",
            file=sys.stderr,
        )
    return 1 if problems else 0


def doctor(arguments) -> int:
    """Report what this board can and cannot do, and refuse to guess.

    Everything that would stop `run` is a failure; everything that merely limits what the
    node is good for is a note. Nothing here contacts the hub: a doctor that needs the
    network cannot tell you why the network is not working.
    """
    findings: list[tuple[str, str, bool]] = []

    def note(name: str, value: str, blocking: bool = False) -> None:
        findings.append((name, value, blocking))

    note("python", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    note("agent", __version__)
    try:
        import paho.mqtt as paho  # noqa: F401

        note("paho-mqtt", getattr(paho, "__version__", "installed"))
    except ImportError:
        note("paho-mqtt", "missing (apt install python3-paho-mqtt)", blocking=True)
    note("clock", health.clock_status())
    board = health.board()
    note("uptime_seconds", str(board["uptime_seconds"]))
    note("temperature_c", str(board["temperature_c"]))
    note("memory_available_kb", str(board["memory_available_kb"]))
    note("throttled", str(board["throttled"]))
    for tool in ("rpicam-vid", "ffmpeg", "arecord", "vcgencmd"):
        note(tool, shutil.which(tool) or "not installed")

    try:
        config = _load(arguments.config, arguments.identity)
    except configuration.ConfigError as error:
        note("configuration", str(error), blocking=True)
        config = None
    if config is not None:
        note("node", f"{config.node_id} ({config.profile})")
        for name, value in _buses(config):
            note(name, value)
        for name, value in _cameras(config):
            note(name, value)
        for name, value in _microphones(config):
            note(name, value)
        try:
            tls_context(config.tls.ca_file, config.tls.cert_file, config.tls.key_file)
            note("tls", "certificate, key and CA are a usable set")
        except TransportError as error:
            note("tls", str(error), blocking=True)
    try:
        note("identity", identity.load(arguments.identity).node_id)
    except identity.IdentityError as error:
        note("identity", str(error), blocking=True)

    width = max(len(name) for name, _, _ in findings)
    blocked = False
    for name, value, blocking in findings:
        mark = "!" if blocking else " "
        blocked = blocked or blocking
        print(f"{mark} {name.ljust(width)}  {value}")
    return 1 if blocked else 0


def _buses(config: configuration.Config) -> list[tuple[str, str]]:
    """Whether each bus the sources use is there and open to this user.

    A missing bus is a note, not a failure: `run` still starts, and that source reports
    itself unavailable, which is the same thing said later.
    """
    wanted: dict[str, str] = {}
    for source in config.sources:
        options = source.options
        if not options.get("enabled", True):
            continue
        if source.kind == "gpio":
            wanted["gpio"] = options["chip"]
        elif source.kind in ("bme280", "adc"):
            wanted[f"i2c-{options['bus']}"] = f"/dev/i2c-{options['bus']}"
        elif source.kind == "onewire":
            wanted[f"1-wire {options['device']}"] = str(DEVICES / options["device"])
    found = []
    if "gpio" in wanted:
        try:
            import gpiod  # noqa: F401

            found.append(("libgpiod", getattr(gpiod, "__version__", "installed")))
        except ImportError:
            found.append(("libgpiod", "missing (apt install python3-libgpiod)"))
    for name, path in wanted.items():
        if name.startswith("1-wire"):
            usable = os.path.exists(path)
            hint = "not found (dtoverlay=w1-gpio, and is the probe wired?)"
        else:
            usable = os.access(path, os.R_OK | os.W_OK)
            if name == "gpio":
                hint = "not usable (is this user in the gpio group?)"
            else:
                hint = "not usable (dtparam=i2c_arm=on, and the i2c group)"
            if not os.path.exists(path):
                hint = "missing" + hint.removeprefix("not usable")
        found.append((name, f"{path} " + ("ready" if usable else hint)))
    return found


def _microphones(config: configuration.Config) -> list[tuple[str, str]]:
    """Whether ALSA has a capture device at all, asked of arecord itself."""
    if not any(s.kind == "microphone" and s.enabled for s in config.sources):
        return []
    binary = shutil.which("arecord")
    if binary is None:
        return [("microphone", "arecord missing (apt install alsa-utils)")]
    try:
        listed = subprocess.run(
            [binary, "--list-devices"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as error:
        return [("microphone", f"could not be listed: {error}")]
    cards = [line for line in listed.stdout.splitlines() if line.startswith("card ")]
    if not cards:
        return [("microphone", "no capture device (is the overlay set, and the audio group?)")]
    return [("microphone", line.partition(": ")[2] or line) for line in cards]


def _cameras(config: configuration.Config) -> list[tuple[str, str]]:
    """Whether the camera port has a sensor on it, asked of the encoder itself."""
    if not any(s.kind == "csi" and s.enabled for s in config.sources):
        return []
    binary = shutil.which("rpicam-vid")
    if binary is None:
        return [("camera", "rpicam-vid missing (apt install rpicam-apps-core)")]
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary, fixed argument
            [binary, "--list-cameras"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return [("camera", f"could not be listed: {error}")]
    sensors = [
        line.strip() for line in result.stdout.splitlines() if line[:1].isdigit() and " : " in line
    ]
    if not sensors:
        return [("camera", "no camera found (is the ribbon seated, camera_auto_detect=1?)")]
    return [("camera", sensor.split(" (")[0]) for sensor in sensors]


def show_identity(arguments) -> int:
    if arguments.create:
        try:
            issued = identity.create(
                arguments.identity, arguments.create, datetime.now(UTC).isoformat()
            )
        except identity.IdentityError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
        print(f"provisioned {issued.node_id} in {arguments.identity}")
        print("the hub still has to approve it before this node may publish anything")
        return 0
    try:
        known = identity.load(arguments.identity)
    except identity.IdentityError as error:
        print(f"{error}", file=sys.stderr)
        return 2
    print(known.summary)
    return 0


def run(arguments) -> int:
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        installed = _load(arguments.config, arguments.identity)
        known = identity.load(arguments.identity)
        overlay = remote.Overlay(arguments.state_dir / "sources.json")
        applied = overlay.load(installed, kinds=drivers.SUPPORTED)
        config = applied.config
        builder = drivers.Builder()
        built = builder(config)
    except (configuration.ConfigError, identity.IdentityError, drivers.UnsupportedSource) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    if known.node_id != config.node_id:
        print(
            f"refused: this node is provisioned as {known.node_id},"
            f" but the configuration says {config.node_id}",
            file=sys.stderr,
        )
        return 2

    agent = Agent(
        config=config,
        identity=known,
        transport=MqttTransport(config),
        drivers=built,
        builder=builder,
        overlay=overlay,
        revision=applied.revision,
        connect_media=media_connector(config),
    )

    def asked_to_stop(signum, frame) -> None:
        log.info("signal %s: stopping", signal.Signals(signum).name)
        agent.request_stop()

    signal.signal(signal.SIGTERM, asked_to_stop)
    signal.signal(signal.SIGINT, asked_to_stop)
    agent.run_forever()
    return 0 if agent.stopped_cleanly else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentry-satellite", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--identity", type=Path, default=DEFAULT_IDENTITY)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE,
        help="where sources configured from the hub are kept",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("validate", help="read the configuration and report what it would do")
    commands.add_parser("doctor", help="report what this board can do, without contacting the hub")
    named = commands.add_parser("identity", help="show the node identity, or issue one")
    named.add_argument("--create", metavar="NODE_ID", help="provision this node, once")
    runner = commands.add_parser("run", help="read the sensors and report to the hub")
    runner.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    return {
        "validate": validate,
        "doctor": doctor,
        "identity": show_identity,
        "run": run,
    }[arguments.command](arguments)


if __name__ == "__main__":
    raise SystemExit(main())
