"""The four things a person does to a satellite: check it, look at it, name it, run it."""

import argparse
import json
import logging
import shutil
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from sentry_satellite import __version__, drivers, health, identity
from sentry_satellite import config as configuration
from sentry_satellite.agent import Agent
from sentry_satellite.mqtt import MqttTransport, TransportError, tls_context

log = logging.getLogger("sentry_satellite")

DEFAULT_CONFIG = Path("/etc/sentry-satellite/node.toml")
DEFAULT_IDENTITY = Path("/etc/sentry-satellite/identity.json")


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
        config = _load(arguments.config, arguments.identity)
        known = identity.load(arguments.identity)
        built = drivers.build(config)
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

    agent = Agent(config=config, identity=known, transport=MqttTransport(config), drivers=built)

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
