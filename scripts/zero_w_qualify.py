#!/usr/bin/env python3
"""Qualify a Raspberry Pi Zero W as a satellite target (gate G0).

Copy this file to the Zero and run it there. It reports what the board actually
is; it does not install anything, change configuration, or touch the hub. A
successful run on a Pi 5 or a Zero 2 W proves nothing about the original Zero W,
so the architecture is reported first and every result carries it.

    python3 zero_w_qualify.py
    python3 zero_w_qualify.py --broker 192.168.1.10:8883 --ca /etc/sentry-satellite/ca.crt
    python3 zero_w_qualify.py --video 10 --json report.json

Only the standard library is used: the satellite profile forbids a build
toolchain on the node, so a check must not need one either.
"""

import argparse
import json
import os
import platform
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import time
from pathlib import Path

TARGET_MACHINE = "armv6l"
RESULTS = []


def record(name, value, note=""):
    RESULTS.append({"check": name, "value": value, "note": note})
    marker = "  " if not note else "! "
    print("%s%-22s %s%s" % (marker, name, value, "   (%s)" % note if note else ""))
    return value


def read_text(path, default=""):
    try:
        return Path(path).read_text(errors="replace").strip("\0\n ")
    except OSError:
        return default


def run(command, timeout=20):
    """Run a command without a shell; a missing binary is a result, not a crash."""
    if shutil.which(command[0]) is None:
        return None, "%s not installed" % command[0]
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    return done.stdout.strip(), done.stderr.strip()


def board():
    print("\n== board ==")
    model = read_text("/proc/device-tree/model") or "unknown"
    record("model", model)
    machine = record("machine", platform.machine())
    if machine != TARGET_MACHINE:
        record(
            "target",
            "NOT the Zero W",
            "expected %s; results here do not qualify the satellite" % TARGET_MACHINE,
        )
    else:
        record("target", "Zero W class (%s)" % TARGET_MACHINE)
    record("kernel", platform.release())
    os_release = read_text("/etc/os-release")
    pretty = re.search(r'PRETTY_NAME="([^"]+)"', os_release)
    record("os", pretty.group(1) if pretty else "unknown")
    record("python", "%s (%d-bit)" % (platform.python_version(), struct.calcsize("P") * 8))
    if sys.version_info < (3, 11):
        record("python tomllib", "missing", "the agent configuration format needs 3.11+")


def resources():
    print("\n== resources ==")
    meminfo = read_text("/proc/meminfo")
    for field in ("MemTotal", "MemAvailable"):
        found = re.search(r"%s:\s+(\d+) kB" % field, meminfo)
        if found:
            record(field.lower(), "%d MiB" % (int(found.group(1)) // 1024))
    usage = shutil.disk_usage("/")
    record("disk free", "%d MiB of %d MiB" % (usage.free // 2**20, usage.total // 2**20))
    load = os.getloadavg()
    record("loadavg", "%.2f %.2f %.2f" % load)
    throttled, _ = run(["vcgencmd", "get_throttled"])
    if throttled:
        flags = throttled.partition("=")[2]
        record(
            "throttled",
            flags,
            "" if flags in ("0x0", "0") else "under-voltage or throttling seen since boot",
        )


def clock():
    print("\n== clock ==")
    status, _ = run(["timedatectl", "show", "--property=NTPSynchronized", "--value"])
    if status is None:
        record("ntp", "unknown", "timedatectl unavailable; remote events need a verified clock")
    else:
        synced = status.strip() == "yes"
        record("ntp", "synchronized" if synced else "NOT synchronized",
               "" if synced else "events would be time_uncertain on the hub")
    record("utc now", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def peripherals():
    print("\n== peripherals ==")
    chips = sorted(str(p) for p in Path("/dev").glob("gpiochip*"))
    record("gpio", ", ".join(chips) if chips else "no gpiochip device")
    cameras, error = run(["rpicam-hello", "--list-cameras"], timeout=30)
    if cameras is None:
        cameras, error = run(["libcamera-hello", "--list-cameras"], timeout=30)
    if cameras is None:
        record("camera", "no rpicam/libcamera tools", error or "")
    else:
        found = [line.strip() for line in cameras.splitlines() if re.match(r"^\s*\d+\s*:", line)]
        record("camera", found[0] if found else "none detected", "" if found else cameras[:80])
    capture, error = run(["arecord", "-l"])
    if capture is None:
        record("audio in", "arecord unavailable", error or "")
    else:
        cards = [line for line in capture.splitlines() if line.startswith("card ")]
        record("audio in", cards[0] if cards else "no capture device")
    record("bluetooth", "present" if Path("/sys/class/bluetooth").exists() else "absent")
    wifi = read_text("/proc/net/wireless")
    record("wifi", "present" if "wlan" in wifi else "not reported")


def dependencies():
    print("\n== agent dependencies ==")
    record("ssl", ssl.OPENSSL_VERSION)
    try:
        import paho.mqtt  # noqa: F401

        version = getattr(paho.mqtt, "__version__", "unknown")
        record("paho-mqtt", version)
    except ImportError:
        record("paho-mqtt", "not installed", "install a pure-Python wheel; no build on ARMv6")
    for module in ("sqlite3", "tomllib"):
        try:
            __import__(module)
            record(module, "available")
        except ImportError:
            record(module, "missing")
    for binary in ("ffmpeg", "rpicam-vid"):
        record(binary, shutil.which(binary) or "not installed")


def broker(address, ca_file):
    print("\n== hub reachability ==")
    host, _, port = address.partition(":")
    port = int(port or 8883)
    try:
        with socket.create_connection((host, port), timeout=10) as raw:
            record("tcp", "connected to %s:%d" % (host, port))
            if ca_file is None:
                record("tls", "skipped", "pass --ca to verify the hub certificate")
                return
            context = ssl.create_default_context(cafile=ca_file)
            with context.wrap_socket(raw, server_hostname=host) as tls:
                peer = tls.getpeercert()
                names = [v for k, v in peer.get("subjectAltName", ()) if k in ("DNS", "IP Address")]
                record("tls", "verified, %s" % tls.version())
                missing = "" if names else "no SAN for %s" % host
                record("cert SAN", ", ".join(names) or "none", missing)
    except (OSError, ssl.SSLError) as exc:
        record("broker", "FAILED", str(exc))


def video(seconds, width=640, height=480, fps=10):
    """Record a short H.264 clip to measure the encoder, not to prove the stream."""
    print("\n== camera encode (%ds at %dx%d/%d) ==" % (seconds, width, height, fps))
    if shutil.which("rpicam-vid") is None:
        record("encode", "rpicam-vid not installed")
        return
    output = Path("/tmp/sentry-zero-qualify.h264")
    command = [
        "rpicam-vid", "-t", str(seconds * 1000), "--width", str(width), "--height", str(height),
        "--framerate", str(fps), "--codec", "h264", "--nopreview", "-o", str(output),
    ]
    started = time.monotonic()
    cpu_before = time.process_time()
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=seconds + 30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        record("encode", "FAILED", str(exc))
        return
    elapsed = time.monotonic() - started
    if done.returncode != 0 or not output.exists():
        record("encode", "FAILED", (done.stderr or "")[:120])
        return
    size = output.stat().st_size
    record("clip", "%d KiB in %.1fs" % (size // 1024, elapsed))
    record("bitrate", "%.2f Mbit/s" % (size * 8 / elapsed / 1e6), "measured over a short clip")
    record("driver cpu", "%.2fs of this process" % (time.process_time() - cpu_before),
           "encoding happens in the GPU block, not here")
    output.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", help="hub MQTT address, host[:port]")
    parser.add_argument("--ca", help="CA file used to verify the hub certificate")
    parser.add_argument("--video", type=int, metavar="SECONDS",
                        help="record a short clip to measure the camera encoder")
    parser.add_argument("--json", type=Path, help="also write the report to this file")
    options = parser.parse_args()

    print("sentry-mode satellite qualification — %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    board()
    resources()
    clock()
    peripherals()
    dependencies()
    if options.broker:
        broker(options.broker, options.ca)
    if options.video:
        video(options.video)

    print("\nNothing here qualifies a profile on its own: record these values in "
          "docs/adr/zero-w-runtime.md together with the image and package versions.")
    if options.json:
        options.json.write_text(json.dumps(
            {"machine": platform.machine(), "results": RESULTS}, indent=2) + "\n")
        print("report written to %s" % options.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
