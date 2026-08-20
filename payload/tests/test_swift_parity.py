"""
Cross-implementation parity between the Python payload and the Swift app.

The two halves of every wire protocol here are written independently: the
payload in Python, the companion app in Swift. They must agree byte for byte.
A mismatch never fails loudly -- it looks like a payload that never answers,
or a balloon whose beacons the app silently ignores -- so it has to be caught
by comparing real output from both.

Skipped automatically when Swift or the app sources are unavailable, so the
suite still runs on a Pi.
"""

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from common.linkproto import Channel, FrameDecoder, encode_frame
from common.meshtastic.crypto import (
    aes_ctr,
    build_nonce,
    channel_hash,
    expand_psk,
)
from common.meshtastic.packet import build_packet, node_id_from_callsign

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_SOURCES = REPO_ROOT / "groundstation/macos" / "RaptorHabGS"
# Swift only permits top-level statements in a file literally named
# main.swift, so the parity program has to keep that name.
PARITY_MAIN = REPO_ROOT / "groundstation/macos" / "Tests" / "main.swift"

# Only the files the parity checks actually touch. Compiling the whole app
# would drag in SwiftUI views and CoreBluetooth for no benefit.
NEEDED_SOURCES = [
    "FrameScanner.swift",
    "LinkProtocol.swift",
    "MeshtasticProtobuf.swift",
    "MeshtasticProtocol.swift",
    "SHA256.swift",
]


def _swift_available() -> bool:
    if shutil.which("swiftc") is None:
        return False
    if not PARITY_MAIN.is_file():
        return False
    return all((APP_SOURCES / name).is_file() for name in NEEDED_SOURCES)


requires_swift = pytest.mark.skipif(
    not _swift_available(),
    reason="swiftc or the macOS app sources are not available here",
)


def modem_streams():
    """Streams for the Swift frame scanner, in the order main.swift reads them.

    Built here so both halves agree on the wire format, and so the awkward
    cases are explicit: a frame full of newlines, a one-byte slip, status text
    mixed in with frames, and pure noise.
    """
    from tests.test_frame_resync import frame, telemetry, image, mixed_stream

    clean, packets = mixed_stream(count=30, seed=2)
    # Status text arrives between frames, never inside one -- the modem
    # finishes a frame before it prints.
    framed = [frame(p) for p in packets]
    text = (b"[READY] Listening for packets...\r\n" + b"".join(framed[:5])
            + b"\n[RADIO] Reconfiguring for new settings...\r\n"
            + b"".join(framed[5:]))
    noise = bytes((i * 37 + 11) % 256 for i in range(4000))
    return [clean, clean[len(frame(packets[0])) - 1:], text, noise]


@pytest.fixture(scope="module")
def swift_results(tmp_path_factory):
    """
    Compile and run the Swift parity program, returning its output.

    A trimmed CRC32 and node-id shim stands in for the two symbols the parity
    subset needs from files that would otherwise pull in the whole app.
    """
    build_dir = tmp_path_factory.mktemp("swift-parity")

    shim = build_dir / "Shim.swift"
    shim.write_text(
        """
import Foundation

// LinkProtocol.swift uses the CRC32 defined in Protocol.swift, which is part
// of the RAPTOR packet parser and pulls in far more than this check needs.
struct CRC32 {
    private static let table: [UInt32] = {
        (0..<256).map { index -> UInt32 in
            var value = UInt32(index)
            for _ in 0..<8 {
                value = (value & 1) != 0 ? (value >> 1) ^ 0xEDB88320 : value >> 1
            }
            return value
        }
    }()

    static func calculate(data: Data, initial: UInt32 = 0xFFFFFFFF) -> UInt32 {
        var crc = initial
        for byte in data {
            crc = table[Int((crc ^ UInt32(byte)) & 0xFF)] ^ (crc >> 8)
        }
        return crc ^ 0xFFFFFFFF
    }
}

// The node-id derivation lives on MeshtasticManager, which is a @MainActor
// ObservableObject. Re-expose just the derivation, calling the same SHA256.
enum MeshtasticManager {
    static func nodeID(forCallsign callsign: String, payloadID: Int) -> UInt32 {
        let seed = "\\(callsign.trimmingCharacters(in: .whitespaces).uppercased())#\\(payloadID)"
        let digest = SHA256Digest.hash(Data(seed.utf8))
        let value = UInt32(digest[0])
            | UInt32(digest[1]) << 8
            | UInt32(digest[2]) << 16
            | UInt32(digest[3]) << 24
        let span: UInt32 = 0xFFFFFFFE - 0x00001000
        return 0x00001000 + (value % span)
    }
}
"""
    )

    binary = build_dir / "parity"
    sources = [str(APP_SOURCES / name) for name in NEEDED_SOURCES]

    compile_result = subprocess.run(
        ["swiftc", "-O", "-o", str(binary), str(shim), str(PARITY_MAIN), *sources],
        capture_output=True, text=True, timeout=600,
    )
    if compile_result.returncode != 0:
        pytest.skip(f"parity program did not compile:\n{compile_result.stderr[:2000]}")

    # Frames produced by Python, for the Swift decoder to chew on.
    python_frames = b"".join([
        encode_frame(Channel.CONTROL, b'{"id":1,"method":"hello"}'),
        encode_frame(Channel.CONSOLE, b"echo hi\n"),
        encode_frame(Channel.EVENT, b'{"event":"x"}'),
        encode_frame(Channel.CONTROL, b""),
        encode_frame(Channel.CONSOLE, b"RH" * 40),
    ])

    environment = dict(
        os.environ,
        PYTHON_FRAMES=python_frames.hex(),
        MODEM_STREAMS=",".join(s.hex() for s in modem_streams()),
    )
    run_result = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=120, env=environment
    )
    if run_result.returncode != 0:
        pytest.fail(f"parity program failed:\n{run_result.stderr[:2000]}")

    return json.loads(run_result.stdout)


# --- Link framing ---------------------------------------------------------


@requires_swift
def test_swift_frames_match_python_frames(swift_results):
    """The same payload must produce identical bytes on both sides."""
    cases = [
        (Channel.CONTROL, bytes.fromhex("7b226d6574686f64223a2268656c6c6f227d")),
        (Channel.CONSOLE, bytes.fromhex("6c73202d6c610a")),
        (Channel.EVENT, bytes.fromhex("7b226576656e74223a2274657374227d")),
        (Channel.CONTROL, b""),
        (Channel.CONSOLE, bytes.fromhex("ab" * 1000)),
        (Channel.CONSOLE, bytes.fromhex("5248" * 50)),
    ]

    for index, (channel, payload) in enumerate(cases):
        expected = encode_frame(channel, payload).hex()
        assert swift_results["linkFrames"][index] == expected, (
            f"frame {index} differs between the two implementations"
        )


@requires_swift
def test_python_frames_decode_in_swift(swift_results):
    """
    Swift decodes Python's frames, fed one byte at a time -- the realistic
    serial case and the path most likely to be wrong.
    """
    expected = [
        (int(Channel.CONTROL), b'{"id":1,"method":"hello"}'),
        (int(Channel.CONSOLE), b"echo hi\n"),
        (int(Channel.EVENT), b'{"event":"x"}'),
        (int(Channel.CONTROL), b""),
        (int(Channel.CONSOLE), b"RH" * 40),
    ]

    decoded = swift_results["linkDecoded"]
    assert len(decoded) == len(expected), (
        f"Swift decoded {len(decoded)} frames, expected {len(expected)}"
    )

    for entry, (channel, payload) in zip(decoded, expected):
        assert int(entry["channel"]) == channel
        assert entry["payload"] == payload.hex()


@requires_swift
def test_swift_frames_decode_in_python(swift_results):
    """The reverse direction: Python must accept what Swift produced."""
    decoder = FrameDecoder()
    frames = []
    for frame_hex in swift_results["linkFrames"]:
        frames.extend(decoder.feed(bytes.fromhex(frame_hex)))

    assert len(frames) == len(swift_results["linkFrames"])


# --- Node identity --------------------------------------------------------


@requires_swift
def test_node_id_derivation_matches(swift_results):
    """
    If these disagree, the app never recognises the balloon's beacons and the
    entire Meshtastic map path is silently dead.
    """
    for key, swift_id in swift_results["nodeIDs"].items():
        callsign, payload_id = key.rsplit("#", 1)
        expected = node_id_from_callsign(callsign, int(payload_id))
        assert swift_id == expected, (
            f"node id for {key!r}: Swift {swift_id:#010x}, "
            f"Python {expected:#010x}"
        )


# --- Encryption -----------------------------------------------------------


@requires_swift
def test_aes_ctr_matches(swift_results):
    """
    Multi-block cases are what matter: the counter must increment as a
    big-endian 128-bit integer to match mbedtls. A little-endian word counter
    produces a correct first block and garbage after it.
    """
    key = bytes.fromhex(
        "603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4"
    )
    cases = [
        (1, 0xDEADBEEF, bytes.fromhex("48656c6c6f")),
        (0xCAFEBABE, 0x12345678, bytes(64)),
        (42, 0x1000, b"\xff" * 100),
    ]

    for index, (packet_id, sender, plaintext) in enumerate(cases):
        expected = aes_ctr(key, build_nonce(packet_id, sender), plaintext).hex()
        assert swift_results["aesCTR"][index] == expected, (
            f"AES-CTR case {index} ({len(plaintext)} bytes) differs"
        )


@requires_swift
def test_channel_hash_matches(swift_results):
    key = expand_psk(b"\x01")
    for name, swift_hash in swift_results["channelHashes"].items():
        assert swift_hash == channel_hash(name, key), f"channel hash for {name!r}"


# --- Whole packets --------------------------------------------------------


@requires_swift
def test_meshtastic_packet_matches(swift_results):
    """A complete encrypted packet, header and all."""
    key = expand_psk(b"\x01")
    expected = build_packet(
        # Swift wraps the text in a Data envelope; Python's build_packet takes
        # the already-wrapped payload, so build the same envelope here.
        payload=_data_envelope(1, b"hello from the stratosphere"),
        sender=0xEFCAB5AC,
        destination=0xFFFFFFFF,
        channel_key=key,
        channel_hash=channel_hash("LongFast", key),
        hop_limit=0,
        packet_id=0x11223344,
    )
    assert swift_results["meshPackets"][0] == expected.hex()


def _data_envelope(portnum: int, payload: bytes) -> bytes:
    from common.meshtastic.protobuf import ProtobufWriter

    writer = ProtobufWriter()
    writer.enum(1, portnum, force=True)
    writer.bytes(2, payload, force=True)
    return writer.to_bytes()


# --- Modem framing --------------------------------------------------------


def _expected_payloads():
    from tests.test_frame_resync import mixed_stream
    _, packets = mixed_stream(count=30, seed=2)
    return [p.hex() for p in packets]


@requires_swift
def test_swift_scanner_recovers_every_frame(swift_results):
    """A clean stream: every packet Python framed comes back byte for byte."""
    assert swift_results["scannerRuns"][0]["payloads"] == _expected_payloads()


@requires_swift
def test_swift_scanner_survives_a_one_byte_slip(swift_results):
    """Frames are delimited at both ends, so a slip flips delimiter parity.
    Without recovery the scanner discards everything from here on."""
    payloads = swift_results["scannerRuns"][1]["payloads"]
    expected = _expected_payloads()
    assert len(payloads) >= len(expected) - 2
    assert payloads == expected[len(expected) - len(payloads):]


@requires_swift
def test_swift_scanner_separates_status_text_from_frames(swift_results):
    """0x0A is an ordinary byte inside a frame. Splitting the stream on
    newlines to find status lines deletes frame bytes -- it took a modem
    forwarding 27,000 packets down to 64 received."""
    run = swift_results["scannerRuns"][2]
    assert run["payloads"] == _expected_payloads()
    assert run["text"] == ["[READY] Listening for packets...",
                           "[RADIO] Reconfiguring for new settings..."]


@requires_swift
def test_swift_scanner_invents_nothing_from_noise(swift_results):
    assert swift_results["scannerRuns"][3]["payloads"] == []
