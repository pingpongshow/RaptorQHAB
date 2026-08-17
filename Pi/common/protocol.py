"""
RaptorHab Protocol Definitions
Packet structures and serialization/deserialization
"""

import struct
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple, Union

from .constants import (
    SYNC_WORD, HEADER_SIZE, CRC_SIZE, MAX_PAYLOAD_SIZE, MAX_PACKET_SIZE,
    PacketType, PacketFlags, FixType, TELEMETRY_PAYLOAD_SIZE
)
from .crc import crc32, crc32_bytes, verify_crc32_packet


@dataclass
class PacketHeader:
    """Common packet header structure"""
    packet_type: PacketType
    sequence: int
    flags: int = PacketFlags.NONE
    
    def serialize(self) -> bytes:
        """Serialize header to bytes (without sync word)"""
        return struct.pack('>BHB', self.packet_type, self.sequence, self.flags)
    
    @classmethod
    def deserialize(cls, data: bytes) -> 'PacketHeader':
        """Deserialize header from bytes (without sync word)"""
        if len(data) < 4:
            raise ValueError(f"Header too short: {len(data)} bytes")
        packet_type, sequence, flags = struct.unpack('>BHB', data[:4])
        return cls(PacketType(packet_type), sequence, flags)


@dataclass
class TelemetryPayload:
    """Telemetry packet payload (Type 0x00)"""
    latitude: float = 0.0          # degrees
    longitude: float = 0.0         # degrees
    altitude: float = 0.0          # meters
    speed: float = 0.0             # m/s
    heading: float = 0.0           # degrees
    satellites: int = 0
    fix_type: FixType = FixType.NONE
    gps_time: int = 0              # Unix timestamp
    battery_mv: int = 0            # millivolts
    cpu_temp: float = 0.0          # Celsius
    radio_temp: float = 0.0        # Celsius
    image_id: int = 0
    image_progress: int = 0        # percent
    rssi: int = 0                  # dBm (signed)
    reserved: bytes = field(default_factory=lambda: bytes(4))
    
    def serialize(self) -> bytes:
        """Serialize telemetry to 36 bytes"""
        # Clamp values to valid ranges to prevent struct overflow
        def clamp(val, min_val, max_val):
            return max(min_val, min(max_val, int(val)))
        
        return struct.pack(
            '>iiIHHBBIHhhHBb4s',
            clamp(self.latitude * 1e7, -2147483648, 2147483647),   # int32
            clamp(self.longitude * 1e7, -2147483648, 2147483647),  # int32
            clamp(self.altitude * 1000, 0, 4294967295),            # uint32
            clamp(self.speed * 100, 0, 65535),                     # uint16
            clamp(self.heading * 100, 0, 65535),                   # uint16
            clamp(self.satellites, 0, 255),                        # uint8
            clamp(self.fix_type, 0, 255),                          # uint8
            clamp(self.gps_time, 0, 4294967295),                   # uint32
            clamp(self.battery_mv, 0, 65535),                      # uint16
            clamp(self.cpu_temp * 100, -32768, 32767),             # int16
            clamp(self.radio_temp * 100, -32768, 32767),           # int16
            clamp(self.image_id, 0, 65535),                        # uint16
            clamp(self.image_progress, 0, 255),                    # uint8
            clamp(self.rssi, -128, 127),                           # int8
            self.reserved                                          # 4 bytes
        )
    
    @classmethod
    def deserialize(cls, data: bytes) -> 'TelemetryPayload':
        """Deserialize telemetry from bytes"""
        if len(data) < TELEMETRY_PAYLOAD_SIZE:
            raise ValueError(f"Telemetry payload too short: {len(data)} bytes")
        
        (lat, lon, alt, speed, heading, sats, fix, gps_time,
         batt, cpu_temp, radio_temp, img_id, img_prog, rssi, reserved) = struct.unpack(
            '>iiIHHBBIHhhHBb4s', data[:TELEMETRY_PAYLOAD_SIZE]
        )
        
        return cls(
            latitude=lat / 1e7,
            longitude=lon / 1e7,
            altitude=alt / 1000.0,
            speed=speed / 100.0,
            heading=heading / 100.0,
            satellites=sats,
            fix_type=FixType(fix),
            gps_time=gps_time,
            battery_mv=batt,
            cpu_temp=cpu_temp / 100.0,
            radio_temp=radio_temp / 100.0,
            image_id=img_id,
            image_progress=img_prog,
            rssi=rssi,
            reserved=reserved
        )


@dataclass
class ImageMetaPayload:
    """Image metadata packet payload (Type 0x01)"""
    image_id: int = 0
    total_size: int = 0            # bytes
    symbol_size: int = 200         # bytes per symbol
    num_source_symbols: int = 0    # number of source symbols
    checksum: int = 0              # CRC-32 of original image
    width: int = 0
    height: int = 0
    timestamp: int = 0             # Unix timestamp
    
    def serialize(self) -> bytes:
        """Serialize image metadata"""
        return struct.pack(
            '>HIHHI HHI',
            self.image_id,
            self.total_size,
            self.symbol_size,
            self.num_source_symbols,
            self.checksum,
            self.width,
            self.height,
            self.timestamp
        )
    
    @classmethod
    def deserialize(cls, data: bytes) -> 'ImageMetaPayload':
        """Deserialize image metadata from bytes"""
        if len(data) < 22:
            raise ValueError(f"Image meta payload too short: {len(data)} bytes")
        
        (img_id, total_size, symbol_size, num_source,
         checksum, width, height, timestamp) = struct.unpack(
            '>HIHHI HHI', data[:22]
        )
        
        return cls(
            image_id=img_id,
            total_size=total_size,
            symbol_size=symbol_size,
            num_source_symbols=num_source,
            checksum=checksum,
            width=width,
            height=height,
            timestamp=timestamp
        )


@dataclass
class ImageDataPayload:
    """Image data packet payload (Type 0x02)"""
    image_id: int = 0
    symbol_id: int = 0             # Fountain code symbol ID
    symbol_data: bytes = field(default_factory=bytes)
    
    def serialize(self) -> bytes:
        """Serialize image data"""
        return struct.pack('>HI', self.image_id, self.symbol_id) + self.symbol_data
    
    @classmethod
    def deserialize(cls, data: bytes) -> 'ImageDataPayload':
        """Deserialize image data from bytes"""
        if len(data) < 6:
            raise ValueError(f"Image data payload too short: {len(data)} bytes")
        
        img_id, symbol_id = struct.unpack('>HI', data[:6])
        symbol_data = data[6:]
        
        return cls(
            image_id=img_id,
            symbol_id=symbol_id,
            symbol_data=symbol_data
        )


@dataclass
class TextMessagePayload:
    """Text message packet payload (Type 0x03)"""
    message: str = ""
    
    def serialize(self) -> bytes:
        """Serialize text message"""
        encoded = self.message.encode('utf-8')
        if len(encoded) > MAX_PAYLOAD_SIZE:
            encoded = encoded[:MAX_PAYLOAD_SIZE]
        return encoded
    
    @classmethod
    def deserialize(cls, data: bytes) -> 'TextMessagePayload':
        """Deserialize text message from bytes"""
        return cls(message=data.decode('utf-8', errors='replace'))


@dataclass
class CommandAckPayload:
    """Command acknowledgment payload (Type 0x10)"""
    acked_type: PacketType = PacketType.CMD_PING
    acked_seq: int = 0
    status: int = 0                # 0 = success, >0 = error code
    data: bytes = field(default_factory=bytes)  # Optional response data
    
    def to_bytes(self) -> bytes:
        """Serialize command ack to bytes"""
        return struct.pack('>BHB', self.acked_type, self.acked_seq, self.status) + self.data
    
    def serialize(self) -> bytes:
        """Alias for to_bytes"""
        return self.to_bytes()
    
    @classmethod
    def from_bytes(cls, data: bytes) -> 'CommandAckPayload':
        """Deserialize command ack from bytes"""
        if len(data) < 4:
            raise ValueError(f"Command ack payload too short: {len(data)} bytes")
        
        cmd_type, cmd_seq, status = struct.unpack('>BHB', data[:4])
        extra_data = data[4:] if len(data) > 4 else b""
        return cls(
            acked_type=PacketType(cmd_type),
            acked_seq=cmd_seq,
            status=status,
            data=extra_data
        )
    
    @classmethod
    def deserialize(cls, data: bytes) -> 'CommandAckPayload':
        """Alias for from_bytes"""
        return cls.from_bytes(data)


@dataclass
class CommandPayload:
    """Generic command payload for SETPARAM and similar commands"""
    param_id: int = 0
    value: int = 0
    extra_data: bytes = field(default_factory=bytes)
    
    def to_bytes(self) -> bytes:
        """Serialize command payload to bytes"""
        return struct.pack('>BH', self.param_id, self.value) + self.extra_data
    
    def serialize(self) -> bytes:
        """Alias for to_bytes"""
        return self.to_bytes()
    
    @classmethod
    def from_bytes(cls, data: bytes) -> 'CommandPayload':
        """Deserialize command payload from bytes"""
        if len(data) < 3:
            raise ValueError(f"Command payload too short: {len(data)} bytes")
        
        param_id, value = struct.unpack('>BH', data[:3])
        extra = data[3:] if len(data) > 3 else b""
        return cls(param_id=param_id, value=value, extra_data=extra)
    
    @classmethod
    def deserialize(cls, data: bytes) -> 'CommandPayload':
        """Alias for from_bytes"""
        return cls.from_bytes(data)


@dataclass
class CommandPingPayload:
    """Ping command payload (Type 0x80)"""
    timestamp: int = 0             # Sender timestamp for RTT measurement
    
    def serialize(self) -> bytes:
        return struct.pack('>I', self.timestamp)
    
    @classmethod
    def deserialize(cls, data: bytes) -> 'CommandPingPayload':
        if len(data) < 4:
            raise ValueError(f"Ping payload too short: {len(data)} bytes")
        timestamp, = struct.unpack('>I', data[:4])
        return cls(timestamp=timestamp)


@dataclass
class CommandSetParamPayload:
    """Set parameter command payload (Type 0x81)"""
    param_id: int = 0
    param_value: int = 0
    
    def serialize(self) -> bytes:
        return struct.pack('>BI', self.param_id, self.param_value)
    
    @classmethod
    def deserialize(cls, data: bytes) -> 'CommandSetParamPayload':
        if len(data) < 5:
            raise ValueError(f"SetParam payload too short: {len(data)} bytes")
        param_id, param_value = struct.unpack('>BI', data[:5])
        return cls(param_id=param_id, param_value=param_value)


# Payload type mapping
PAYLOAD_TYPES = {
    PacketType.TELEMETRY: TelemetryPayload,
    PacketType.IMAGE_META: ImageMetaPayload,
    PacketType.IMAGE_DATA: ImageDataPayload,
    PacketType.TEXT_MSG: TextMessagePayload,
    PacketType.CMD_ACK: CommandAckPayload,
    PacketType.CMD_PING: CommandPingPayload,
    PacketType.CMD_SETPARAM: CommandPayload,
    PacketType.CMD_CAPTURE: None,  # No payload
    PacketType.CMD_REBOOT: None,   # Optional magic bytes
}


def build_packet(
    packet_type: PacketType,
    sequence: int,
    payload: Union[bytes, object],
    flags: int = PacketFlags.NONE
) -> bytes:
    """
    Build a complete packet with sync word, header, payload, and CRC
    
    Args:
        packet_type: Type of packet
        sequence: Sequence number (0-65535)
        payload: Payload object or raw bytes
        flags: Packet flags
        
    Returns:
        Complete packet bytes
    """
    # Serialize payload if it's an object
    if hasattr(payload, 'serialize'):
        payload_bytes = payload.serialize()
    elif isinstance(payload, bytes):
        payload_bytes = payload
    else:
        raise TypeError(f"Payload must be bytes or have serialize method")
    
    # Check payload size
    if len(payload_bytes) > MAX_PAYLOAD_SIZE:
        raise ValueError(f"Payload too large: {len(payload_bytes)} > {MAX_PAYLOAD_SIZE}")
    
    # Build header
    header = PacketHeader(packet_type, sequence, flags)
    
    # Assemble packet without CRC
    packet = SYNC_WORD + header.serialize() + payload_bytes
    
    # Add CRC-32
    packet += crc32_bytes(packet)
    
    return packet


def parse_packet(data: bytes) -> Optional[Tuple[PacketType, int, int, bytes]]:
    """
    Parse a raw packet and verify CRC
    
    The SX1262 may return padded data (255 bytes), so we need to determine
    the actual packet length based on packet type and verify CRC at the
    correct position.
    
    Args:
        data: Raw packet bytes (may be padded)
        
    Returns:
        Tuple of (packet_type, sequence, flags, payload_bytes) or None if invalid
    """
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        # Check minimum size for header
        if len(data) < HEADER_SIZE:
            logger.debug(f"Packet too short for header: {len(data)} bytes")
            return None
        
        # Verify sync word
        if data[:4] != SYNC_WORD:
            logger.debug(f"Sync word mismatch: got {data[:4].hex()}, expected {SYNC_WORD.hex()}")
            return None
        
        # Parse header to get packet type
        header = PacketHeader.deserialize(data[4:8])
        packet_type = header.packet_type
        
        # Calculate expected packet length based on packet type
        # Packet = HEADER(8) + PAYLOAD + CRC(4)
        expected_payload_len = _get_expected_payload_length(packet_type, data)
        
        if expected_payload_len is None:
            # Unknown packet type or can't determine length
            # Fall back to trying full received length
            logger.debug(f"Unknown packet type {packet_type}, trying full length")
            expected_payload_len = len(data) - HEADER_SIZE - CRC_SIZE
        
        expected_total = HEADER_SIZE + expected_payload_len + CRC_SIZE
        
        if len(data) < expected_total:
            logger.debug(f"Packet too short: {len(data)} < {expected_total}")
            return None
        
        # Extract just the actual packet (not padding)
        actual_packet = data[:expected_total]
        
        # Verify CRC on actual packet
        if not verify_crc32_packet(actual_packet):
            # Try the full received data as fallback
            if len(data) >= HEADER_SIZE + CRC_SIZE:
                if verify_crc32_packet(data):
                    actual_packet = data
                    expected_payload_len = len(data) - HEADER_SIZE - CRC_SIZE
                else:
                    calculated = crc32(actual_packet[:-4])
                    received = struct.unpack('>I', actual_packet[-4:])[0]
                    logger.debug(f"CRC mismatch: calc=0x{calculated:08x}, recv=0x{received:08x}, pkt_len={expected_total}")
                    return None
            else:
                return None
        
        # Extract payload (between header and CRC)
        payload = actual_packet[HEADER_SIZE:-CRC_SIZE]
        
        logger.debug(f"Valid packet: type={packet_type}, seq={header.sequence}, payload_len={len(payload)}")
        return (packet_type, header.sequence, header.flags, payload)
        
    except Exception as e:
        logger.debug(f"Parse exception: {e}")
        return None


def _get_expected_payload_length(packet_type: int, data: bytes) -> Optional[int]:
    """
    Calculate expected payload length based on packet type
    
    Args:
        packet_type: The packet type byte
        data: Full received data (for types that need to peek at payload)
        
    Returns:
        Expected payload length in bytes, or None if unknown
    """
    # Import here to avoid circular imports
    from common.constants import TELEMETRY_PAYLOAD_SIZE, FOUNTAIN_SYMBOL_SIZE
    
    if packet_type == PacketType.TELEMETRY:
        # Fixed size telemetry payload
        return TELEMETRY_PAYLOAD_SIZE  # 36 bytes
    
    elif packet_type == PacketType.IMAGE_META:
        # Image metadata: image_id(2) + total_size(4) + symbol_size(2) + 
        #                 num_source_symbols(2) + checksum(4) + width(2) + height(2) + timestamp(4)
        return 22
    
    elif packet_type == PacketType.IMAGE_DATA:
        # Image data: image_id(2) + symbol_id(4) + symbol_data(FOUNTAIN_SYMBOL_SIZE)
        return 2 + 4 + FOUNTAIN_SYMBOL_SIZE  # 206 bytes
    
    elif packet_type == PacketType.TEXT_MSG:
        # Variable length - we need to scan for valid CRC
        # Try common lengths
        return None
    
    elif packet_type == PacketType.CMD_ACK:
        # Command ack: acked_type(1) + acked_seq(2) + status(1) + optional data
        # Minimum is 4 bytes
        return 4
    
    else:
        # Unknown type
        return None


def parse_packet_header(data: bytes) -> Tuple[PacketHeader, bytes]:
    """
    Parse a raw packet and verify CRC, returning header object
    
    Args:
        data: Raw packet bytes
        
    Returns:
        Tuple of (header, payload_bytes)
        
    Raises:
        ValueError: If packet is invalid
    """
    # Check minimum size (HEADER_SIZE already includes sync word)
    if len(data) < HEADER_SIZE + CRC_SIZE:
        raise ValueError(f"Packet too short: {len(data)} bytes")
    
    # Verify sync word
    if data[:4] != SYNC_WORD:
        raise ValueError(f"Invalid sync word: {data[:4].hex()}")
    
    # Verify CRC
    if not verify_crc32_packet(data):
        raise ValueError("CRC verification failed")
    
    # Parse header (after sync word)
    header = PacketHeader.deserialize(data[4:8])
    
    # Extract payload (between header and CRC)
    payload = data[8:-4]
    
    return header, payload


def parse_packet_full(data: bytes) -> Tuple[PacketHeader, object]:
    """
    Parse a raw packet and deserialize the payload
    
    Args:
        data: Raw packet bytes
        
    Returns:
        Tuple of (header, payload_object)
    """
    header, payload_bytes = parse_packet(data)
    
    # Get payload class for this packet type
    payload_class = PAYLOAD_TYPES.get(header.packet_type)
    
    if payload_class is None:
        # Unknown packet type, return raw bytes
        return header, payload_bytes
    
    # Deserialize payload
    payload = payload_class.deserialize(payload_bytes)
    
    return header, payload
