"""
Protobuf encoding, Meshtastic packet framing, and message round-trips.
"""

import struct

import pytest

from common.meshtastic.messages import (
    HardwareModel,
    PortNum,
    build_data,
    build_device_metrics,
    build_position,
    build_telemetry,
    build_text_message,
    build_user,
    parse_data,
    parse_device_metrics,
    parse_position,
    parse_text_message,
    parse_user,
)
from common.meshtastic.crypto import channel_hash, expand_psk, generate_psk
from common.meshtastic.packet import (
    BROADCAST_ADDR,
    HEADER_SIZE,
    MAX_PAYLOAD_SIZE,
    MeshHeader,
    MeshPacket,
    MeshPacketError,
    build_packet,
    generate_packet_id,
    node_id_from_callsign,
    node_id_to_string,
    parse_packet,
    short_name_from_callsign,
)
from common.meshtastic.protobuf import (
    ProtobufError,
    ProtobufReader,
    ProtobufWriter,
    decode_varint,
    encode_varint,
)


# --- protobuf primitives --------------------------------------------------


@pytest.mark.parametrize(
    "value,encoded",
    [
        (0, "00"), (1, "01"), (127, "7f"), (128, "8001"),
        (300, "ac02"), (16383, "ff7f"), (16384, "808001"),
    ],
)
def test_varint_encoding_matches_the_spec(value, encoded):
    assert encode_varint(value).hex() == encoded


def test_varint_round_trip():
    for value in (0, 1, 127, 128, 255, 65535, 2**31, 2**63 - 1):
        assert decode_varint(encode_varint(value))[0] == value


def test_varint_rejects_negative_input():
    with pytest.raises(ValueError, match="non-negative"):
        encode_varint(-1)


def test_decode_varint_rejects_truncated_input():
    with pytest.raises(ProtobufError, match="truncated"):
        decode_varint(b"\x80\x80\x80")


def test_decode_varint_rejects_overlong_input():
    with pytest.raises(ProtobufError, match="exceeds 64 bits"):
        decode_varint(b"\x80" * 12)


def test_proto3_defaults_are_omitted():
    """Keeps beacons small on a link running at about a kilobit per second."""
    writer = ProtobufWriter()
    writer.uint32(1, 0).bool(2, False).string(3, "").bytes(4, b"")
    assert writer.to_bytes() == b""


def test_defaults_can_be_forced():
    assert ProtobufWriter().uint32(1, 0, force=True).to_bytes() == b"\x08\x00"


def test_negative_int32_is_sign_extended():
    """Protobuf encodes a negative int32 as a ten-byte sign-extended varint."""
    encoded = ProtobufWriter().int32(1, -1, force=True).to_bytes()
    assert len(encoded) == 11  # 1 key byte + 10 varint bytes


def test_writer_rejects_out_of_range_values():
    with pytest.raises(ValueError, match="out of range"):
        ProtobufWriter().uint32(1, 2**32)
    with pytest.raises(ValueError, match="out of range"):
        ProtobufWriter().int32(1, 2**31)


def test_writer_rejects_field_number_zero():
    with pytest.raises(ValueError, match="field number"):
        ProtobufWriter().uint32(0, 1)


def test_reader_round_trips_mixed_field_types():
    writer = ProtobufWriter()
    writer.uint32(1, 42).string(2, "RaptorHAB").float(3, 1.5).bytes(4, b"\xDE\xAD")

    fields = ProtobufReader(writer.to_bytes()).to_dict()
    assert fields[1][-1] == 42
    assert fields[2][-1] == b"RaptorHAB"
    assert struct.unpack("<f", fields[3][-1])[0] == 1.5
    assert fields[4][-1] == b"\xDE\xAD"


def test_reader_tolerates_unknown_fields():
    """A beacon from newer firmware must still parse."""
    writer = ProtobufWriter()
    writer.uint32(1, 7).string(99, "field from the future")
    fields = ProtobufReader(writer.to_bytes()).to_dict()
    assert fields[1][-1] == 7
    assert 99 in fields


def test_reader_rejects_truncated_length_delimited_field():
    with pytest.raises(ProtobufError, match="truncated"):
        ProtobufReader(b"\x12\x10short").to_dict()


def test_reader_rejects_group_wire_types():
    with pytest.raises(ProtobufError, match="unsupported wire type"):
        ProtobufReader(b"\x0b").to_dict()


# --- node identity --------------------------------------------------------


def test_node_id_is_deterministic():
    """Stable across reflashes, so receivers keep a continuous history."""
    assert node_id_from_callsign("RPHAB1", 1) == node_id_from_callsign("RPHAB1", 1)


def test_node_id_varies_by_callsign_and_payload():
    assert node_id_from_callsign("RPHAB1", 1) != node_id_from_callsign("RPHAB2", 1)
    assert node_id_from_callsign("RPHAB1", 1) != node_id_from_callsign("RPHAB1", 2)


def test_node_id_is_case_and_whitespace_insensitive():
    assert node_id_from_callsign(" rphab1 ", 1) == node_id_from_callsign("RPHAB1", 1)


def test_node_id_avoids_reserved_values():
    for callsign in ("A", "RPHAB1", "ZZZZZZ", "test", "0"):
        for payload in range(6):
            node_id = node_id_from_callsign(callsign, payload)
            assert node_id >= 0x1000
            assert node_id != BROADCAST_ADDR


def test_node_id_string_format():
    assert node_id_to_string(0xA1B2C3D4) == "!a1b2c3d4"
    assert node_id_to_string(1) == "!00000001"


def test_short_name_is_at_most_four_characters():
    assert short_name_from_callsign("RPHAB1") == "HAB1"
    assert short_name_from_callsign("AB") == "AB"
    assert short_name_from_callsign("!!!") == "HAB"


# --- packet header --------------------------------------------------------


def test_header_is_exactly_sixteen_bytes():
    assert len(MeshHeader().serialize()) == HEADER_SIZE


def test_header_round_trip():
    header = MeshHeader(
        destination=0x11223344,
        sender=0xAABBCCDD,
        packet_id=0x01020304,
        hop_limit=3,
        want_ack=True,
        via_mqtt=True,
        hop_start=5,
        channel_hash=0x08,
        next_hop=0x42,
        relay_node=0x99,
    )
    assert MeshHeader.deserialize(header.serialize()) == header


def test_header_flag_packing():
    header = MeshHeader(hop_limit=3, want_ack=True, via_mqtt=False, hop_start=7)
    flags = header.serialize()[12]
    assert flags & 0x07 == 3
    assert flags & 0x08
    assert not flags & 0x10
    assert (flags & 0xE0) >> 5 == 7


def test_header_rejects_out_of_range_hop_values():
    with pytest.raises(ValueError, match="hop_limit"):
        MeshHeader(hop_limit=8)
    with pytest.raises(ValueError, match="hop_start"):
        MeshHeader(hop_start=9)


def test_header_deserialize_rejects_short_input():
    with pytest.raises(MeshPacketError, match="header needs"):
        MeshHeader.deserialize(b"\x00" * 15)


def test_broadcast_detection():
    assert MeshHeader(destination=BROADCAST_ADDR).is_broadcast
    assert not MeshHeader(destination=0x1234).is_broadcast


# --- whole packets --------------------------------------------------------


def test_encrypted_packet_round_trip():
    key = expand_psk(b"\x01")
    sender = node_id_from_callsign("RPHAB1", 1)
    payload = build_data(PortNum.TEXT_MESSAGE_APP, b"hello from 30 km up")

    packet = build_packet(payload, sender=sender, channel_key=key, channel_hash=0x08)
    parsed = parse_packet(packet, channel_key=key)

    assert parsed.header.sender == sender
    assert parsed.header.is_broadcast
    assert parsed.payload == payload


def test_payload_is_actually_encrypted_on_the_wire():
    key = generate_psk(32)
    payload = build_data(PortNum.TEXT_MESSAGE_APP, b"sensitive")
    packet = build_packet(payload, sender=1, channel_key=key)
    assert payload not in packet


def test_unencrypted_packet_when_no_key():
    payload = build_data(PortNum.TEXT_MESSAGE_APP, b"in the clear")
    packet = build_packet(payload, sender=1, channel_key=b"")
    assert packet[HEADER_SIZE:] == payload


def test_hop_limit_zero_is_the_default():
    """
    From altitude the balloon reaches an enormous number of nodes; letting
    them rebroadcast could congest whole regional meshes.
    """
    parsed = parse_packet(build_packet(b"\x08\x01", sender=1))
    assert parsed.header.hop_limit == 0
    assert parsed.header.hop_start == 0


def test_hop_start_mirrors_hop_limit_on_a_new_packet():
    parsed = parse_packet(build_packet(b"\x08\x01", sender=1, hop_limit=3))
    assert parsed.header.hop_limit == 3
    assert parsed.header.hop_start == 3


def test_oversized_payload_is_rejected():
    with pytest.raises(MeshPacketError, match="maximum after"):
        build_packet(b"\x00" * (MAX_PAYLOAD_SIZE + 1), sender=1)


def test_maximum_payload_is_accepted():
    packet = build_packet(b"\x00" * MAX_PAYLOAD_SIZE, sender=1)
    assert len(packet) == 255


def test_packet_ids_are_unique():
    assert len({generate_packet_id() for _ in range(500)}) == 500


def test_explicit_packet_id_is_used():
    parsed = parse_packet(build_packet(b"\x08\x01", sender=1, packet_id=0xCAFEBABE))
    assert parsed.header.packet_id == 0xCAFEBABE


def test_decryption_with_the_wrong_key_yields_garbage_not_an_error():
    """
    AES-CTR cannot detect a wrong key. Callers must validate the decrypted
    bytes as a protobuf rather than assuming success.
    """
    payload = build_data(PortNum.TEXT_MESSAGE_APP, b"secret")
    packet = build_packet(payload, sender=1, channel_key=generate_psk(32))
    parsed = parse_packet(packet, channel_key=generate_psk(32))
    assert parsed.payload != payload


# --- application messages -------------------------------------------------


def test_position_round_trip():
    encoded = build_position(
        latitude=39.7392,
        longitude=-104.9903,
        altitude_m=30480.0,
        timestamp=1755400000,
        satellites=11,
        ground_speed_mps=12.0,
        ground_track_deg=271.5,
    )
    position = parse_position(encoded)

    assert position.latitude == pytest.approx(39.7392, abs=1e-6)
    assert position.longitude == pytest.approx(-104.9903, abs=1e-6)
    assert position.altitude_m == 30480
    assert position.timestamp == 1755400000
    assert position.satellites == 11
    assert position.ground_speed_mps == 12
    assert position.ground_track_deg == pytest.approx(271.5, abs=0.001)


def test_position_handles_southern_and_western_hemispheres():
    position = parse_position(build_position(-33.8688, 151.2093))
    assert position.latitude == pytest.approx(-33.8688, abs=1e-6)
    assert position.longitude == pytest.approx(151.2093, abs=1e-6)

    position = parse_position(build_position(-23.5505, -46.6333))
    assert position.latitude == pytest.approx(-23.5505, abs=1e-6)
    assert position.longitude == pytest.approx(-46.6333, abs=1e-6)


def test_position_fits_comfortably_in_one_lora_packet():
    encoded = build_position(39.7392, -104.9903, 30480.0, satellites=11)
    packet = build_packet(
        build_data(PortNum.POSITION_APP, encoded), sender=1, channel_key=expand_psk(b"\x01")
    )
    assert len(packet) < 100


def test_device_metrics_round_trip():
    metrics = parse_device_metrics(
        build_device_metrics(
            battery_level=87, voltage=4.15, uptime_seconds=3600
        )
    )
    assert metrics.battery_level == 87
    assert metrics.voltage == pytest.approx(4.15, abs=0.001)
    assert metrics.uptime_seconds == 3600


def test_battery_level_is_clamped_to_the_protocol_range():
    """101 means "plugged in" in Meshtastic; anything above is invalid."""
    assert parse_device_metrics(build_device_metrics(battery_level=150)).battery_level == 101


def test_telemetry_wraps_device_metrics():
    encoded = build_telemetry(
        device_metrics=build_device_metrics(battery_level=50), timestamp=1755400000
    )
    fields = ProtobufReader(encoded).to_dict()
    assert 2 in fields, "device_metrics should be field 2"
    assert parse_device_metrics(fields[2][-1]).battery_level == 50


def test_user_round_trip():
    user = parse_user(
        build_user(
            node_id="!a1b2c3d4",
            long_name="RaptorHAB RPHAB1",
            short_name="HAB1",
            hw_model=HardwareModel.PRIVATE_HW,
        )
    )
    assert user.node_id == "!a1b2c3d4"
    assert user.long_name == "RaptorHAB RPHAB1"
    assert user.short_name == "HAB1"
    assert user.hw_model == HardwareModel.PRIVATE_HW


def test_user_truncates_overlong_names():
    user = parse_user(build_user("!1", "x" * 100, "TOOLONG"))
    assert len(user.long_name) <= 39
    assert len(user.short_name) <= 4


def test_data_envelope_round_trip():
    data = parse_data(build_data(PortNum.POSITION_APP, b"\x01\x02\x03"))
    assert data.portnum == PortNum.POSITION_APP
    assert data.payload == b"\x01\x02\x03"


def test_text_message_round_trip():
    assert parse_text_message(build_text_message("Hello from the stratosphere")) == (
        "Hello from the stratosphere"
    )


def test_text_message_truncates_on_a_character_boundary():
    """
    Slicing raw bytes would split a multi-byte character and render as a
    replacement glyph on the receiver.
    """
    text = "🎈" * 100  # four bytes each
    truncated = build_text_message(text, max_bytes=50)
    assert len(truncated) <= 50
    decoded = truncated.decode("utf-8")  # must not raise
    assert "�" not in decoded


def test_text_message_preserves_short_unicode():
    text = "Ascenso — 30 km 🎈"
    assert parse_text_message(build_text_message(text)) == text
