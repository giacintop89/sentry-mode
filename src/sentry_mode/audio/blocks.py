"""How sound from a satellite is framed on the wire, and how the hub puts it back together.

A microphone stream is a run of blocks, each a fixed header and the samples it carries:
signed 16-bit little-endian, one channel, 16 kHz. The header numbers the block, says which
sample of the node's capture it starts at and when the node read it, so the hub can tell
a lost block from a late one without trusting either clock.

The satellite has no Pydantic and no shared code with the hub, so the layout is published
as `contracts/satellite/v1/audio.json` by `scripts/generate_contracts.py`, and the agent's
tests hold its own packing to that file.

Putting a stream back together is deliberately strict. A block that is not newer than the
last one is dropped. A block that starts later than expected is a gap: up to `MAX_FILL`
samples of it are filled with silence, so what plays and what is recorded keep their
length; a longer gap is reported and not filled. Anything that does not parse ends the
connection: the node starts a fresh one, and nothing half-read is ever played.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"SMA1"
VERSION = 1
HEADER = struct.Struct("<4sBBHIQQH")
"""magic, version, flags, reserved, sequence, first sample, captured (ns), sample count."""
FIELDS = (
    ("magic", "bytes4"),
    ("version", "u8"),
    ("flags", "u8"),
    ("reserved", "u16"),
    ("sequence", "u32"),
    ("first_sample", "u64"),
    ("captured_ns", "u64"),
    ("samples", "u16"),
)
RATE = 16000
CHANNELS = 1
SAMPLE_BYTES = 2
BLOCK_SAMPLES = RATE // 10
"""100 ms: short enough to hear promptly, long enough that a Zero W is not busy framing."""
MAX_BLOCK_SAMPLES = 2 * BLOCK_SAMPLES
FLAG_GAP = 0x01
"""The node lost samples before this block: its own queue was full."""
FLAGS = {"gap": FLAG_GAP}
MAX_FILL = RATE
"""The longest gap filled with silence: one second."""


class BlockError(ValueError):
    """What arrived is not a block of this format."""


@dataclass(frozen=True)
class Block:
    sequence: int
    first_sample: int
    captured_ns: int
    flags: int
    pcm: bytes

    @property
    def samples(self) -> int:
        return len(self.pcm) // SAMPLE_BYTES


def pack(block: Block) -> bytes:
    """The hub never sends blocks; this is here so tests speak the format exactly."""
    return (
        HEADER.pack(
            MAGIC,
            VERSION,
            block.flags,
            0,
            block.sequence,
            block.first_sample,
            block.captured_ns,
            block.samples,
        )
        + block.pcm
    )


def contract() -> dict:
    """The layout, as the satellite reads it from the contracts directory."""
    return {
        "schema_version": 1,
        "magic": MAGIC.decode(),
        "version": VERSION,
        "byte_order": "little",
        "header_bytes": HEADER.size,
        "struct": HEADER.format,
        "fields": [{"name": name, "type": kind} for name, kind in FIELDS],
        "sample_format": "s16le",
        "channels": CHANNELS,
        "sample_rate": RATE,
        "block_samples": BLOCK_SAMPLES,
        "max_block_samples": MAX_BLOCK_SAMPLES,
        "flags": FLAGS,
    }


@dataclass
class Reassembler:
    """Bytes from one connection in, PCM ready to play out, and a count of what went wrong."""

    blocks: int = 0
    duplicates: int = 0
    gaps: int = 0
    lost_samples: int = 0
    filled_samples: int = 0
    node_gaps: int = 0
    last_captured_ns: int | None = None

    def __post_init__(self) -> None:
        self._pending = b""
        self._sequence: int | None = None
        self._next_sample: int | None = None

    def feed(self, data: bytes) -> list[bytes]:
        self._pending += data
        out: list[bytes] = []
        while len(self._pending) >= HEADER.size:
            magic, version, flags, reserved, sequence, first, captured, count = HEADER.unpack_from(
                self._pending
            )
            if magic != MAGIC or version != VERSION:
                raise BlockError("not a block of sound")
            if reserved or flags & ~FLAG_GAP:
                raise BlockError("a block uses header bits nobody agreed on")
            if not 0 < count <= MAX_BLOCK_SAMPLES:
                raise BlockError(f"a block of {count} samples")
            end = HEADER.size + count * SAMPLE_BYTES
            if len(self._pending) < end:
                break
            pcm, self._pending = self._pending[HEADER.size : end], self._pending[end:]
            out.extend(self._accept(Block(sequence, first, captured, flags, pcm)))
        return out

    def _accept(self, block: Block) -> list[bytes]:
        if self._sequence is not None and block.sequence <= self._sequence:
            self.duplicates += 1
            return []
        expected = self._next_sample
        if expected is not None and block.first_sample < expected:
            # Newer by number but overlapping what was played: the node restarted its count
            # without a new connection, which it never does. Refuse it rather than guess.
            raise BlockError("a block overlaps the one before it")
        self._sequence = block.sequence
        self._next_sample = block.first_sample + block.samples
        self.blocks += 1
        self.last_captured_ns = block.captured_ns
        if block.flags & FLAG_GAP:
            self.node_gaps += 1
        out = []
        missing = 0 if expected is None else block.first_sample - expected
        if missing:
            self.gaps += 1
            self.lost_samples += missing
            if missing <= MAX_FILL:
                self.filled_samples += missing
                out.append(bytes(missing * SAMPLE_BYTES))
        out.append(block.pcm)
        return out

    def status(self) -> dict:
        return {
            "blocks": self.blocks,
            "duplicates": self.duplicates,
            "gaps": self.gaps,
            "node_gaps": self.node_gaps,
            "lost_samples": self.lost_samples,
            "filled_samples": self.filled_samples,
        }


__all__ = [
    "BLOCK_SAMPLES",
    "Block",
    "BlockError",
    "HEADER",
    "RATE",
    "Reassembler",
    "contract",
    "pack",
]
