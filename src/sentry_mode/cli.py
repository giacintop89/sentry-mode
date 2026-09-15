import argparse
import json
import logging
from pathlib import Path

from pydantic import ValidationError
from yaml import YAMLError

from sentry_mode.app import run
from sentry_mode.config import load_config
from sentry_mode.core.errors import HardwareError
from sentry_mode.hardware.camera import Camera, list_cameras
from sentry_mode.hardware.microphone import Microphone
from sentry_mode.hardware.speaker import Speaker
from sentry_mode.hardware.status import inspect_hardware
from sentry_mode.logging_config import configure_logging
from sentry_mode.vision.capture import capture_image


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Headless Sentry Mode hardware CLI")
    root.add_argument("--config", type=Path, help="YAML configuration (or SENTRY_MODE_CONFIG)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Inspect hardware and local network")
    commands.add_parser("run", help="Run until SIGINT/SIGTERM")
    web = commands.add_parser("serve", help="Host the hardware control dashboard")
    web.add_argument("--host", default="0.0.0.0")
    web.add_argument("--port", type=int, default=8083)
    web.add_argument(
        "--https-port", type=int, help="Additional HTTPS listener for phone microphone"
    )
    web.add_argument("--https-host", help="Bind address for HTTPS; defaults to --host")
    web.add_argument("--tls-cert", type=Path)
    web.add_argument("--tls-key", type=Path)
    web.add_argument("--tls-ca", type=Path, help="Public local CA certificate for phone setup")
    config = commands.add_parser("config").add_subparsers(dest="action", required=True)
    config.add_parser("show")
    config.add_parser("validate")
    camera = commands.add_parser("camera").add_subparsers(dest="action", required=True)
    camera.add_parser("list")
    camera.add_parser("test")
    capture = camera.add_parser("capture")
    capture.add_argument("--output", type=Path, required=True)
    audio = commands.add_parser("audio").add_subparsers(dest="action", required=True)
    for name in ("list", "test-output", "test-input"):
        audio.add_parser(name)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        configure_logging(config.logging.level)
        if args.command == "config":
            print(
                config.model_dump_json(indent=2) if args.action == "show" else "Configuration valid"
            )
        elif args.command == "status":
            status = inspect_hardware(config)
            print("Sentry Mode\n-----------")
            for name in ("camera", "microphone", "speaker", "network"):
                print(
                    f"{name.capitalize() + ':':14} "
                    + ("OK" if getattr(status, f"{name}_available") else "unavailable")
                )
        elif args.command == "camera":
            camera = Camera(config.camera)
            if args.action == "list":
                print(
                    "\n".join(list_cameras())
                    or "No Linux camera paths found; configure an index on other platforms."
                )
            elif args.action == "capture":
                capture_image(camera, args.output)
                print(f"Captured {args.output}")
            else:
                with camera:
                    for _ in range(5):
                        camera.capture_frame()
                    print(json.dumps(camera.info()))
        elif args.command == "audio":
            microphone, speaker = Microphone(config.microphone), Speaker(config.speaker)
            if args.action == "list":
                for name, adapter in (("Microphone", microphone), ("Speaker", speaker)):
                    print(f"{name} devices:")
                    devices = adapter.list_devices()
                    for device in devices:
                        print(f"  {device.name}: {device.description} ({device.backend})")
                    if not devices:
                        print("  unavailable")
            elif args.action == "test-output":
                speaker.test_output()
                print("Test tone played; confirm audibility at the speaker.")
            else:
                print(f"Microphone capture successful: {microphone.test_input()}")
        elif args.command == "serve":
            from sentry_mode.web import serve

            serve(
                config,
                args.host,
                args.port,
                https_host=args.https_host,
                https_port=args.https_port,
                tls_cert=args.tls_cert,
                tls_key=args.tls_key,
                tls_ca=args.tls_ca,
            )
        elif args.command == "run":
            run(config)
        return 0
    except (HardwareError, ValidationError, YAMLError, OSError, ValueError, ImportError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
