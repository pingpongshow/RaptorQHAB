"""Packet framing, CRC, and payload serialization round-trips."""

import struct

import pytest

from common.constants import (
    CRC_SIZE,
    HEADER_SIZE,
    MAX_PAYLOAD_SIZE,
    SYNC_WORD,
    TELEMETRY_PAYLOAD_SIZE,
    FixType,
    PacketFlags,
    PacketType,
)
from common.crc import crc32, crc32_bytes, verify_crc32_packet
from common.protocol import (
    CommandAckPayload,
    ImageDataPayload,
    ImageMetaPayload,
    PacketHeader,
    TelemetryPayload,
    TextMessagePayload,
    build_packet,
    parse_packet,
    parse_packet_full,
)


# --- CRC ------------------------------------------------------------------


def test_crc32_matches_zlib():
    """Our table-driven CRC must agree with the standard IEEE 802.3 CRC-32."""
    import zlib

    for data in (b"", b"a", b"RaptorHAB", bytes(range(256)), b"\x00" * 1000):
        assert crc32(data) == zlib.crc32(data) & 0xFFFFFFFF


def test_crc32_bytes_is_big_endian():
    data = b"RaptorHAB"
    assert crc32_bytes(data) == struct.pack(">I", crc32(data))


def test_verify_crc32_packet_detects_single_bit_flip():
    packet = build_packet(PacketType.TELEMETRY, 1, TelemetryPayload())
    assert verify_crc32_packet(packet)

    corrupted = bytearray(packet)
    corrupted[10] ^= 0x01
    assert not verify_crc32_packet(bytes(corrupted))


def test_verify_crc32_packet_rejects_runt():
    assert not verify_crc32_packet(b"\x00\x01")


# --- Header ---------------------------------------------------------------


def test_header_round_trip():
    header = PacketHeader(PacketType.IMAGE_DATA, 4242, PacketFlags.URGENT)
    restored = PacketHeader.deserialize(header.serialize())
    assert restored == header


def test_header_size_constant_matches_reality():
    header = PacketHeader(PacketType.TELEMETRY, 0, 0)
    assert len(SYNC_WORD) + len(header.serialize()) == HEADER_SIZE


# --- Telemetry payload ----------------------------------------------------


def test_telemetry_payload_is_fixed_size():
    assert len(TelemetryPayload().serialize()) == TELEMETRY_PAYLOAD_SIZE


def test_telemetry_round_trip_preserves_values():
    original = TelemetryPayload(
        latitude=39.7392,
        longitude=-104.9903,
        altitude=30480.0,
        speed=12.5,
        heading=271.25,
        satellites=11,
        fix_type=FixType.FIX_3D,
        gps_time=1755400000,
        battery_mv=4150,
        cpu_temp=41.5,
        radio_temp=-12.25,
        image_id=77,
        image_progress=63,
        rssi=-97,
    )
    restored = TelemetryPayload.deserialize(original.serialize())

    assert restored.latitude == pytest.approx(original.latitude, abs=1e-6)
    assert restored.longitude == pytest.approx(original.longitude, abs=1e-6)
    assert restored.altitude == pytest.approx(original.altitude, abs=0.01)
    assert restored.speed == pytest.approx(original.speed, abs=0.01)
    assert restored.heading == pytest.approx(original.heading, abs=0.01)
    assert restored.satellites == original.satellites
    assert restored.fix_type == original.fix_type
    assert restored.gps_time == original.gps_time
    assert restored.battery_mv == original.battery_mv
    assert restored.cpu_temp == pytest.approx(original.cpu_temp, abs=0.01)
    assert restored.radio_temp == pytest.approx(original.radio_temp, abs=0.01)
    assert restored.image_id == original.image_id
    assert restored.image_progress == original.image_progress
    assert restored.rssi == original.rssi


def test_telemetry_clamps_out_of_range_rather_than_raising():
    """A wild GPS reading must not crash the transmit path."""
    payload = TelemetryPayload(latitude=1e9, longitude=-1e9, speed=1e9, rssi=-9999)
    encoded = payload.serialize()
    assert len(encoded) == TELEMETRY_PAYLOAD_SIZE
    TelemetryPayload.deserialize(encoded)


def test_telemetry_negative_altitude_clamps_to_zero():
    """
    Documents a known limitation: altitude is encoded unsigned, so a landing
    site below sea level (or GPS noise near zero) reads back as 0 m.
    """
    restored = TelemetryPayload.deserialize(
        TelemetryPayload(altitude=-50.0).serialize()
    )
    assert restored.altitude == 0.0


# --- Image payloads -------------------------------------------------------


def test_image_meta_round_trip():
    original = ImageMetaPayload(
        image_id=12,
        total_size=48000,
        symbol_size=200,
        num_source_symbols=240,
        checksum=0xDEADBEEF,
        width=1280,
        height=960,
        timestamp=1755400000,
    )
    assert ImageMetaPayload.deserialize(original.serialize()) == original


def test_image_meta_serialized_length_matches_parser_expectation():
    """The receive path hardcodes 22 bytes for IMAGE_META."""
    assert len(ImageMetaPayload().serialize()) == 22


def test_image_data_round_trip():
    original = ImageDataPayload(image_id=7, symbol_id=123456, symbol_data=bytes(range(200)))
    assert ImageDataPayload.deserialize(original.serialize()) == original


def test_image_data_fits_max_payload():
    """symbol_size 200 plus the 6-byte header must fit the 243-byte maximum."""
    payload = ImageDataPayload(image_id=1, symbol_id=1, symbol_data=b"\xAA" * 200)
    assert len(payload.serialize()) <= MAX_PAYLOAD_SIZE


# --- Command ack ----------------------------------------------------------


def test_command_ack_round_trip():
    original = CommandAckPayload(
        acked_type=PacketType.CMD_SETPARAM, acked_seq=99, status=0, data=b"\x01\x02"
    )
    assert CommandAckPayload.from_bytes(original.to_bytes()) == original


# --- Whole-packet framing -------------------------------------------------


def test_build_and_parse_telemetry_packet():
    payload = TelemetryPayload(latitude=39.0, longitude=-105.0, altitude=1000.0)
    packet = build_packet(PacketType.TELEMETRY, 512, payload)

    assert packet.startswith(SYNC_WORD)
    assert len(packet) == HEADER_SIZE + TELEMETRY_PAYLOAD_SIZE + CRC_SIZE

    parsed = parse_packet(packet)
    assert parsed is not None
    packet_type, sequence, flags, payload_bytes = parsed
    assert packet_type == PacketType.TELEMETRY
    assert sequence == 512
    assert flags == PacketFlags.NONE
    assert TelemetryPayload.deserialize(payload_bytes).altitude == pytest.approx(1000.0)


def test_parse_packet_tolerates_sx1262_padding():
    """The radio returns a padded buffer; the parser must find the real end."""
    packet = build_packet(PacketType.TELEMETRY, 3, TelemetryPayload())
    padded = packet + b"\x00" * (255 - len(packet))

    parsed = parse_packet(padded)
    assert parsed is not None
    assert parsed[0] == PacketType.TELEMETRY
    assert parsed[1] == 3


def test_parse_raptorq_image_data_packet():
    """
    Regression: the parser assumed an IMAGE_DATA symbol was exactly
    FOUNTAIN_SYMBOL_SIZE bytes, but RaptorQ prefixes each symbol with a
    4-byte payload ID. Every real image packet failed the primary CRC check
    and survived only via a fallback that assumed an unpadded buffer.
    """
    from common.constants import FOUNTAIN_SYMBOL_SIZE, RAPTORQ_PAYLOAD_ID_SIZE

    symbol = bytes(range(256))[: FOUNTAIN_SYMBOL_SIZE + RAPTORQ_PAYLOAD_ID_SIZE]
    payload = ImageDataPayload(image_id=9, symbol_id=1234, symbol_data=symbol)
    packet = build_packet(PacketType.IMAGE_DATA, 100, payload)

    parsed = parse_packet(packet)
    assert parsed is not None
    assert ImageDataPayload.deserialize(parsed[3]).symbol_data == symbol


def test_parse_raptorq_image_data_packet_with_padding():
    """The same packet, padded by the radio to the full 255-byte buffer."""
    from common.constants import FOUNTAIN_SYMBOL_SIZE, RAPTORQ_PAYLOAD_ID_SIZE

    symbol = bytes(range(256))[: FOUNTAIN_SYMBOL_SIZE + RAPTORQ_PAYLOAD_ID_SIZE]
    payload = ImageDataPayload(image_id=9, symbol_id=1234, symbol_data=symbol)
    packet = build_packet(PacketType.IMAGE_DATA, 100, payload)
    padded = packet + b"\x00" * (255 - len(packet))

    parsed = parse_packet(padded)
    assert parsed is not None, "padded RaptorQ image packet must still parse"
    assert ImageDataPayload.deserialize(parsed[3]).symbol_data == symbol


def test_parse_lt_image_data_packet_still_works():
    """The shorter LT symbol length remains a valid candidate."""
    from common.constants import FOUNTAIN_SYMBOL_SIZE

    symbol = b"\xA5" * FOUNTAIN_SYMBOL_SIZE
    payload = ImageDataPayload(image_id=9, symbol_id=1, symbol_data=symbol)
    packet = build_packet(PacketType.IMAGE_DATA, 1, payload)
    padded = packet + b"\x00" * (255 - len(packet))

    parsed = parse_packet(padded)
    assert parsed is not None
    assert ImageDataPayload.deserialize(parsed[3]).symbol_data == symbol


def test_parse_packet_rejects_bad_sync_word():
    packet = bytearray(build_packet(PacketType.TELEMETRY, 1, TelemetryPayload()))
    packet[0] ^= 0xFF
    assert parse_packet(bytes(packet)) is None


def test_parse_packet_rejects_corrupt_crc():
    packet = bytearray(build_packet(PacketType.TELEMETRY, 1, TelemetryPayload()))
    packet[-1] ^= 0xFF
    assert parse_packet(bytes(packet)) is None


def test_parse_packet_rejects_truncated_input():
    packet = build_packet(PacketType.TELEMETRY, 1, TelemetryPayload())
    assert parse_packet(packet[:6]) is None
    assert parse_packet(b"") is None


def test_build_packet_rejects_oversized_payload():
    with pytest.raises(ValueError):
        build_packet(PacketType.TEXT_MSG, 0, b"\x00" * (MAX_PAYLOAD_SIZE + 1))


def test_build_packet_sequence_wraps_within_uint16():
    packet = build_packet(PacketType.TELEMETRY, 65535, TelemetryPayload())
    parsed = parse_packet(packet)
    assert parsed is not None and parsed[1] == 65535


def test_parse_packet_full_returns_deserialized_payload():
    """Regression: parse_packet_full used to unpack a 4-tuple into 2 names."""
    original = TelemetryPayload(latitude=39.0, longitude=-105.0)
    packet = build_packet(PacketType.TELEMETRY, 8, original)

    result = parse_packet_full(packet)
    assert result is not None

    header, payload = result
    assert isinstance(header, PacketHeader)
    assert header.sequence == 8
    assert isinstance(payload, TelemetryPayload)
    assert payload.latitude == pytest.approx(39.0, abs=1e-6)


def test_parse_packet_full_returns_none_on_garbage():
    assert parse_packet_full(b"not a packet at all") is None


def test_text_message_round_trip_through_packet():
    original = TextMessagePayload(message="RaptorHAB airborne — hello")
    packet = build_packet(PacketType.TEXT_MSG, 1, original)
    parsed = parse_packet(packet)
    assert parsed is not None
    assert TextMessagePayload.deserialize(parsed[3]).message == original.message
