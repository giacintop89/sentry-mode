"""The blocks a microphone stream is made of, packed exactly as the hub unpacks them.

The layout is the hub's, published in `contracts/satellite/v1/audio.json`; the tests hold
this file to it. Every block says which sample of the capture it starts at and when it was
read, so the hub can tell a lost block from a late one.
"""

import struct

MAGIC = b"SMA1"
VERSION = 1
HEADER = struct.Struct("<4sBBHIQQH")
RATE = 16000
CHANNELS = 1
SAMPLE_BYTES = 2
BLOCK_SAMPLES = RATE // 10
BLOCK_BYTES = BLOCK_SAMPLES * SAMPLE_BYTES
FLAG_GAP = 0x01
"""This node lost samples before this block: its own queue was full."""


def pack(sequence: int, first_sample: int, captured_ns: int, pcm: bytes, *, gap: bool) -> bytes:
    if len(pcm) % SAMPLE_BYTES or not 0 < len(pcm) <= 2 * BLOCK_BYTES:
        raise ValueError(f"a block cannot carry {len(pcm)} bytes")
    flags = FLAG_GAP if gap else 0
    count = len(pcm) // SAMPLE_BYTES
    return (
        HEADER.pack(
            MAGIC,
            VERSION,
            flags,
            0,
            sequence & 0xFFFFFFFF,
            first_sample,
            captured_ns,
            count,
        )
        + pcm
    )
