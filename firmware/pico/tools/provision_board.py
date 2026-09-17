#!/usr/bin/env python3
"""Tell a board who it is and which network to join, over the USB cable.

The record is the same JSON `read_provisioning` reads on the board: a node id the hub has
registered, where the broker is, and — for a board that has a radio — the network to join.
Until `PICO-02` writes it to flash, what is sent here lives in RAM and is gone at the next
boot, which is inconvenient and is also the only reason this is safe to run casually.

    firmware/pico/tools/provision_board.py --record .local/pico-provisioning.json --join

The record holds a passphrase and points at a private key, so it belongs somewhere git does
not look: `.local/` is ignored in this repository. Nothing here prints either of them back,
and the board does not either.

    {
      "node_id": "pico-ingresso",
      "mqtt_host": "192.168.11.240",
      "mqtt_port": 8883,
      "wifi_ssid": "casa",
      "wifi_password": "…",
      "ca_file": "/etc/sentry-mode/satellites/ca.crt",
      "cert_file": "/etc/sentry-mode/satellites/pico-ingresso.crt",
      "key_file": ".local/pico-ingresso.key"
    }

The three files are the authority the board checks the broker against and the certificate
and key it answers with, issued by `scripts/satellite_admin.py`. They are sent with
`--connect`, which also tells the board to open the connection and keep it open.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ALLOWED = {"node_id", "mqtt_host", "mqtt_port", "wifi_ssid", "wifi_password"}
CREDENTIALS = {"ca_file": "ca", "cert_file": "cert", "key_file": "key"}
SECRET = "wifi_password"


def read_record(path: Path, ssid: str | None, password_env: str | None) -> dict:
    record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(record, dict):
        raise SystemExit(f"{path} is not one provisioning record")
    if ssid:
        record["wifi_ssid"] = ssid
    if password_env:
        secret = os.environ.get(password_env)
        if secret is None:
            raise SystemExit(f"${password_env} is not set")
        record[SECRET] = secret
    unknown = set(record) - ALLOWED - set(CREDENTIALS)
    if unknown:
        raise SystemExit(f"this firmware reads none of: {', '.join(sorted(unknown))}")
    for required in ("node_id", "mqtt_host", "mqtt_port"):
        if required not in record:
            raise SystemExit(f"a record needs {required}")
    return record


def read_credentials(record: dict, paths: dict[str, Path | None]) -> dict:
    """The three PEM files, read from wherever the record or the arguments point.

    Nothing is checked here beyond it being there and looking like PEM: the board refuses a
    key offered as a certificate, and mbedTLS decides whether any of it is real. What this
    does care about is never printing the key.
    """
    found: dict[str, str] = {}
    for field, name in CREDENTIALS.items():
        where = paths.get(field) or (Path(record[field]) if field in record else None)
        if where is None:
            continue
        text = where.read_text(encoding="utf-8")
        if "-----BEGIN " not in text:
            raise SystemExit(f"{where} does not look like PEM")
        found[name] = text
    missing = set(CREDENTIALS.values()) - set(found)
    if found and missing:
        raise SystemExit(f"a connection needs all three; missing: {', '.join(sorted(missing))}")
    return found


def talk(port, line: str, *, patience: float = 3.0, quiet: bool = False) -> list[str]:
    """Send one line and print what the board says for a moment afterwards."""
    port.write(line.encode() + b"\n")
    port.flush()
    said: list[str] = []
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        raw = port.readline()
        if not raw:
            continue
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        said.append(text)
        if not quiet:
            print(text)
    return said


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--record", type=Path, default=Path(".local/pico-provisioning.json"))
    parser.add_argument("--ssid", help="the network, if it is not in the record")
    parser.add_argument(
        "--password-env",
        metavar="NAME",
        help="an environment variable holding the passphrase, so it stays out of the shell's"
        " history and out of the file",
    )
    parser.add_argument("--join", action="store_true", help="join the network afterwards")
    parser.add_argument("--ca-file", type=Path, help="the authority, if it is not in the record")
    parser.add_argument("--cert-file", type=Path, help="this node's certificate")
    parser.add_argument("--key-file", type=Path, help="this node's private key")
    parser.add_argument(
        "--connect",
        action="store_true",
        help="send the credentials and open the connection to the broker",
    )
    arguments = parser.parse_args(argv)

    try:
        import serial
    except ModuleNotFoundError:
        raise SystemExit("this needs pyserial: pip install pyserial") from None

    record = read_record(arguments.record, arguments.ssid, arguments.password_env)
    credentials = read_credentials(
        record,
        {
            "ca_file": arguments.ca_file,
            "cert_file": arguments.cert_file,
            "key_file": arguments.key_file,
        },
    )
    identity = {key: value for key, value in record.items() if key in ALLOWED}
    shown = {key: ("…" if key == SECRET else value) for key, value in identity.items()}
    print(f"sending {json.dumps(shown, ensure_ascii=False)}")
    if arguments.connect and not credentials:
        raise SystemExit("--connect needs an authority, a certificate and a key")

    with serial.Serial(arguments.port, 115200, timeout=0.5) as port:
        port.reset_input_buffer()
        talk(port, "provision " + json.dumps(identity, separators=(",", ":")), patience=2.0)
        if arguments.join:
            said = talk(port, "join", patience=30.0)
            if not any("joined" in line for line in said):
                print("the board did not say it joined", file=sys.stderr)
                return 1
        if credentials:
            # One line, the key among it. It is never echoed and never logged here.
            print(f"sending {len(credentials)} credentials, none of them printed")
            talk(
                port, "credentials " + json.dumps(credentials, separators=(",", ":")), patience=3.0
            )
        if arguments.connect:
            said = talk(port, "connect", patience=30.0)
            if not any("online" in line for line in said):
                print("the board did not say it was online", file=sys.stderr)
                talk(port, "status", patience=2.0)
                return 1
        talk(port, "status", patience=2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
