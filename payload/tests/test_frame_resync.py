"""Framing: separating frames from status text, and recovering from a slip.

Two faults on the live link motivated these. The macOS app split its input on
newlines before looking for frames, but 0x0A is an ordinary data byte inside a
frame and the modem does not escape it, so the split deleted frame bytes. And
frames are delimited at both ends -- 0x7E <frame> 0x7E 0x7E <frame> 0x7E -- so
once a scanner loses a single byte it pairs each closing delimiter with the
next frame's opening one and stays wrong forever, discarding every frame that
follows in silence. Together they took a modem forwarding 27,000 packets down
to 64 received, then to none at all.
"""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from groundstation.python.raptorhabgs.core.protocol import FrameExtractor
from common.protocol import (PacketType, TelemetryPayload, ImageDataPayload,
                             FixType, build_packet)

RAPTOR, MESH, ESC = 0x7E, 0x7B, 0x7D


def frame(packet: bytes, rssi_int: int = -23, delimiter: int = RAPTOR) -> bytes:
    """Byte-for-byte what forwardPacket() puts on the wire."""
    n = len(packet)
    header = [(n >> 8) & 0xFF, n & 0xFF, rssi_int & 0xFF, 0, 0, 0]
    checksum = 0
    for b in header + list(packet):
        checksum ^= b
    out = bytearray([delimiter])
    for b in header + list(packet) + [checksum]:
        if b == RAPTOR:
            out += bytes([ESC, 0x5E])
        elif b == MESH:
            out += bytes([ESC, 0x5B])
        elif b == ESC:
            out += bytes([ESC, 0x5D])
        else:
            out.append(b)
    out.append(delimiter)
    return bytes(out)


def telemetry(seq: int) -> bytes:
    return build_packet(PacketType.TELEMETRY, seq, TelemetryPayload(
        latitude=44.9, longitude=-93.1, altitude=1000 + seq, speed=12.0,
        heading=210.0, satellites=11, fix_type=FixType.FIX_3D,
        gps_time=1755600000 + seq, battery_mv=3980, cpu_temp=41.0,
        radio_temp=38.0, image_id=3, image_progress=57, rssi=-23))


def image(seq: int, rng: random.Random) -> bytes:
    return build_packet(PacketType.IMAGE_DATA, seq, ImageDataPayload(
        image_id=3, symbol_id=seq,
        symbol_data=bytes(rng.randrange(256) for _ in range(210))))


def feed(extractor: FrameExtractor, stream: bytes, chunk: int = 64):
    """Deliver in chunks, the way USB reads actually arrive."""
    got = []
    for i in range(0, len(stream), chunk):
        got.extend(extractor.add_data(stream[i:i + chunk]))
    return got


def mixed_stream(count=300, seed=5):
    """Telemetry and imagery in the ratio the modem reports (about 1 in 6)."""
    rng = random.Random(seed)
    packets = [image(i, rng) if i % 6 == 0 else telemetry(i) for i in range(count)]
    return b"".join(frame(p) for p in packets), packets


def test_frames_containing_newlines_survive():
    """0x0A inside a frame is data, not a line ending."""
    stream, packets = mixed_stream()
    assert stream.count(0x0A) > 20, "corpus should exercise the case"
    assert len(feed(FrameExtractor(), stream)) == len(packets)


def test_image_frames_are_not_lost():
    """Image frames are four times the size of telemetry, so a length-biased
    fault shows up here first: 0 images alongside healthy telemetry was the
    reported symptom."""
    stream, packets = mixed_stream()
    payloads = [p for _, _, p in feed(FrameExtractor(), stream)]
    images = sum(1 for p in payloads if len(p) > 100)
    assert images == sum(1 for i in range(len(packets)) if i % 6 == 0)


def test_recovers_from_a_one_byte_slip():
    """Starting on a closing delimiter is the parity trap. Everything after
    the first frame or two must still arrive."""
    stream, packets = mixed_stream()
    first = len(frame(packets[0]))
    got = feed(FrameExtractor(), stream[first - 1:])
    assert len(got) >= len(packets) - 2


@pytest.mark.parametrize("offset", [1, 7, 33, 150, 231])
def test_recovers_from_any_starting_offset(offset):
    stream, packets = mixed_stream()
    got = feed(FrameExtractor(), stream[offset:])
    assert len(got) >= len(packets) - 2


def test_recovers_from_a_dropped_fragment_mid_stream():
    stream, packets = mixed_stream()
    damaged = stream[:5000] + stream[5037:]
    got = feed(FrameExtractor(), damaged)
    assert len(got) >= len(packets) - 3


def test_raw_meshtastic_delimiter_in_image_data_is_escaped():
    """A single-radio modem opens no 0x7B frames, but must still escape the
    byte -- a parser watching for both delimiters would read a raw 0x7B in
    image data as a frame start."""
    payload = build_packet(PacketType.IMAGE_DATA, 1, ImageDataPayload(
        image_id=1, symbol_id=1, symbol_data=bytes([MESH]) * 210))
    wire = frame(payload)
    assert wire.count(MESH) == 0, "0x7B must not appear raw inside a frame"
    assert len(feed(FrameExtractor(), wire)) == 1


def test_status_text_between_frames_is_not_confused_for_a_frame():
    stream, packets = mixed_stream(count=40)
    noisy = (b"[READY] Listening for packets...\r\n" + stream[:400]
             + b"\r\n[RADIO] Reconfiguring for new settings...\n" + stream[400:])
    assert len(feed(FrameExtractor(), noisy)) >= len(packets) - 2


def test_a_false_start_costs_one_byte_not_a_frame():
    """The whole point of validating before consuming."""
    extractor = FrameExtractor()
    stream, packets = mixed_stream(count=20)
    assert len(feed(extractor, b"\x7e\x7e\x7e" + stream)) == len(packets)


def test_garbage_never_yields_a_frame():
    rng = random.Random(9)
    junk = bytes(rng.randrange(256) for _ in range(200000))
    assert feed(FrameExtractor(), junk) == []
