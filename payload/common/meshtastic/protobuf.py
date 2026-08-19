"""
Minimal protocol buffer encoder and decoder.

Meshtastic messages are protobufs, but the official Python package pulls in
the full protobuf runtime plus a large generated schema -- more weight and
startup cost than a Pi Zero should carry for the six message types this
payload actually needs.

This implements only the wire format (encoding.proto "Encoding" section of the
protobuf spec), which is small and completely specified:

    key    = (field_number << 3) | wire_type
    varint = base-128, little-endian, high bit set on continuation bytes

Supported wire types:
    0  varint       int32/int64/uint32/uint64/bool/enum
    1  fixed64      double, fixed64, sfixed64
    2  length-delim string, bytes, embedded messages
    5  fixed32      float, fixed32, sfixed32

Groups (wire types 3 and 4) are deprecated in proto3 and not supported.
"""

import struct
from typing import Any, Dict, Iterator, List, Optional, Tuple

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LENGTH_DELIMITED = 2
WIRE_FIXED32 = 5

_UINT32_MAX = 0xFFFFFFFF
_UINT64_MAX = 0xFFFFFFFFFFFFFFFF


class ProtobufError(ValueError):
    """Raised on malformed protobuf input."""


# --------------------------------------------------------------------------
# Primitive encoding
# --------------------------------------------------------------------------


def encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a base-128 varint."""
    if value < 0:
        raise ValueError("encode_varint requires a non-negative value; "
                         "use encode_varint_signed for negative int32/int64")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode_varint_signed(value: int) -> bytes:
    """
    Encode a signed int32/int64 the way protobuf does: negative values are
    sign-extended to 64 bits, so they always occupy ten bytes.
    """
    if value < 0:
        value += 1 << 64
    return encode_varint(value)


def encode_zigzag(value: int) -> int:
    """ZigZag transform for sint32/sint64, which favour small magnitudes."""
    return (value << 1) ^ (value >> 63) if value < 0 else value << 1


def decode_zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def encode_key(field_number: int, wire_type: int) -> bytes:
    if field_number < 1:
        raise ValueError(f"field number must be >= 1, got {field_number}")
    return encode_varint((field_number << 3) | wire_type)


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------


class ProtobufWriter:
    """
    Builds a protobuf message field by field.

    Proto3 default values are omitted by default, matching what the official
    library emits, so a beacon carrying mostly defaults stays small -- which
    matters on a link running at about a kilobit per second.
    """

    def __init__(self):
        self._buffer = bytearray()

    def __len__(self) -> int:
        return len(self._buffer)

    def to_bytes(self) -> bytes:
        return bytes(self._buffer)

    # --- varint fields ---

    def uint32(self, field: int, value: int, force: bool = False) -> "ProtobufWriter":
        if value == 0 and not force:
            return self
        if not 0 <= value <= _UINT32_MAX:
            raise ValueError(f"field {field}: {value} out of range for uint32")
        self._buffer += encode_key(field, WIRE_VARINT) + encode_varint(value)
        return self

    def uint64(self, field: int, value: int, force: bool = False) -> "ProtobufWriter":
        if value == 0 and not force:
            return self
        if not 0 <= value <= _UINT64_MAX:
            raise ValueError(f"field {field}: {value} out of range for uint64")
        self._buffer += encode_key(field, WIRE_VARINT) + encode_varint(value)
        return self

    def int32(self, field: int, value: int, force: bool = False) -> "ProtobufWriter":
        if value == 0 and not force:
            return self
        if not -2147483648 <= value <= 2147483647:
            raise ValueError(f"field {field}: {value} out of range for int32")
        self._buffer += encode_key(field, WIRE_VARINT) + encode_varint_signed(value)
        return self

    def sint32(self, field: int, value: int, force: bool = False) -> "ProtobufWriter":
        if value == 0 and not force:
            return self
        self._buffer += encode_key(field, WIRE_VARINT) + encode_varint(
            encode_zigzag(value)
        )
        return self

    def bool(self, field: int, value: bool, force: bool = False) -> "ProtobufWriter":
        if not value and not force:
            return self
        self._buffer += encode_key(field, WIRE_VARINT) + encode_varint(1 if value else 0)
        return self

    def enum(self, field: int, value: int, force: bool = False) -> "ProtobufWriter":
        return self.uint32(field, value, force=force)

    # --- fixed-width fields ---

    def fixed32(self, field: int, value: int, force: bool = False) -> "ProtobufWriter":
        if value == 0 and not force:
            return self
        self._buffer += encode_key(field, WIRE_FIXED32) + struct.pack("<I", value)
        return self

    def sfixed32(self, field: int, value: int, force: bool = False) -> "ProtobufWriter":
        if value == 0 and not force:
            return self
        self._buffer += encode_key(field, WIRE_FIXED32) + struct.pack("<i", value)
        return self

    def float(self, field: int, value: float, force: bool = False) -> "ProtobufWriter":
        if value == 0.0 and not force:
            return self
        self._buffer += encode_key(field, WIRE_FIXED32) + struct.pack("<f", value)
        return self

    def double(self, field: int, value: float, force: bool = False) -> "ProtobufWriter":
        if value == 0.0 and not force:
            return self
        self._buffer += encode_key(field, WIRE_FIXED64) + struct.pack("<d", value)
        return self

    # --- length-delimited fields ---

    def bytes(self, field: int, value: bytes, force: bool = False) -> "ProtobufWriter":
        if not value and not force:
            return self
        self._buffer += (
            encode_key(field, WIRE_LENGTH_DELIMITED)
            + encode_varint(len(value))
            + value
        )
        return self

    def string(self, field: int, value: str, force: bool = False) -> "ProtobufWriter":
        if not value and not force:
            return self
        return self.bytes(field, value.encode("utf-8"), force=True)

    def message(
        self, field: int, value: "ProtobufWriter", force: bool = False
    ) -> "ProtobufWriter":
        encoded = value.to_bytes()
        if not encoded and not force:
            return self
        return self.bytes(field, encoded, force=True)


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------


def decode_varint(data: bytes, offset: int = 0) -> Tuple[int, int]:
    """
    Decode a varint.

    Returns:
        (value, new_offset)

    Raises:
        ProtobufError: on truncated input or a varint longer than 10 bytes.
    """
    result = 0
    shift = 0
    start = offset

    while True:
        if offset >= len(data):
            raise ProtobufError(f"truncated varint at offset {start}")
        if shift >= 64:
            raise ProtobufError(f"varint at offset {start} exceeds 64 bits")

        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result & _UINT64_MAX, offset
        shift += 7


class ProtobufReader:
    """
    Iterates the fields of a protobuf message.

    Deliberately tolerant: unknown field numbers are yielded rather than
    rejected, because Meshtastic adds fields over time and a beacon from a
    newer firmware must still parse.
    """

    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0

    def __iter__(self) -> Iterator[Tuple[int, int, Any]]:
        """Yield (field_number, wire_type, value) for each field."""
        while self._offset < len(self._data):
            yield self._read_field()

    def _read_field(self) -> Tuple[int, int, Any]:
        key, self._offset = decode_varint(self._data, self._offset)
        field_number = key >> 3
        wire_type = key & 0x07

        if field_number == 0:
            raise ProtobufError("field number 0 is not valid")

        if wire_type == WIRE_VARINT:
            value, self._offset = decode_varint(self._data, self._offset)
            return field_number, wire_type, value

        if wire_type == WIRE_FIXED64:
            self._require(8)
            value = self._data[self._offset : self._offset + 8]
            self._offset += 8
            return field_number, wire_type, value

        if wire_type == WIRE_FIXED32:
            self._require(4)
            value = self._data[self._offset : self._offset + 4]
            self._offset += 4
            return field_number, wire_type, value

        if wire_type == WIRE_LENGTH_DELIMITED:
            length, self._offset = decode_varint(self._data, self._offset)
            self._require(length)
            value = self._data[self._offset : self._offset + length]
            self._offset += length
            return field_number, wire_type, value

        raise ProtobufError(
            f"unsupported wire type {wire_type} for field {field_number} "
            f"(groups are not supported)"
        )

    def _require(self, count: int) -> None:
        if self._offset + count > len(self._data):
            raise ProtobufError(
                f"truncated field at offset {self._offset}: need {count} bytes, "
                f"have {len(self._data) - self._offset}"
            )

    def to_dict(self) -> Dict[int, List[Any]]:
        """
        Collect every field into {field_number: [values]}.

        Repeated fields naturally accumulate; for singular fields protobuf
        semantics say last-one-wins, so callers should take `values[-1]`.
        """
        out: Dict[int, List[Any]] = {}
        for field_number, _wire_type, value in self:
            out.setdefault(field_number, []).append(value)
        return out


def as_int32(value: int) -> int:
    """Reinterpret a decoded varint as a signed int32."""
    value &= _UINT32_MAX
    return value - (1 << 32) if value & 0x80000000 else value


def as_float(raw: bytes) -> float:
    return struct.unpack("<f", raw)[0]


def as_sfixed32(raw: bytes) -> int:
    return struct.unpack("<i", raw)[0]


def as_fixed32(raw: bytes) -> int:
    return struct.unpack("<I", raw)[0]
