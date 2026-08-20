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

import logging

# Parsing runs on every packet the modem forwards -- a hundred a second on
# a healthy link. A malformed stream printed a line per packet straight to
# stdout, which floods the terminal and drags the Qt UI down with it. These
# are diagnostics: they belong behind a level that is off unless asked for.

import struct
from enum import IntEnum
from dataclasses import dataclass
from typing import Optional, Tuple, List
from datetime import datetime

logger = logging.getLogger(__name__)


class PacketType(IntEnum):
    """Packet type identifiers matching the airborne firmware."""
    # Air -> Ground
    TELEMETRY = 0x00
    IMAGE_META = 0x01
    IMAGE_DATA = 0x02
    TEXT_MESSAGE = 0x03
    FLIGHT_SUMMARY = 0x04
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

# The dual-E22 modem carries two independent radios and so two streams: RAPTOR
# image traffic on 0x7E, and whole Meshtastic LoRa packets on 0x7B. A modem
# with one radio only ever sends 0x7E, so both are handled here and neither
# modem needs to know which ground station it is talking to.
MESHTASTIC_DELIMITER = 0x7B

# GFSK has no signal-to-noise measurement -- the SX1262 reports SNR only for
# LoRa. Modems send this in the SNR field rather than a number that looks like
# a reading. Older firmware sent -20 here, which was RadioLib's
# RADIOLIB_ERR_WRONG_MODEM error code being printed as decibels; both are
# treated as "not measured".
SNR_NOT_AVAILABLE = -128.0
_LEGACY_SNR_ERROR = -20.0


def snr_is_measured(snr: float) -> bool:
    """Whether an SNR field carries a real measurement."""
    return snr > SNR_NOT_AVAILABLE + 1.0 and snr != _LEGACY_SNR_ERROR

# Returned by _extract_frame when a frame belonged to the Meshtastic stream and
# has been stashed rather than returned. Distinct from None, which means "no
# complete frame yet" and stops the extraction loop.
_MESHTASTIC_FRAME = object()

# A candidate frame start that did not validate. The buffer has been advanced
# past one byte and the caller should scan again.
_RESYNC = object()
ESCAPE_BYTE = 0x7D

# Protocol constants
HEADER_SIZE = 8  # sync(4) + type(1) + seq(2) + flags(1)
CRC_SIZE = 4
TELEMETRY_PAYLOAD_SIZE = 36
FLIGHT_SUMMARY_PAYLOAD_SIZE = 30
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
            logger.debug(f"[Protocol] Telemetry too short: {len(data)} < {TELEMETRY_PAYLOAD_SIZE}")
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
            logger.debug(f"[Protocol] Telemetry parse error: {e}")
            return None


@dataclass
class ImageMetaPayload:
    """Image metadata packet."""
    image_id: int = 0
    total_size: int = 0
    symbol_size: int = 0
    num_source_symbols: int = 0
    checksum: int = 0
    width: int = 0
    height: int = 0
    timestamp: int = 0

    # The payload packs this as ">HIHHIHHI". The field order is checksum,
    # width, height, timestamp -- not width, height, checksum. Reading it the
    # other way round still decodes the image, because the symbols are fine,
    # but it reports the two halves of the CRC-32 as the image dimensions and
    # compares the checksum against something that was never a checksum. That
    # makes the integrity check silently useless: a corrupted image passes.
    STRUCT = ">HIHHIHHI"

    @classmethod
    def deserialize(cls, data: bytes) -> Optional["ImageMetaPayload"]:
        """
        Deserialize image metadata.

        Format (22 bytes, big-endian) -- must match
        payload/common/protocol.py:ImageMetaPayload.serialize():
        - image_id: uint16 (2)          offset 0
        - total_size: uint32 (4)        offset 2
        - symbol_size: uint16 (2)       offset 6
        - num_source_symbols: uint16 (2) offset 8
        - checksum: uint32 (4)          offset 10
        - width: uint16 (2)             offset 14
        - height: uint16 (2)            offset 16
        - timestamp: uint32 (4)         offset 18
        """
        if len(data) < IMAGE_META_PAYLOAD_SIZE:
            logger.debug(f"[Protocol] ImageMeta too short: {len(data)} < {IMAGE_META_PAYLOAD_SIZE}")
            return None

        try:
            (image_id, total_size, symbol_size, num_source_symbols,
             checksum, width, height, timestamp) = struct.unpack_from(
                cls.STRUCT, data, 0)
            payload = cls()
            payload.image_id = image_id
            payload.total_size = total_size
            payload.symbol_size = symbol_size
            payload.num_source_symbols = num_source_symbols
            payload.checksum = checksum
            payload.width = width
            payload.height = height
            payload.timestamp = timestamp
            return payload
        except Exception as e:
            logger.debug(f"[Protocol] Image meta parse error: {e}")
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
            logger.debug(f"[Protocol] ImageData too short: {len(data)}")
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
            logger.debug(f"[Protocol] Image data parse error: {e}")
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
            logger.debug(f"[Protocol] Text message parse error: {e}")
            return None


@dataclass
class FlightSummaryPayload:
    """The whole flight in 30 bytes (Type 0x04).

    Telemetry says where the balloon is now; this says what the flight has
    been. One of these received late -- or relayed off the mesh by a stranger
    -- carries the story even when every other packet was missed.
    """
    max_altitude_m: float = 0.0
    max_altitude_time: int = 0
    max_ascent_rate_mps: float = 0.0
    max_descent_rate_mps: float = 0.0
    distance_travelled_m: float = 0.0
    min_cpu_temp_c: float = 0.0
    max_cpu_temp_c: float = 0.0
    packets_sent: int = 0
    images_captured: int = 0
    flight_time_sec: int = 0
    zone: int = 0

    ZONE_NAMES = ("unknown", "launch", "cruise", "descent", "landed")

    @property
    def zone_name(self) -> str:
        return self.ZONE_NAMES[self.zone] if self.zone < len(self.ZONE_NAMES) else "?"

    @classmethod
    def deserialize(cls, data: bytes):
        if len(data) < FLIGHT_SUMMARY_PAYLOAD_SIZE:
            return None
        (alt, t, asc, desc, dist, tmin, tmax,
         pkts, imgs, mins, zone) = struct.unpack(
            ">IIhhIhhIHHH", data[:FLIGHT_SUMMARY_PAYLOAD_SIZE])
        return cls(
            max_altitude_m=alt / 10.0,
            max_altitude_time=t,
            max_ascent_rate_mps=asc / 10.0,
            max_descent_rate_mps=desc / 10.0,
            distance_travelled_m=float(dist),
            min_cpu_temp_c=tmin / 10.0,
            max_cpu_temp_c=tmax / 10.0,
            packets_sent=pkts,
            images_captured=imgs,
            flight_time_sec=mins * 60,
            zone=zone,
        )


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
        PacketType.FLIGHT_SUMMARY: FLIGHT_SUMMARY_PAYLOAD_SIZE,  # 30 - fixed
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
            logger.debug(f"[Protocol] Packet too short: {len(data)} bytes")
            return None
        
        # Verify sync word "RAPT"
        if data[:4] != SYNC_WORD:
            logger.debug(f"[Protocol] Invalid sync word: {data[:4].hex()}")
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
                logger.debug(f"[Protocol] Not enough data for {packet_type.name}: {len(data)} < {expected_total}")
                return None
            
            actual_packet = data[:expected_total]
        else:
            # Variable-size packet - use actual received size
            # The CRC is at the end, so the packet is the full data
            actual_packet = data
            
            # Check minimum size
            min_payload = cls.MIN_PAYLOAD_SIZES.get(packet_type, 1)
            if len(data) < HEADER_SIZE + min_payload + CRC_SIZE:
                logger.debug(f"[Protocol] Packet too small for {packet_type.name}: {len(data)}")
                return None
        
        # Verify CRC
        if not CRC32.verify(actual_packet):
            logger.debug(f"[Protocol] CRC mismatch for packet type {packet_type.name}")
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

        # Meshtastic packets from a dual-radio modem, still encrypted. The
        # modem does not hold the channel keys and deliberately does not
        # decrypt: a borrowed board never carries them.
        self._meshtastic: List[Tuple[float, float, bytes]] = []
        self.meshtastic_frames = 0
        # Candidate frame starts rejected. Climbing steadily means the stream
        # is being corrupted upstream.
        self.resyncs = 0

    def take_meshtastic(self) -> List[Tuple[float, float, bytes]]:
        """Whole Meshtastic LoRa packets heard since the last call."""
        frames, self._meshtastic = self._meshtastic, []
        return frames
    
    def add_data(self, data: bytes) -> List[Tuple[float, float, bytes]]:
        """
        Add data to buffer and extract complete RAPTOR frames.

        Returns: List of (rssi, snr, payload) tuples

        Meshtastic frames from a dual-radio modem are collected separately and
        retrieved with take_meshtastic(). Keeping them out of this return value
        is deliberate: every existing caller treats what comes back as a RAPTOR
        packet and would try to parse a Meshtastic one as an image symbol.
        """
        self.buffer.extend(data)
        frames = []
        
        while True:
            frame = self._extract_frame()
            if frame is None:
                break
            if frame is _MESHTASTIC_FRAME or frame is _RESYNC:
                continue
            frames.append(frame)
        
        # Prevent buffer overflow
        if len(self.buffer) > 10000:
            logger.debug(f"[FrameExtractor] Buffer overflow, clearing {len(self.buffer)} bytes")
            self.buffer.clear()
        
        return frames
    
    def _extract_frame(self) -> Optional[Tuple[float, float, bytes]]:
        """Extract a single frame from the buffer with byte de-stuffing."""
        
        # Find the start of either stream, whichever comes first.
        starts = [i for i in (self.buffer.find(FRAME_DELIMITER),
                              self.buffer.find(MESHTASTIC_DELIMITER)) if i >= 0]
        if not starts:
            # Nothing framed in here at all.
            self.buffer.clear()
            return None

        start_idx = min(starts)

        # Remove data before frame start
        if start_idx > 0:
            del self.buffer[:start_idx]
            return _RESYNC

        delimiter = self.buffer[0]

        # Start delimiter + header + a byte of data + checksum + end delimiter
        if len(self.buffer) < 10:
            return None

        # Find end delimiter - scan for a delimiter that's NOT part of an
        # escape sequence. In properly stuffed data it only appears as one.
        end_offset = None
        i = 1  # Start after start delimiter

        while i < len(self.buffer):
            # A frame ends on the same delimiter it began with. The other
            # stream's delimiter is escaped inside a frame, so it cannot
            # appear here unescaped.
            if self.buffer[i] == delimiter:
                end_offset = i
                break
            # Skip escape sequences
            if self.buffer[i] == ESCAPE_BYTE and i + 1 < len(self.buffer):
                i += 2  # Skip escape byte and following byte
            else:
                i += 1

        if end_offset is None:
            if len(self.buffer) > 2000:
                # No closing delimiter within any plausible frame length, so
                # this opening byte was payload. Step over it; clearing the
                # buffer would take the real frames with it.
                del self.buffer[:1]
                self.resyncs += 1
                return _RESYNC
            return None

        # De-stuff before consuming anything. Frames are delimited at both
        # ends, so a scanner that loses a byte can pair a closing delimiter
        # with the next frame's opening one and stay wrong forever, silently
        # discarding everything that follows. Validating first means a false
        # start costs one byte instead of the frames inside it.
        destuffed = self._destuff(bytes(self.buffer[1:end_offset]))

        if not self._frame_is_valid(destuffed):
            del self.buffer[:1]
            self.resyncs += 1
            return _RESYNC

        # Remove frame from buffer (including both delimiters)
        del self.buffer[:end_offset + 1]

        if delimiter == MESHTASTIC_DELIMITER:
            parsed = self._parse_frame(destuffed, require_sync=False)
            if parsed is not None:
                self._meshtastic.append(parsed)
                self.meshtastic_frames += 1
            return _MESHTASTIC_FRAME

        # Parse the de-stuffed frame
        return self._parse_frame(destuffed)
    
    @staticmethod
    def _frame_is_valid(destuffed) -> bool:
        """Is this candidate a real frame, before any of it is consumed?

        The modem XORs every byte it sends into the checksum, so a complete
        valid frame XORs to zero. That is what makes resynchronisation
        trustworthy: a false start has to satisfy the length field and then
        hit 1 chance in 256 to be accepted.
        """
        # len(2) + rssi(2) + snr(2) + data(1+) + checksum(1)
        if destuffed is None or len(destuffed) < 8:
            return False
        data_len = (destuffed[0] << 8) | destuffed[1]
        if not 0 < data_len <= 255 or len(destuffed) != 7 + data_len:
            return False
        parity = 0
        for byte in destuffed:
            parity ^= byte
        return parity == 0

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
                elif next_byte == 0x5B:
                    # 0x7B is a frame delimiter on the dual-E22 modem, so it
                    # escapes it like any other. Missing this mapping is not a
                    # cosmetic problem: a 210-byte image packet contains a
                    # 0x7B about 56% of the time, and each one desynchronised
                    # the frame and failed its checksum. Measured against the
                    # real parser, 45% of image packets survived instead of
                    # 100%. A single-radio modem never emits this sequence, so
                    # accepting it costs those modems nothing.
                    destuffed.append(0x7B)
                    i += 2
                elif next_byte == 0x5D:
                    destuffed.append(0x7D)
                    i += 2
                else:
                    # Invalid escape sequence - just pass through
                    logger.debug(f"[FrameExtractor] Invalid escape: 7D {next_byte:02X}")
                    destuffed.append(data[i])
                    i += 1
            else:
                destuffed.append(data[i])
                i += 1
        
        return destuffed
    
    def _parse_frame(self, frame: bytearray,
                     require_sync: bool = True) -> Optional[Tuple[float, float, bytes]]:
        """
        Parse a de-stuffed frame.
        
        Frame format (after de-stuffing, no delimiters):
        [LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM]

        require_sync is False for Meshtastic frames: they carry a whole LoRa
        packet from somebody else's radio and have no RAPT sync word to check.
        """
        if frame is None or len(frame) < 8:
            return None
        
        # Parse header
        len_hi = frame[0]
        len_lo = frame[1]
        data_len = (len_hi << 8) | len_lo
        
        if data_len <= 0 or data_len > 255:
            logger.debug(f"[FrameExtractor] Invalid frame length: {data_len}")
            return None
        
        # Expected size: len(2) + rssi(2) + snr(2) + data(dataLen) + checksum(1)
        expected_size = 2 + 2 + 2 + data_len + 1
        
        if len(frame) < expected_size:
            logger.debug(f"[FrameExtractor] Frame size mismatch: got {len(frame)}, expected {expected_size}")
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
            logger.debug(f"[FrameExtractor] Serial checksum mismatch: rx={received_checksum:02X}, calc={calculated_checksum:02X}")
            self.checksum_failures += 1
            return None
        
        # Validate that packet starts with RAPT sync
        if require_sync:
            if len(packet_data) < 8:
                logger.debug(f"[FrameExtractor] Packet data too short: {len(packet_data)}")
                return None

            if packet_data[:4] != SYNC_WORD:
                logger.debug(f"[FrameExtractor] Packet missing RAPT sync: {packet_data[:4].hex()}")
                self.no_rapt_failures += 1
                return None
        
        self.frames_extracted += 1
        self.rssi = rssi
        self.snr = snr
        
        return (rssi, snr, packet_data)
    
    def clear(self):
        """Clear the buffer."""
        self.buffer.clear()
