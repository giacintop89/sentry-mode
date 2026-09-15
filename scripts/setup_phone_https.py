#!/usr/bin/env python3
"""Create a local CA and a server certificate; never install trust on any device.

The authority is named after the host, not after this application: the same
certificate fronts every service on the Pi that a phone must reach over TLS.
"""

import argparse
import ipaddress
import re
import shlex
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("addresses", nargs="+", help="Pi LAN IP and optional DNS names")
    args = parser.parse_args()
    names = []
    for address in dict.fromkeys([*args.addresses, "localhost", "127.0.0.1"]):
        try:
            names.append("IP:" + str(ipaddress.ip_address(address)))
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", address):
                parser.error("Use plain IP addresses or DNS names without a URL or port.")
            names.append("DNS:" + address)
    root = Path(__file__).resolve().parents[1]
    folder = root / ".local" / "tls"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)

    def run(*command):
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    ca, key = folder / "ca.crt", folder / "ca.key"
    if ca.exists() != key.exists():
        parser.error(
            "Local CA is incomplete. Restore its matching certificate/key before continuing."
        )
    if not ca.exists():
        run(
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "3650",
            "-subj",
            "/CN=pi5 Local CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            str(key),
            "-out",
            str(ca),
        )
        key.chmod(0o600)
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        request, extensions = Path(tmp) / "server.csr", Path(tmp) / "extensions.cnf"
        extensions.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\nsubjectAltName=" + ",".join(names) + "\n"
        )
        run(
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=pi5",
            "-keyout",
            str(folder / "server.key"),
            "-out",
            str(request),
        )
        (folder / "server.key").chmod(0o600)
        run(
            "openssl",
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca),
            "-CAkey",
            str(key),
            "-CAcreateserial",
            "-days",
            "365",
            "-sha256",
            "-extfile",
            str(extensions),
            "-out",
            str(folder / "server.crt"),
        )
    run("openssl", "verify", "-CAfile", str(ca), str(folder / "server.crt"))
    print("Local certificate created. Start the app from the repository with:")
    print(
        shlex.join(
            [
                ".venv/bin/sentry-mode",
                "serve",
                "--port",
                "8083",
                "--https-port",
                "8443",
                "--tls-cert",
                str(folder / "server.crt"),
                "--tls-key",
                str(folder / "server.key"),
                "--tls-ca",
                str(ca),
            ]
        )
    )
    print("Open http://" + args.addresses[0] + ":8083 and follow “Set up this phone”.")


if __name__ == "__main__":
    main()
