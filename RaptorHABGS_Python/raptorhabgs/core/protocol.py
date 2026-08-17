"""
Protocol definitions for RaptorHab communication.
Parses packets from the Heltec LoRa radio modem.

Frame format from modem (with HDLC byte stuffing):
[0x7E][LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM][0x7E]

Byte stuffing:
- 0x7E in data -> 0x7D 0x5E
- 0x7D in data -> 0x7D 0x5D

Packet format (inside DATA):
[SYNC: "RAPT" (4 bytes)][TYPE (1)][SEQ_HI (1)][SEQ_LO (1)][FLAGS (1)][PAYLOAD...][CRC32 (4)]
"""

import struct
from enum import IntEnum
from dataclasses import dataclass
from typing import Optional, Tuple, List
from datetime import datetime


class PacketType(IntEnum):
    """Packet type identifiers matching the airborne firmware."""
    # Air -> Ground
    TELEMETRY = 0x00
    IMAGE_META = 0x01
    IMAGE_DATA = 0x02
    TEXT_MESSAGE = 0x03
    COMMAND_ACK = 0x10
    
    # Ground -> Air
    CMD_PING = 0x80
    CMD_SET_PARAM = 0x81
    CMD_CAPTURE = 0x82
    CMD_REBOOT = 0x83
    
    UNKNOWN = 0xFF


class FixType(IntEnum):
    """GPS fix type."""
    NONE = 0
    FIX_2D = 1
    FIX_3D = 2


# Sync word for packet detection - "RAPT"
SYNC_WORD = bytes([0x52, 0x41, 0x50, 0x54])

# Frame delimiters (from Heltec modem)
FRAME_DELIMITER = 0x7E
ESCAPE_BYTE = 0x7D

# Protocol constants
HEADER_SIZE = 8  # sync(4) + type(1) + seq(2) + flags(1)
CRC_SIZE = 4
TELEMETRY_PAYLOAD_SIZE = 36
IMAGE_META_PAYLOAD_SIZE = 22
FOUNTAIN_SYMBOL_SIZE = 200


@dataclass
class TelemetryPayload:
    """Parsed telemetry packet matching the airborne firmware format."""
    latitude: float = 0.0          # degrees
    longitude: float = 0.0         # degrees
    altitude: float = 0.0          # meters
    speed: float = 0.0             # m/s
    heading: float = 0.0           # degrees
    satellites: int = 0
    fix_type: int = 0
    gps_time: int = 0              # Unix timestamp
    battery_mv: int = 0            # millivolts
    cpu_temp: float = 0.0          # Celsius
    radio_temp: float = 0.0        # Celsius
    image_id: int = 0
    image_progress: int = 0        # percent
    rssi: int = 0                  # dBm (from airborne unit's last received)
    
    @classmethod
    def deserialize(cls, data: bytes) -> Optional["TelemetryPayload"]:
        """
        Deserialize telemetry from binary data.
        
        Format (36 bytes, big-endian):
        - latitude: int32 (4) - degrees * 1e7
        - longitude: int32 (4) - degrees * 1e7
        - altitude: uint32 (4) - meters * 1000
        - speed: uint16 (2) - m/s * 100
        - heading: uint16 (2) - degrees * 100
        - satellites: uint8 (1)
        - fix_type: uint8 (1)
        - gps_time: uint32 (4) - Unix timestamp
        - battery_mv: uint16 (2)
        - cpu_temp: int16 (2) - Celsius * 100
        - radio_temp: int16 (2) - Celsius * 100
        - image_id: uint16 (2)
        - image_progress: uint8 (1)
        - rssi: int8 (1)
        - reserved: 2 bytes
        """
        if len(data) < TELEMETRY_PAYLOAD_SIZE:
            print(f"[Protocol] Telemetry too short: {len(data)} < {TELEMETRY_PAYLOAD_SIZE}")
            return None
        
        try:
            payload = cls()
            offset = 0
            
            # Latitude (int32, big-endian, scaled by 1e7)
            lat_raw = struct.unpack_from(">i", data, offset)[0]
            payload.latitude = lat_raw / 1e7
            offset += 4
            
            # Longitude (int32, big-endian, scaled by 1e7)
            lon_raw = struct.unpack_from(">i", data, offset)[0]
            payload.longitude = lon_raw / 1e7
            offset += 4
            
            # Altitude (uint32, big-endian, scaled by 1000)
            alt_raw = struct.unpack_from(">I", data, offset)[0]
            payload.altitude = alt_raw / 1000.0
            offset += 4
            
            # Speed (uint16, big-endian, scaled by 100)
            speed_raw = struct.unpack_from(">H", data, offset)[0]
            payload.speed = speed_raw / 100.0
            offset += 2
            
            # Heading (uint16, big-endian, scaled by 100)
            heading_raw = struct.unpack_from(">H", data, offset)[0]
            payload.heading = heading_raw / 100.0
            offset += 2
            
            # Satellites (uint8)
            payload.satellites = data[offset]
            offset += 1
            
            # Fix type (uint8)
            payload.fix_type = data[offset]
            offset += 1
            
            # GPS time (uint32, big-endian)
            payload.gps_time = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            
            # Battery mV (uint16, big-endian)
            payload.battery_mv = struct.unpack_from(">H", data, offset)[0]
            offset += 2
            
            # CPU temp (int16, big-endian, scaled by 100)
            cpu_temp_raw = struct.unpack_from(">h", data, offset)[0]
            payload.cpu_temp = cpu_temp_raw / 100.0
            offset += 2
            
            # Radio temp (int16, big-endian, scaled by 100)
            radio_temp_raw = struct.unpack_from(">h", data, offset)[0]
            payload.radio_temp = radio_temp_raw / 100.0
            offset += 2
            
            # Image ID (uint16, big-endian)
            payload.image_id = struct.unpack_from(">H", data, offset)[0]
            offset += 2
            
            # Image progress (uint8)
            payload.image_progress = data[offset]
            offset += 1
            
            # RSSI (int8)
            payload.rssi = struct.unpack_from(">b", data, offset)[0]
            offset += 1
            
            # Reserved: 2 bytes (ignored)
            
            return payload
            
        except Exception as e:
            print(f"[Protocol] Telemetry parse error: {e}")
            return None


@dataclass
class ImageMetaPayload:
    """Image metadata packet."""
    image_id: int = 0
    total_size: int = 0
    symbol_size: int = 0
    num_source_symbols: int = 0
    width: int = 0
    height: int = 0
    checksum: int = 0
    
    @classmethod
    def deserialize(cls, data: bytes) -> Optional["ImageMetaPayload"]:
        """
        Deserialize image metadata.
        
        Format (22 bytes, big-endian):
        - image_id: uint16 (2)
        - total_size: uint32 (4)
        - symbol_size: uint16 (2)
        - num_source_symbols: uint16 (2)
        - width: uint16 (2)
        - height: uint16 (2)
        - checksum: uint32 (4)
        - reserved: 4 bytes
        """
        if len(data) < IMAGE_META_PAYLOAD_SIZE:
            print(f"[Protocol] ImageMeta too short: {len(data)} < {IMAGE_META_PAYLOAD_SIZE}")
            return None
        
        try:
            payload = cls()
            payload.image_id = struct.unpack_from(">H", data, 0)[0]
            payload.total_size = struct.unpack_from(">I", data, 2)[0]
            payload.symbol_size = struct.unpack_from(">H", data, 6)[0]
            payload.num_source_symbols = struct.unpack_from(">H", data, 8)[0]
            payload.width = struct.unpack_from(">H", data, 10)[0]
            payload.height = struct.unpack_from(">H", data, 12)[0]
            payload.checksum = struct.unpack_from(">I", data, 14)[0]
            return payload
        except Exception as e:
            print(f"[Protocol] Image meta parse error: {e}")
            return None


@dataclass
class ImageDataPayload:
    """Image data (RaptorQ symbol) packet."""
    image_id: int = 0
    symbol_id: int = 0             # Used as deduplication key
    esi: int = 0                   # Extracted from raptorq header (for debug)
    symbol_data: bytes = b""       # Full raptorq serialized packet
    
    @classmethod
    def deserialize(cls, data: bytes) -> Optional["ImageDataPayload"]:
        """
        Deserialize image data packet.
        
        Format (big-endian):
        - image_id: uint16 (2)
        - symbol_id: uint32 (4)
        - raptorq_packet: remaining bytes (4-byte header + 200 data)
        """
        if len(data) < 10:  # Minimum: imageId(2) + symbolId(4) + some data
            print(f"[Protocol] ImageData too short: {len(data)}")
            return None
        
        try:
            payload = cls()
            payload.image_id = struct.unpack_from(">H", data, 0)[0]
            payload.symbol_id = struct.unpack_from(">I", data, 2)[0]
            
            # Remaining data is the raptorq serialized packet
            raptorq_data = data[6:]
            
            # Extract ESI from raptorq header for debug
            if len(raptorq_data) >= 4:
                payload.esi = struct.unpack_from(">I", raptorq_data, 0)[0]
            
            # Store full raptorq packet
            payload.symbol_data = bytes(raptorq_data)
            
            return payload
        except Exception as e:
            print(f"[Protocol] Image data parse error: {e}")
            return None


@dataclass
class TextMessagePayload:
    """Text message packet."""
    message: str = ""
    
    @classmethod
    def deserialize(cls, data: bytes) -> Optional["TextMessagePayload"]:
        """Deserialize text message."""
        try:
            # Find null terminator or use full data
            null_pos = data.find(0)
            if null_pos >= 0:
                text_data = data[:null_pos]
            else:
                text_data = data
            
            payload = cls()
            payload.message = text_data.decode("utf-8", errors="replace")
            return payload
        except Exception as e:
            print(f"[Protocol] Text message parse error: {e}")
            return None


class CRC32:
    """CRC-32 implementation (IEEE 802.3 polynomial)."""
    
    POLYNOMIAL = 0xEDB88320
    _table: List[int] = []
    
    @classmethod
    def _init_table(cls):
        """Initialize CRC lookup table."""
        if cls._table:
            return
        
        cls._table = []
        for i in range(256):
            crc = i
            for _ in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ cls.POLYNOMIAL
                else:
                    crc >>= 1
            cls._table.append(crc)
    
    @classmethod
    def calculate(cls, data: bytes, initial: int = 0xFFFFFFFF) -> int:
        """Calculate CRC32 of data."""
        cls._init_table()
        
        crc = initial
        for byte in data:
            index = (crc ^ byte) & 0xFF
            crc = cls._table[index] ^ (crc >> 8)
        
        return crc ^ 0xFFFFFFFF
    
    @classmethod
    def verify(cls, packet: bytes) -> bool:
        """Verify CRC32 at end of packet."""
        if len(packet) < 4:
            return False
        
        data_without_crc = packet[:-4]
        received_crc = struct.unpack(">I", packet[-4:])[0]  # Big-endian CRC
        calculated_crc = cls.calculate(data_without_crc)
        
        return calculated_crc == received_crc


class PacketParser:
    """
    Parses RaptorHab packets.
    
    Packet format:
    [SYNC: "RAPT" (4)][TYPE (1)][SEQ_HI (1)][SEQ_LO (1)][FLAGS (1)][PAYLOAD...][CRC32 (4)]
    """
    
    # Expected payload sizes by packet type (minimum for variable types)
    # Fixed-size packets: TELEMETRY, IMAGE_META
    # Variable-size packets: IMAGE_DATA, TEXT_MESSAGE (use actual received size)
    FIXED_PAYLOAD_SIZES = {
        PacketType.TELEMETRY: TELEMETRY_PAYLOAD_SIZE,      # 36 bytes - fixed
        PacketType.IMAGE_META: IMAGE_META_PAYLOAD_SIZE,    # 22 bytes - fixed
        PacketType.COMMAND_ACK: 4,                          # 4 bytes - fixed
    }
    
    # Minimum payload sizes for variable-length packets
    MIN_PAYLOAD_SIZES = {
        PacketType.IMAGE_DATA: 10,      # imageId(2) + symbolId(4) + some data
        PacketType.TEXT_MESSAGE: 1,     # At least 1 byte
    }
    
    @classmethod
    def parse(cls, data: bytes) -> Optional[Tuple[int, int, int, bytes]]:
        """
        Parse a raw packet and verify CRC.
        
        Args:
            data: Raw packet bytes (should include sync word)
        
        Returns: 
            (packet_type, sequence, flags, payload) or None if invalid
        """
        # Check minimum size: sync(4) + header(4) + crc(4) = 12
        if len(data) < HEADER_SIZE + CRC_SIZE:
            print(f"[Protocol] Packet too short: {len(data)} bytes")
            return None
        
        # Verify sync word "RAPT"
        if data[:4] != SYNC_WORD:
            print(f"[Protocol] Invalid sync word: {data[:4].hex()}")
            return None
        
        # Parse header (after sync word)
        try:
            packet_type = PacketType(data[4])
        except ValueError:
            packet_type = PacketType.UNKNOWN
        
        sequence = (data[5] << 8) | data[6]  # Big-endian
        flags = data[7]
        
        # Determine packet size based on type
        if packet_type in cls.FIXED_PAYLOAD_SIZES:
            # Fixed-size packet - use expected size
            expected_payload = cls.FIXED_PAYLOAD_SIZES[packet_type]
            expected_total = HEADER_SIZE + expected_payload + CRC_SIZE
            
            if len(data) < expected_total:
                print(f"[Protocol] Not enough data for {packet_type.name}: {len(data)} < {expected_total}")
                return None
            
            actual_packet = data[:expected_total]
        else:
            # Variable-size packet - use actual received size
            # The CRC is at the end, so the packet is the full data
            actual_packet = data
            
            # Check minimum size
            min_payload = cls.MIN_PAYLOAD_SIZES.get(packet_type, 1)
            if len(data) < HEADER_SIZE + min_payload + CRC_SIZE:
                print(f"[Protocol] Packet too small for {packet_type.name}: {len(data)}")
                return None
        
        # Verify CRC
        if not CRC32.verify(actual_packet):
            print(f"[Protocol] CRC mismatch for packet type {packet_type.name}")
            return None
        
        # Extract payload (between header and CRC)
        payload = actual_packet[HEADER_SIZE:-CRC_SIZE]
        
        return (int(packet_type), sequence, flags, payload)
    
    @staticmethod
    def calculate_checksum(data: bytes) -> int:
        """Calculate XOR checksum (for serial frame, not packet CRC)."""
        checksum = 0
        for byte in data:
            checksum ^= byte
        return checksum


class FrameExtractor:
    """
    Extracts frames from the serial data stream.
    
    Handles the Heltec modem frame format with HDLC byte stuffing:
    [0x7E][LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM][0x7E]
    
    Byte stuffing (inside frame):
    - 0x7E -> 0x7D 0x5E
    - 0x7D -> 0x7D 0x5D
    """
    
    def __init__(self):
        self.buffer = bytearray()
        self.rssi = 0.0
        self.snr = 0.0
        
        # Stats for debugging
        self.frames_extracted = 0
        self.checksum_failures = 0
        self.no_rapt_failures = 0
    
    def add_data(self, data: bytes) -> List[Tuple[float, float, bytes]]:
        """
        Add data to buffer and extract complete frames.
        
        Returns: List of (rssi, snr, payload) tuples
        """
        self.buffer.extend(data)
        frames = []
        
        while True:
            frame = self._extract_frame()
            if frame is None:
                break
            frames.append(frame)
        
        # Prevent buffer overflow
        if len(self.buffer) > 10000:
            print(f"[FrameExtractor] Buffer overflow, clearing {len(self.buffer)} bytes")
            self.buffer.clear()
        
        return frames
    
    def _extract_frame(self) -> Optional[Tuple[float, float, bytes]]:
        """Extract a single frame from the buffer with byte de-stuffing."""
        
        # Find frame start delimiter
        try:
            start_idx = self.buffer.index(FRAME_DELIMITER)
        except ValueError:
            # No start delimiter, clear buffer
            self.buffer.clear()
            return None
        
        # Remove data before frame start
        if start_idx > 0:
            del self.buffer[:start_idx]
        
        # Need at least a few bytes to check
        if len(self.buffer) < 2:
            return None
        
        # Find end delimiter - scan for 0x7E that's NOT part of escape sequence
        # In properly stuffed data, 0x7E only appears as delimiter
        end_offset = None
        i = 1  # Start after start delimiter
        
        while i < len(self.buffer):
            if self.buffer[i] == FRAME_DELIMITER:
                end_offset = i
                break
            # Skip escape sequences
            if self.buffer[i] == ESCAPE_BYTE and i + 1 < len(self.buffer):
                i += 2  # Skip escape byte and following byte
            else:
                i += 1
        
        if end_offset is None:
            # No end delimiter yet - need more data
            if len(self.buffer) > 2000:
                print(f"[FrameExtractor] Buffer too large ({len(self.buffer)}), clearing")
                self.buffer.clear()
            return None
        
        # Extract stuffed frame (excluding delimiters)
        stuffed_data = bytes(self.buffer[1:end_offset])
        
        # Remove frame from buffer (including both delimiters)
        del self.buffer[:end_offset + 1]
        
        # De-stuff the data (HDLC-style)
        destuffed = self._destuff(stuffed_data)
        
        if destuffed is None or len(destuffed) < 8:
            print(f"[FrameExtractor] Frame too short after de-stuffing: {len(destuffed) if destuffed else 0}")
            return None
        
        # Parse the de-stuffed frame
        return self._parse_frame(destuffed)
    
    def _destuff(self, data: bytes) -> Optional[bytearray]:
        """Remove HDLC byte stuffing."""
        destuffed = bytearray()
        i = 0
        
        while i < len(data):
            if data[i] == ESCAPE_BYTE and i + 1 < len(data):
                next_byte = data[i + 1]
                if next_byte == 0x5E:
                    destuffed.append(0x7E)
                    i += 2
                elif next_byte == 0x5D:
                    destuffed.append(0x7D)
                    i += 2
                else:
                    # Invalid escape sequence - just pass through
                    print(f"[FrameExtractor] Invalid escape: 7D {next_byte:02X}")
                    destuffed.append(data[i])
                    i += 1
            else:
                destuffed.append(data[i])
                i += 1
        
        return destuffed
    
    def _parse_frame(self, frame: bytearray) -> Optional[Tuple[float, float, bytes]]:
        """
        Parse a de-stuffed frame.
        
        Frame format (after de-stuffing, no delimiters):
        [LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM]
        """
        if len(frame) < 8:
            return None
        
        # Parse header
        len_hi = frame[0]
        len_lo = frame[1]
        data_len = (len_hi << 8) | len_lo
        
        if data_len <= 0 or data_len > 255:
            print(f"[FrameExtractor] Invalid frame length: {data_len}")
            return None
        
        # Expected size: len(2) + rssi(2) + snr(2) + data(dataLen) + checksum(1)
        expected_size = 2 + 2 + 2 + data_len + 1
        
        if len(frame) < expected_size:
            print(f"[FrameExtractor] Frame size mismatch: got {len(frame)}, expected {expected_size}")
            return None
        
        # Parse RSSI and SNR (handle negative values properly)
        rssi_int = struct.unpack_from("b", frame, 2)[0]  # signed
        rssi_frac = frame[3]
        snr_int = struct.unpack_from("b", frame, 4)[0]   # signed
        snr_frac = frame[5]
        
        # Calculate RSSI and SNR with proper sign handling
        if rssi_int < 0:
            rssi = float(rssi_int) - rssi_frac / 100.0
        else:
            rssi = float(rssi_int) + rssi_frac / 100.0
        
        if snr_int < 0:
            snr = float(snr_int) - snr_frac / 100.0
        else:
            snr = float(snr_int) + snr_frac / 100.0
        
        # Extract data
        data_start = 6
        data_end = data_start + data_len
        packet_data = bytes(frame[data_start:data_end])
        
        # Verify checksum (XOR of all bytes except checksum)
        received_checksum = frame[data_end]
        calculated_checksum = 0
        for i in range(data_end):
            calculated_checksum ^= frame[i]
        
        if received_checksum != calculated_checksum:
            print(f"[FrameExtractor] Serial checksum mismatch: rx={received_checksum:02X}, calc={calculated_checksum:02X}")
            self.checksum_failures += 1
            return None
        
        # Validate that packet starts with RAPT sync
        if len(packet_data) < 8:
            print(f"[FrameExtractor] Packet data too short: {len(packet_data)}")
            return None
        
        if packet_data[:4] != SYNC_WORD:
            print(f"[FrameExtractor] Packet missing RAPT sync: {packet_data[:4].hex()}")
            self.no_rapt_failures += 1
            return None
        
        self.frames_extracted += 1
        self.rssi = rssi
        self.snr = snr
        
        return (rssi, snr, packet_data)
    
    def clear(self):
        """Clear the buffer."""
        self.buffer.clear()
