"""The frames that cross the USB cable to a satellite with no radio.

The firmware's half of this is checked against it by `firmware/pico/tools/link_check.py`,
which runs in `make pico`. What is here is the bridge's own reading: a cable that dropped a
byte, a cable that invented one, a frame from the side that never sends it, and the
counters that turn all of that into a number somebody can look at.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path

import pytest

from sentry_mode.satellites import link

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "satellite" / "v1" / "bridge.json"


def frame(carries=link.Carries.EVENTS, counter=0, payload=b"{}") -> bytes:
    return link.pack(link.Frame(carries, counter, payload))


def test_a_frame_is_its_header_its_payload_and_a_checksum_over_both():
    raw = frame(counter=0x01020304, payload=b'{"a":1}')
    assert raw[:4] == b"SMB1"
    assert raw[4] == link.VERSION
    assert raw[5] == int(link.Carries.EVENTS)
    assert raw[6] == 0
    assert raw[link.HEADER.size : link.HEADER.size + 7] == b'{"a":1}'
    assert link.TRAILER.unpack_from(raw, len(raw) - 4)[0] == zlib.crc32(raw[:-4])


def test_the_published_contract_is_the_format():
    said = json.loads(CONTRACT.read_text())
    assert said == link.contract()
    assert said["magic"] == "SMB1"
    assert said["struct"] == link.HEADER.format
    assert said["checksum"] == "crc32"
    # Direction is part of the format, not a convention on top of it.
    assert set(said["from_the_node"]) & set(said["to_the_node"]) == set()


def test_frames_come_out_in_the_order_they_went_in():
    reader = link.Reader()
    stream = frame(link.Carries.STATE, 0, b'{"online":true}') + frame(link.Carries.EVENTS, 1)
    out = reader.feed(stream)
    assert [one.carries for one in out] == [link.Carries.STATE, link.Carries.EVENTS]
    assert out[0].payload == b'{"online":true}'
    assert out[0].channel == "state"
    assert reader.status() == {"frames": 2, "discarded": 0, "missed": 0}


def test_a_frame_split_anywhere_is_put_back_together():
    reader = link.Reader()
    raw = frame(counter=3, payload=b'{"long":"enough to be split"}')
    out = [one for byte in raw for one in reader.feed(bytes([byte]))]
    assert len(out) == 1 and out[0].counter == 3
    assert reader.discarded == 0


def test_rubbish_before_a_frame_is_thrown_away_and_counted():
    reader = link.Reader()
    out = reader.feed(b"a line of console text\n" + frame(counter=1))
    assert len(out) == 1
    assert reader.discarded == 23


def test_a_byte_lost_costs_that_frame_and_not_the_next_one():
    reader = link.Reader()
    lost = frame(counter=1, payload=b'{"a":1}')[:-1]
    out = reader.feed(lost + frame(counter=2, payload=b'{"b":2}'))
    assert [one.payload for one in out] == [b'{"b":2}']
    assert reader.discarded > 0


def test_a_payload_that_arrived_wrong_is_refused_rather_than_handed_on():
    reader = link.Reader()
    damaged = bytearray(frame(counter=1, payload=b'{"a":1}'))
    damaged[link.HEADER.size + 2] ^= 0xFF
    out = reader.feed(bytes(damaged) + frame(counter=2, payload=b'{"b":2}'))
    assert [one.payload for one in out] == [b'{"b":2}']


def test_a_frame_from_the_side_that_never_sends_it_is_refused():
    reader = link.Reader(expect_from_the_node=True)
    with pytest.raises(link.LinkError, match="not something the node sends"):
        reader.feed(frame(link.Carries.COMMANDS, 1, b'{"c":1}'))
    # And the other way round, for the board's reader.
    board = link.Reader(expect_from_the_node=False)
    with pytest.raises(link.LinkError, match="not something the bridge sends"):
        board.feed(frame(link.Carries.EVENTS, 1))
    assert board.feed(frame(link.Carries.HELLO, 2, b""))[0].carries == link.Carries.HELLO


def test_a_kind_nobody_has_defined_is_not_read_as_the_nearest_one():
    reader = link.Reader()
    made_up = bytearray(frame(counter=1))
    made_up[5] = 99
    made_up[-4:] = link.TRAILER.pack(zlib.crc32(bytes(made_up[:-4])))
    with pytest.raises(link.LinkError, match="which is nothing"):
        reader.feed(bytes(made_up))


def test_a_version_or_a_flag_this_bridge_does_not_know_is_not_read():
    reader = link.Reader()
    newer = bytearray(frame(counter=1, payload=b'{"a":1}'))
    newer[4] = 2
    flagged = bytearray(frame(counter=2, payload=b'{"b":2}'))
    flagged[6] = 0x01
    out = reader.feed(bytes(newer) + bytes(flagged) + frame(counter=3, payload=b'{"c":3}'))
    assert [one.payload for one in out] == [b'{"c":3}']


def test_frames_that_never_arrived_are_a_number_rather_than_a_silence():
    reader = link.Reader()
    reader.feed(frame(counter=10) + frame(counter=14))
    assert reader.missed == 3
    assert reader.frames == 2


def test_a_counter_that_wrapped_is_not_four_billion_lost_frames():
    reader = link.Reader()
    reader.feed(frame(counter=0xFFFFFFFF) + frame(counter=0) + frame(counter=1))
    assert reader.missed == 0
    assert reader.frames == 3


def test_the_longest_payload_goes_through_and_a_longer_one_is_never_packed():
    reader = link.Reader()
    biggest = b"x" * link.MAX_PAYLOAD
    assert reader.feed(frame(payload=biggest))[0].payload == biggest
    with pytest.raises(link.LinkError, match="a frame of 2049 bytes"):
        link.pack(link.Frame(link.Carries.EVENTS, 0, biggest + b"x"))


def test_a_length_longer_than_the_format_allows_is_not_waited_for():
    # A header that claims more than a frame may carry: believing it would leave the reader
    # waiting for bytes that are never coming, with the next real frame stuck behind them.
    reader = link.Reader()
    lying = bytearray(frame(counter=1))
    lying[11:13] = (link.MAX_PAYLOAD + 1).to_bytes(2, "little")
    out = reader.feed(bytes(lying) + frame(counter=2, payload=b'{"real":true}'))
    assert [one.payload for one in out] == [b'{"real":true}']


def test_a_console_line_is_a_frame_like_everything_else_and_goes_to_no_topic():
    # The board has one USB port. Either text and frames share it unmarked — where a line
    # of diagnostics can be read as a frame, and a frame lands in somebody's terminal — or
    # text is carried as what it is. It is carried as what it is.
    said = link.Frame(link.Carries.SAID, 0, b"# ready pico2 node=pico-cablato\n")
    reader = link.Reader(expect_from_the_node=True)
    out = reader.feed(link.pack(said))
    assert out[0].payload == said.payload
    assert out[0].channel is None
    board = link.Reader(expect_from_the_node=False)
    typed = board.feed(link.pack(link.Frame(link.Carries.TYPED, 0, b"status")))
    assert typed[0].carries == link.Carries.TYPED and typed[0].channel is None


def test_neither_half_of_the_console_travels_the_way_the_other_one_does():
    reader = link.Reader(expect_from_the_node=True)
    with pytest.raises(link.LinkError, match="not something the node sends"):
        reader.feed(link.pack(link.Frame(link.Carries.TYPED, 0, b"status")))
    board = link.Reader(expect_from_the_node=False)
    with pytest.raises(link.LinkError, match="not something the bridge sends"):
        board.feed(link.pack(link.Frame(link.Carries.SAID, 0, b"# ready\n")))
