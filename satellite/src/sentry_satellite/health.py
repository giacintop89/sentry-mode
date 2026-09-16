"""Proof of life, and the numbers that say whether to believe the rest.

Health is published often, at QoS 0, and never blocks on anything. Everything it reports
is read defensively: a board that cannot answer a question says `null` rather than making
the agent fall over, and a missing answer is itself worth seeing on the hub.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def uptime_seconds() -> float | None:
    text = _read("/proc/uptime")
    try:
        return float(text.split()[0]) if text else None
    except (ValueError, IndexError):
        return None


def temperature_c() -> float | None:
    text = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(text) / 1000, 1) if text else None
    except ValueError:
        return None


def load_average() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def memory_available_kb() -> int | None:
    text = _read("/proc/meminfo")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            try:
                return int(line.split()[1])
            except (ValueError, IndexError):
                return None
    return None


def throttled() -> str | None:
    """The Raspberry Pi firmware's opinion on power and temperature, if it will say."""
    binary = shutil.which("vcgencmd")
    if binary is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary, no shell, no input
            [binary, "get_throttled"], capture_output=True, text=True, timeout=2
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    _, _, value = result.stdout.strip().partition("=")
    return value or None


def clock_status() -> str:
    """Whether this board's idea of the time is worth anything.

    `synced` means the operating system says a time source has been accepted, not that the
    JSON claimed so. The hub does not have to believe this either: it is one input to a
    decision it makes for itself.
    """
    binary = shutil.which("timedatectl")
    if binary is None:
        return "unknown"
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary, no shell, no input
            [binary, "show", "--property=NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    answer = result.stdout.strip().lower()
    if answer == "yes":
        return "synced"
    if answer == "no":
        return "unsynced"
    return "unknown"


def board() -> dict:
    return {
        "uptime_seconds": uptime_seconds(),
        "temperature_c": temperature_c(),
        "load1": load_average(),
        "memory_available_kb": memory_available_kb(),
        "throttled": throttled(),
    }


def payload(
    *,
    node_id: str,
    boot_id: str,
    started_at: float,
    clock: str,
    queue: dict,
    sources: dict,
    now: float | None = None,
) -> dict:
    moment = time.monotonic() if now is None else now
    return {
        "schema_version": 1,
        "node_id": node_id,
        "boot_id": boot_id,
        "agent_uptime_seconds": round(moment - started_at, 1),
        "clock_status": clock,
        "queue": queue,
        "sources": sources,
        "board": board(),
    }
