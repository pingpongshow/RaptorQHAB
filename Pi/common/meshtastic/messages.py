"""
Meshtastic application messages.

Everything a Meshtastic node sends is wrapped in a `Data` protobuf carrying a
port number and an opaque payload. This module builds the handful of message
types the balloon needs, hand-encoded against the wire format rather than
generated from the .proto files.

Field numbers are from the Meshtastic protobuf definitions
(meshtastic/mesh.proto, meshtastic/telemetry.proto). They are part of the wire
contract and cannot change without breaking interoperability, so they are
written out explicitly here rather than hidden behind a code generator.
"""

import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

from .protobuf import ProtobufReader, ProtobufWriter, as_float, as_int32, as_sfixed32

logger = logging.getLogger(__name__)


class PortNum(IntEnum):
    """Meshtastic application port numbers (portnums.proto)."""

    UNKNOWN_APP = 0
    TEXT_MESSAGE_APP = 1
    POSITION_APP = 3
    NODEINFO_APP = 4
    ROUTING_APP = 5
    ADMIN_APP = 6
    TELEMETRY_APP = 67
    PRIVATE_APP = 256


class HardwareModel(IntEnum):
    """Subset of HardwareModel (mesh.proto)."""

    UNSET = 0
    PRIVATE_HW = 255


# --------------------------------------------------------------------------
# Data envelope
# --------------------------------------------------------------------------


def build_data(
    portnum: PortNum,
    payload: bytes,
    want_response: bool = False,
    reply_id: int = 0,
    request_id: int = 0,
    bitfield: Optional[int] = None,
) -> bytes:
    """
    Wrap an application payload in the Meshtastic `Data` protobuf.

    Data field numbers (mesh.proto):
        1 portnum, 2 payload, 3 want_response, 4 dest, 5 source,
        6 request_id, 7 reply_id, 8 emoji, 9 bitfield
    """
    writer = ProtobufWriter()
    writer.enum(1, int(portnum), force=True)
    writer.bytes(2, payload, force=True)
    writer.bool(3, want_response)
    writer.uint32(6, request_id)
    writer.uint32(7, reply_id)
    if bitfield is not None:
        writer.uint32(9, bitfield)
    return writer.to_bytes()


@dataclass
class DecodedData:
    """A parsed Data envelope."""

    portnum: int = 0
    payload: bytes = b""
    want_response: bool = False
    request_id: int = 0
    reply_id: int = 0


def parse_data(raw: bytes) -> DecodedData:
    """Parse a Data protobuf. Raises ProtobufError on malformed input."""
    fields = ProtobufReader(raw).to_dict()
    return DecodedData(
        portnum=fields.get(1, [0])[-1],
        payload=fields.get(2, [b""])[-1],
        want_response=bool(fields.get(3, [0])[-1]),
        request_id=fields.get(6, [0])[-1],
        reply_id=fields.get(7, [0])[-1],
    )


# --------------------------------------------------------------------------
# Position
# --------------------------------------------------------------------------


def build_position(
    latitude: float,
    longitude: float,
    altitude_m: float = 0.0,
    timestamp: Optional[int] = None,
    satellites: int = 0,
    ground_speed_mps: float = 0.0,
    ground_track_deg: float = 0.0,
    precision_bits: int = 32,
) -> bytes:
    """
    Build a Position protobuf.

    Position field numbers (mesh.proto):
        1 latitude_i (sfixed32, degrees * 1e7)
        2 longitude_i (sfixed32, degrees * 1e7)
        3 altitude (int32, metres above MSL)
        4 time (fixed32, unix seconds)
        9 timestamp
        10 timestamp_millis_adjust
        17 sats_in_view
        20 ground_speed (uint32, m/s)
        21 ground_track (uint32, degrees * 1e5)
        22 precision_bits
    """
    writer = ProtobufWriter()

    writer.sfixed32(1, _degrees_to_i(latitude), force=True)
    writer.sfixed32(2, _degrees_to_i(longitude), force=True)
    writer.int32(3, int(round(altitude_m)))
    writer.fixed32(4, int(timestamp if timestamp is not None else time.time()))
    writer.uint32(17, max(0, min(255, int(satellites))))
    writer.uint32(20, max(0, int(round(ground_speed_mps))))
    writer.uint32(21, max(0, min(35999999, int(round(ground_track_deg * 1e5)))))
    writer.uint32(22, max(0, min(32, int(precision_bits))))

    return writer.to_bytes()


@dataclass
class DecodedPosition:
    latitude: float = 0.0
    longitude: float = 0.0
    altitude_m: int = 0
    timestamp: int = 0
    satellites: int = 0
    ground_speed_mps: int = 0
    ground_track_deg: float = 0.0


def parse_position(raw: bytes) -> DecodedPosition:
    """Parse a Position protobuf."""
    fields = ProtobufReader(raw).to_dict()

    def sfixed(number):
        values = fields.get(number)
        return as_sfixed32(values[-1]) if values else 0

    def varint(number):
        values = fields.get(number)
        return values[-1] if values else 0

    track = varint(21)
    return DecodedPosition(
        latitude=sfixed(1) / 1e7,
        longitude=sfixed(2) / 1e7,
        altitude_m=as_int32(varint(3)),
        timestamp=(
            int.from_bytes(fields[4][-1], "little") if 4 in fields else 0
        ),
        satellites=varint(17),
        ground_speed_mps=varint(20),
        ground_track_deg=track / 1e5,
    )


def _degrees_to_i(degrees: float) -> int:
    """Degrees to Meshtastic's scaled integer, clamped to int32."""
    scaled = int(round(degrees * 1e7))
    return max(-2147483648, min(2147483647, scaled))


# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------


def build_device_metrics(
    battery_level: int = 0,
    voltage: float = 0.0,
    channel_utilization: float = 0.0,
    air_util_tx: float = 0.0,
    uptime_seconds: int = 0,
) -> bytes:
    """
    Build a DeviceMetrics protobuf (telemetry.proto).

    Field numbers:
        1 battery_level (uint32, percent; 101 means plugged in)
        2 voltage (float, volts)
        3 channel_utilization (float, percent)
        4 air_util_tx (float, percent)
        5 uptime_seconds (uint32)
    """
    writer = ProtobufWriter()
    writer.uint32(1, max(0, min(101, int(battery_level))))
    writer.float(2, float(voltage))
    writer.float(3, float(channel_utilization))
    writer.float(4, float(air_util_tx))
    writer.uint32(5, max(0, int(uptime_seconds)))
    return writer.to_bytes()


def build_environment_metrics(
    temperature_c: Optional[float] = None,
    relative_humidity: Optional[float] = None,
    barometric_pressure: Optional[float] = None,
) -> bytes:
    """
    Build an EnvironmentMetrics protobuf (telemetry.proto).

    Field numbers:
        1 temperature (float, Celsius)
        2 relative_humidity (float, percent)
        3 barometric_pressure (float, hPa)
    """
    writer = ProtobufWriter()
    if temperature_c is not None:
        writer.float(1, float(temperature_c), force=True)
    if relative_humidity is not None:
        writer.float(2, float(relative_humidity), force=True)
    if barometric_pressure is not None:
        writer.float(3, float(barometric_pressure), force=True)
    return writer.to_bytes()


def build_telemetry(
    device_metrics: Optional[bytes] = None,
    environment_metrics: Optional[bytes] = None,
    timestamp: Optional[int] = None,
) -> bytes:
    """
    Build a Telemetry protobuf wrapping one metrics variant.

    Field numbers:
        1 time (fixed32)
        2 device_metrics
        3 environment_metrics
    """
    writer = ProtobufWriter()
    writer.fixed32(1, int(timestamp if timestamp is not None else time.time()))
    if device_metrics is not None:
        writer.bytes(2, device_metrics, force=True)
    if environment_metrics is not None:
        writer.bytes(3, environment_metrics, force=True)
    return writer.to_bytes()


@dataclass
class DecodedDeviceMetrics:
    battery_level: int = 0
    voltage: float = 0.0
    channel_utilization: float = 0.0
    air_util_tx: float = 0.0
    uptime_seconds: int = 0


def parse_device_metrics(raw: bytes) -> DecodedDeviceMetrics:
    fields = ProtobufReader(raw).to_dict()

    def flt(number):
        values = fields.get(number)
        return as_float(values[-1]) if values else 0.0

    return DecodedDeviceMetrics(
        battery_level=fields.get(1, [0])[-1],
        voltage=flt(2),
        channel_utilization=flt(3),
        air_util_tx=flt(4),
        uptime_seconds=fields.get(5, [0])[-1],
    )


# --------------------------------------------------------------------------
# Node info
# --------------------------------------------------------------------------


def build_user(
    node_id: str,
    long_name: str,
    short_name: str,
    hw_model: HardwareModel = HardwareModel.PRIVATE_HW,
    is_licensed: bool = False,
    role: int = 0,
) -> bytes:
    """
    Build a User protobuf, which is what NODEINFO_APP carries.

    Without this the balloon shows up on a handheld as a bare hex id. With it
    the operator sees a name and knows what they are looking at.

    Field numbers (mesh.proto User):
        1 id (string, "!a1b2c3d4")
        2 long_name (string)
        3 short_name (string, <= 4 characters)
        5 hw_model (enum)
        6 is_licensed (bool)
        7 role (enum)
    """
    writer = ProtobufWriter()
    writer.string(1, node_id, force=True)
    writer.string(2, long_name[:39], force=True)
    writer.string(3, short_name[:4], force=True)
    writer.enum(5, int(hw_model))
    writer.bool(6, is_licensed)
    writer.enum(7, role)
    return writer.to_bytes()


@dataclass
class DecodedUser:
    node_id: str = ""
    long_name: str = ""
    short_name: str = ""
    hw_model: int = 0
    is_licensed: bool = False


def parse_user(raw: bytes) -> DecodedUser:
    fields = ProtobufReader(raw).to_dict()

    def text(number):
        values = fields.get(number)
        return values[-1].decode("utf-8", errors="replace") if values else ""

    return DecodedUser(
        node_id=text(1),
        long_name=text(2),
        short_name=text(3),
        hw_model=fields.get(5, [0])[-1],
        is_licensed=bool(fields.get(6, [0])[-1]),
    )


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------


def build_text_message(text: str, max_bytes: int = 200) -> bytes:
    """
    Encode a text message payload, truncated on a UTF-8 character boundary.

    Slicing raw bytes would split a multi-byte character and produce a
    replacement glyph on the receiver.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded

    truncated = encoded[:max_bytes]
    while truncated and (truncated[-1] & 0xC0) == 0x80:
        truncated = truncated[:-1]
    if truncated and (truncated[-1] & 0x80):
        truncated = truncated[:-1]

    logger.debug(f"Text message truncated from {len(encoded)} to {len(truncated)} bytes")
    return truncated


def parse_text_message(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")
