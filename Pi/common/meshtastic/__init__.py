"""
Minimal, dependency-free Meshtastic implementation for the RaptorHab payload.

Covers exactly what the balloon needs: build encrypted Meshtastic packets,
parse received ones, and pick the right regional frequency and power ceiling
for wherever the balloon currently is.

The official `meshtastic` Python package would bring the full protobuf runtime
and generated schema, which is more weight and startup cost than a Pi Zero
should carry for six message types.
"""

from .crypto import (
    DEFAULT_PSK,
    channel_hash,
    decrypt_payload,
    encrypt_payload,
    expand_psk,
    format_psk_fingerprint,
    generate_psk,
    parse_psk,
)
from .messages import (
    PortNum,
    build_data,
    build_device_metrics,
    build_position,
    build_telemetry,
    build_text_message,
    build_user,
    parse_data,
    parse_position,
    parse_user,
)
from .packet import (
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
from .regions import (
    DEFAULT_BANDWIDTH_KHZ,
    DEFAULT_CHANNEL_NAME,
    DEFAULT_REGION_CODE,
    REGIONS,
    REGIONS_BY_CODE,
    SUB_GHZ_REGION_CODES,
    Region,
    clamp_power_to_region,
    frequency_for_channel,
    get_region,
    region_for_position,
)

__all__ = [
    # crypto
    "DEFAULT_PSK",
    "channel_hash",
    "decrypt_payload",
    "encrypt_payload",
    "expand_psk",
    "format_psk_fingerprint",
    "generate_psk",
    "parse_psk",
    # messages
    "PortNum",
    "build_data",
    "build_device_metrics",
    "build_position",
    "build_telemetry",
    "build_text_message",
    "build_user",
    "parse_data",
    "parse_position",
    "parse_user",
    # packet
    "BROADCAST_ADDR",
    "HEADER_SIZE",
    "MAX_PAYLOAD_SIZE",
    "MeshHeader",
    "MeshPacket",
    "MeshPacketError",
    "build_packet",
    "generate_packet_id",
    "node_id_from_callsign",
    "node_id_to_string",
    "parse_packet",
    "short_name_from_callsign",
    # regions
    "DEFAULT_BANDWIDTH_KHZ",
    "DEFAULT_CHANNEL_NAME",
    "DEFAULT_REGION_CODE",
    "REGIONS",
    "REGIONS_BY_CODE",
    "SUB_GHZ_REGION_CODES",
    "Region",
    "clamp_power_to_region",
    "frequency_for_channel",
    "get_region",
    "region_for_position",
]
