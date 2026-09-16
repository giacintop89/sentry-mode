#!/usr/bin/env python3
"""Issue the certificates and decide which nodes the hub will listen to.

This is an administrative tool, run by a person with a shell, and deliberately not
something the web interface can reach. It writes into `/etc` and signs certificates; an
event arriving over MQTT must never be able to cause any of it.

    python scripts/satellite_admin.py init-ca --directory /etc/sentry-mode/satellites
    python scripts/satellite_admin.py hub-cert --address 192.168.11.10
    python scripts/satellite_admin.py node-key --node zero-entrance --out /tmp/zero
    python scripts/satellite_admin.py sign --node zero-entrance --csr /tmp/zero.csr
    python scripts/satellite_admin.py register --node zero-entrance --certificate zero-entrance.crt
    python scripts/satellite_admin.py approve --node zero-entrance
    python scripts/satellite_admin.py acl --out /etc/mosquitto/sentry-acl

Keys are elliptic curve P-256: a Zero W handles the handshake in a fraction of the time an
RSA key of comparable strength would take, and every client here is one we issue ourselves.
"""

import argparse
import ipaddress
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sentry_mode.satellites.identity import NodeRegistry, fingerprint  # noqa: E402

CA_DAYS = 3650
LEAF_DAYS = 825
DEFAULT_DIRECTORY = Path("/etc/sentry-mode/satellites")
DEFAULT_NODES_FILE = Path(".local/satellite-nodes.json")


class Refused(RuntimeError):
    """Something would have been overwritten, or a tool is missing."""


def openssl(*arguments: str) -> str:
    binary = shutil.which("openssl")
    if binary is None:
        raise Refused("openssl is not installed")
    result = subprocess.run(  # noqa: S603 - fixed binary, arguments built here
        [binary, *arguments], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise Refused(f"openssl {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout


def keep(path: Path, force: bool) -> None:
    """Refuse to replace something that exists, unless told to, and keep a copy if so."""
    if not path.exists():
        return
    if not force:
        raise Refused(f"{path} exists; pass --force to replace it (a backup is kept)")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, backup)
    print(f"kept a copy at {backup}")


def private(path: Path) -> None:
    path.chmod(0o600)


def subject_alt_names(addresses: list[str]) -> str:
    entries = []
    for address in addresses:
        try:
            ipaddress.ip_address(address)
        except ValueError:
            entries.append(f"DNS:{address}")
        else:
            entries.append(f"IP:{address}")
    return ",".join(entries)


def init_ca(arguments) -> int:
    """Create the authority every satellite and the hub will be issued from."""
    directory = arguments.directory
    directory.mkdir(parents=True, exist_ok=True)
    key, certificate = directory / "ca.key", directory / "ca.crt"
    keep(key, arguments.force)
    keep(certificate, arguments.force)
    openssl(
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(certificate),
        "-days",
        str(CA_DAYS),
        "-subj",
        "/CN=Sentry Mode satellites CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE,pathlen:0",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    private(key)
    print(f"wrote {certificate} and {key}")
    print("the CA key never leaves this machine; only ca.crt is copied to a satellite")
    return 0


def _sign(directory: Path, csr: Path, out: Path, extensions: str, force: bool) -> None:
    keep(out, force)
    config = out.with_suffix(".ext")
    config.write_text(extensions, encoding="utf-8")
    try:
        openssl(
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(directory / "ca.crt"),
            "-CAkey",
            str(directory / "ca.key"),
            "-CAcreateserial",
            "-out",
            str(out),
            "-days",
            str(LEAF_DAYS),
            "-extfile",
            str(config),
        )
    finally:
        config.unlink(missing_ok=True)


def hub_cert(arguments) -> int:
    """Issue the hub's certificate, valid for the address the satellites actually use.

    Not for `localhost`: the satellites reach the broker over the network, and a
    certificate that only matches the loopback address leads straight to somebody turning
    verification off to make it work.
    """
    directory = arguments.directory
    if not (directory / "ca.crt").exists():
        raise Refused(f"there is no authority in {directory}; run init-ca first")
    key, csr, certificate = directory / "hub.key", directory / "hub.csr", directory / "hub.crt"
    keep(key, arguments.force)
    openssl(
        "req",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(csr),
        "-subj",
        "/CN=sentry-mode-hub",
    )
    private(key)
    names = subject_alt_names(arguments.address)
    _sign(
        directory,
        csr,
        certificate,
        f"subjectAltName={names}\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth,clientAuth\n",
        arguments.force,
    )
    csr.unlink(missing_ok=True)
    print(f"wrote {certificate} for {names}")
    return 0


def node_key(arguments) -> int:
    """Make a key and a request for a node. Run this on the node when you can."""
    out = arguments.out
    out.parent.mkdir(parents=True, exist_ok=True)
    key, csr = out.with_suffix(".key"), out.with_suffix(".csr")
    keep(key, arguments.force)
    openssl(
        "req",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(csr),
        "-subj",
        f"/CN={arguments.node}",
    )
    private(key)
    print(f"wrote {key} and {csr}")
    print("copy the key to the node and keep it there; only the request needs signing")
    return 0


def sign(arguments) -> int:
    """Sign a node's request. The name in the certificate is the name it may publish as."""
    directory = arguments.directory
    subject = openssl("req", "-in", str(arguments.csr), "-noout", "-subject")
    if f"CN={arguments.node}" not in subject.replace(" = ", "="):
        raise Refused(
            f"that request is for {subject.strip()}, not for {arguments.node}."
            " The certificate name is the node's identity and cannot be changed here."
        )
    certificate = arguments.out or directory / f"{arguments.node}.crt"
    _sign(
        directory,
        arguments.csr,
        certificate,
        f"subjectAltName=DNS:{arguments.node}\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=clientAuth\n",
        arguments.force,
    )
    print(f"wrote {certificate}")
    print(f"fingerprint {fingerprint(certificate.read_text())}")
    print("the node still has to be registered and approved before the hub listens to it")
    return 0


def registry(arguments) -> NodeRegistry:
    return NodeRegistry(arguments.nodes_file)


def register(arguments) -> int:
    record = registry(arguments).register(
        arguments.node,
        display_name=arguments.name,
        profile=arguments.profile,
        zone=arguments.zone,
        certificate=arguments.certificate.read_text() if arguments.certificate else None,
    )
    print(f"{record.node_id} is registered and {record.status}")
    return 0


def approve(arguments) -> int:
    record = registry(arguments).approve(
        arguments.node,
        certificate=arguments.certificate.read_text() if arguments.certificate else None,
    )
    print(f"{record.node_id} may publish, pinned to {record.fingerprint}")
    return 0


def revoke(arguments) -> int:
    record = registry(arguments).revoke(arguments.node, reason=arguments.reason)
    print(f"{record.node_id} is revoked")
    print("regenerate the ACL and reload the broker, then check the node is really gone:")
    print("a reload does not close open connections. The hub refuses this node either way.")
    return 0


def show(arguments) -> int:
    records = registry(arguments).all()
    if arguments.json:
        print(json.dumps([record.model_dump(mode="json") for record in records], indent=2))
        return 0
    if not records:
        print("no nodes are registered")
        return 0
    width = max(len(record.node_id) for record in records)
    for record in records:
        pinned = (record.fingerprint or "")[:16]
        print(f"{record.node_id.ljust(width)}  {record.status:<9} {record.profile:<16} {pinned}")
    return 0


def acl(arguments) -> int:
    """Write the broker's access control list from the registry.

    One node, one name, its own topics. A satellite writes only under its own node and
    reads only its own commands; nothing a satellite can publish reaches another satellite.
    """
    nodes = registry(arguments)
    approved = len(nodes.of_status("approved"))
    prefix = arguments.topic_prefix
    lines = [
        "# Generated by scripts/satellite_admin.py. Do not edit by hand.",
        f"# {approved} approved node{'' if approved == 1 else 's'},"
        f" registry revision {nodes.revision}.",
        "",
        "# The hub reads everything the nodes write, and is the only writer of commands.",
        "user sentry-mode-hub",
        f"topic read {prefix}/nodes/+/events",
        f"topic read {prefix}/nodes/+/state",
        f"topic read {prefix}/nodes/+/health",
        f"topic read {prefix}/nodes/+/acks",
        f"topic write {prefix}/nodes/+/commands",
        f"topic write {prefix}/nodes/+/state",
        "",
    ]
    for record in nodes.of_status("approved"):
        lines += [
            f"# {record.display_name}",
            f"user {record.node_id}",
            f"topic write {prefix}/nodes/{record.node_id}/events",
            f"topic write {prefix}/nodes/{record.node_id}/state",
            f"topic write {prefix}/nodes/{record.node_id}/health",
            f"topic write {prefix}/nodes/{record.node_id}/acks",
            f"topic read {prefix}/nodes/{record.node_id}/commands",
            "",
        ]
    body = "\n".join(lines)
    if arguments.out:
        keep(arguments.out, True)  # generated: replacing it is the point, a backup is kept
        arguments.out.write_text(body, encoding="utf-8")
        print(f"wrote {arguments.out}")
        print("reload the broker, and remember that a reload does not close open connections")
    else:
        print(body, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument("--nodes-file", type=Path, default=DEFAULT_NODES_FILE)
    parser.add_argument(
        "--force", action="store_true", help="replace what is there, keeping a backup"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init-ca", help="create the certificate authority")

    hub = commands.add_parser("hub-cert", help="issue the hub's certificate")
    hub.add_argument(
        "--address",
        action="append",
        required=True,
        help="an address the satellites use; repeat for more (IP or name)",
    )

    key = commands.add_parser("node-key", help="make a key and a signing request for a node")
    key.add_argument("--node", required=True)
    key.add_argument("--out", type=Path, required=True, help="path without an extension")

    signer = commands.add_parser("sign", help="sign a node's request")
    signer.add_argument("--node", required=True)
    signer.add_argument("--csr", type=Path, required=True)
    signer.add_argument("--out", type=Path)

    new = commands.add_parser("register", help="record a node as waiting for approval")
    new.add_argument("--node", required=True)
    new.add_argument("--name")
    new.add_argument("--profile", default="sensor-presence")
    new.add_argument("--zone")
    new.add_argument("--certificate", type=Path)

    allowed = commands.add_parser("approve", help="let a node publish")
    allowed.add_argument("--node", required=True)
    allowed.add_argument("--certificate", type=Path)

    gone = commands.add_parser("revoke", help="stop listening to a node")
    gone.add_argument("--node", required=True)
    gone.add_argument("--reason")

    listing = commands.add_parser("list", help="show every registered node")
    listing.add_argument("--json", action="store_true")

    rules = commands.add_parser("acl", help="write the broker access control list")
    rules.add_argument("--out", type=Path)
    rules.add_argument("--topic-prefix", default="sentry/v1")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    handlers = {
        "init-ca": init_ca,
        "hub-cert": hub_cert,
        "node-key": node_key,
        "sign": sign,
        "register": register,
        "approve": approve,
        "revoke": revoke,
        "list": show,
        "acl": acl,
    }
    try:
        return handlers[arguments.command](arguments)
    except (Refused, ValueError, LookupError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
