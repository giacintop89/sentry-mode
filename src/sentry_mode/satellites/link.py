"""How a Pico with no radio reaches the hub: frames over a USB cable, and what they mean.

A Pico or Pico 2 without a W has no radio, so it cannot be a satellite on its own. What it
can do is speak to a Linux machine that already is one, over the USB cable that powers it,
and let that machine carry its messages to the broker. This file is the shape of what goes
down the cable, and `scripts/pico_bridge.py` is the machine's half of it.

A frame is a magic, a version, what it carries, a counter, a length, the payload, and a
CRC-32 over all of it. The counter is per direction and increases by one a frame, so a
reader can say how many went missing rather than only that something did. The checksum
catches a cable that dropped a byte; it catches nothing at all about who is on the other
end of it, and neither does the USB serial number. **A device on this link is trusted
because somebody physically plugged it in and wrote it into the bridge's configuration,
and for no other reason.**

What a frame carries is one of the five channels a satellite already has — its events, its
state, its health, its acknowledgements, the commands coming back — plus two the cable
needs and the air does not: the time, because a board with no radio has no SNTP to ask,
and a hello, which is the bridge saying it is here and the node should announce itself.
The kind is also the direction: a node that sent a command, or a bridge that sent an
event, is refused rather than believed.

The board's console goes down the same cable, and inside frames like everything else:
`SAID` is a line the board printed, `TYPED` is a line somebody sent it. A board with one
USB port has one channel, and the choice is between text and frames sharing it unmarked —
where a line of diagnostics can be read as a frame, and a frame lands in somebody's
terminal — or text being carried as what it is. It is carried as what it is, and neither
kind is ever forwarded to the broker.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum

MAGIC = b"SMB1"
VERSION = 1
HEADER = struct.Struct("<4sBBBIH")
"""magic, version, what it carries, flags, counter, payload length."""
TRAILER = struct.Struct("<I")
"""CRC-32 of everything before it, the magic included."""
FIELDS = (
    ("magic", "bytes4"),
    ("version", "u8"),
    ("carries", "u8"),
    ("flags", "u8"),
    ("counter", "u32"),
    ("length", "u16"),
)
MAX_PAYLOAD = 2048
"""What the largest thing either side sends fits in: a configuration is under two
kilobytes on these boards, and nothing else comes close."""
MAX_FRAME = HEADER.size + MAX_PAYLOAD + TRAILER.size


class Carries(IntEnum):
    """What a frame holds. The number is on the wire, so it never changes meaning."""

    NOTHING = 0
    EVENTS = 1
    STATE = 2
    HEALTH = 3
    ACKS = 4
    COMMANDS = 5
    TIME = 6
    HELLO = 7
    SAID = 8
    TYPED = 9


FROM_THE_NODE = frozenset(
    {Carries.EVENTS, Carries.STATE, Carries.HEALTH, Carries.ACKS, Carries.SAID}
)
"""What a board sends. The first four are the channel of the same name, and the payload is
the same JSON that would have gone to the broker on it; `SAID` is a line of its console,
which goes to whoever is watching and nowhere near a topic."""
TO_THE_NODE = frozenset({Carries.COMMANDS, Carries.TIME, Carries.HELLO, Carries.TYPED})
"""What the bridge sends. `TIME` carries `{"unix_ms": …}`, `HELLO` carries nothing — a
board answers it by announcing itself, as it would to a broker — and `TYPED` carries a
line for the board's console, which is the only way to provision one over this cable."""

CHANNELS = {
    Carries.EVENTS: "events",
    Carries.STATE: "state",
    Carries.HEALTH: "health",
    Carries.ACKS: "acks",
    Carries.COMMANDS: "commands",
}
"""Which topic a frame's payload belongs on, for the four the bridge forwards and the one
it receives. `TIME`, `HELLO`, `SAID` and `TYPED` are the cable's own and have no topic: a
console line is not a message about this node, and nothing publishes it."""


class LinkError(ValueError):
    """What arrived is not a frame of this format, or not one this side may send."""


@dataclass(frozen=True)
class Frame:
    carries: Carries
    counter: int
    payload: bytes

    @property
    def channel(self) -> str | None:
        return CHANNELS.get(self.carries)


def checksum(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def pack(frame: Frame) -> bytes:
    """One frame, ready to write to the port."""
    if len(frame.payload) > MAX_PAYLOAD:
        raise LinkError(f"a frame of {len(frame.payload)} bytes")
    head = HEADER.pack(MAGIC, VERSION, int(frame.carries), 0, frame.counter, len(frame.payload))
    body = head + frame.payload
    return body + TRAILER.pack(checksum(body))


def contract() -> dict:
    """The layout, as the firmware reads it from the contracts directory."""
    return {
        "schema_version": 1,
        "magic": MAGIC.decode(),
        "version": VERSION,
        "byte_order": "little",
        "header_bytes": HEADER.size,
        "trailer_bytes": TRAILER.size,
        "struct": HEADER.format,
        "trailer_struct": TRAILER.format,
        "checksum": "crc32",
        "checksum_covers": "the header and the payload, the magic included",
        "fields": [{"name": name, "type": kind} for name, kind in FIELDS],
        "max_payload_bytes": MAX_PAYLOAD,
        "carries": {one.name.lower(): int(one) for one in Carries},
        "from_the_node": sorted(int(one) for one in FROM_THE_NODE),
        "to_the_node": sorted(int(one) for one in TO_THE_NODE),
        "channels": {str(int(one)): name for one, name in CHANNELS.items()},
    }


@dataclass
class Reader:
    """Bytes off the port in, frames out, and a count of what the cable cost.

    Fed whatever arrives, in whatever sizes it arrives in. A frame is returned only when
    its checksum is right; anything else is thrown away one byte at a time until the magic
    lines up again, which is what makes a cable that dropped a byte a hiccup rather than a
    session that has to be restarted.
    """

    expect_from_the_node: bool = True
    """Which side is being read. A frame the other side has no business sending is
    refused, so a mistake is loud instead of being acted on."""

    frames: int = 0
    discarded: int = 0
    """Bytes thrown away looking for the next magic. A cable that is working reports
    zero, and a number that climbs is a cable, a driver or a board losing bytes."""
    missed: int = 0
    """Frames the counter says never arrived. A cable that is working reports zero."""

    def __post_init__(self) -> None:
        self._pending = b""
        self._counter: int | None = None

    def feed(self, data: bytes) -> list[Frame]:
        self._pending += data
        out: list[Frame] = []
        while True:
            frame = self._take()
            if frame is None:
                return out
            out.append(frame)

    def _take(self) -> Frame | None:
        while True:
            start = self._pending.find(MAGIC)
            if start < 0:
                # Keep only what could still be the beginning of a magic.
                keep = len(MAGIC) - 1
                if len(self._pending) > keep:
                    self._discard(len(self._pending) - keep)
                return None
            if start:
                self._discard(start)
            if len(self._pending) < HEADER.size:
                return None
            _, version, carries, flags, counter, length = HEADER.unpack_from(self._pending)
            if version != VERSION or flags or length > MAX_PAYLOAD:
                self._discard(len(MAGIC))
                continue
            end = HEADER.size + length + TRAILER.size
            if len(self._pending) < end:
                return None
            body = self._pending[: HEADER.size + length]
            (said,) = TRAILER.unpack_from(self._pending, HEADER.size + length)
            if said != checksum(body):
                self._discard(len(MAGIC))
                continue
            self._pending = self._pending[end:]
            return self._accept(carries, counter, body[HEADER.size :])

    def _discard(self, count: int) -> None:
        self.discarded += count
        self._pending = self._pending[count:]

    def _accept(self, carries: int, counter: int, payload: bytes) -> Frame:
        try:
            what = Carries(carries)
        except ValueError:
            raise LinkError(f"a frame carrying {carries}, which is nothing") from None
        allowed = FROM_THE_NODE if self.expect_from_the_node else TO_THE_NODE
        if what not in allowed:
            side = "node" if self.expect_from_the_node else "bridge"
            raise LinkError(f"a {what.name.lower()} frame is not something the {side} sends")
        if self._counter is not None:
            self.missed += (counter - self._counter - 1) & 0xFFFFFFFF
        self._counter = counter
        self.frames += 1
        return Frame(what, counter, payload)

    def status(self) -> dict:
        return {
            "frames": self.frames,
            "discarded": self.discarded,
            "missed": self.missed,
        }


__all__ = [
    "CHANNELS",
    "FROM_THE_NODE",
    "MAGIC",
    "MAX_FRAME",
    "MAX_PAYLOAD",
    "TO_THE_NODE",
    "VERSION",
    "Carries",
    "Frame",
    "LinkError",
    "Reader",
    "contract",
    "pack",
]
