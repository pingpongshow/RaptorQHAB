"""
RaptorHab Common Constants
Shared between airborne payload and ground station
"""

from enum import IntEnum

# Protocol version
PROTOCOL_VERSION = 1

# Sync word for packet detection ("RAPT")
SYNC_WORD = bytes([0x52, 0x41, 0x50, 0x54])
SYNC_WORD_HEX = 0x52415054

# Packet structure sizes
SYNC_SIZE = 4
TYPE_SIZE = 1
SEQ_SIZE = 2
FLAGS_SIZE = 1
CRC_SIZE = 4
HEADER_SIZE = SYNC_SIZE + TYPE_SIZE + SEQ_SIZE + FLAGS_SIZE  # 8 bytes
MAX_PACKET_SIZE = 255
MAX_PAYLOAD_SIZE = MAX_PACKET_SIZE - HEADER_SIZE - CRC_SIZE  # 243 bytes

# Telemetry payload size
TELEMETRY_PAYLOAD_SIZE = 36


class PacketType(IntEnum):
    """Packet type identifiers"""
    # Air -> Ground
    TELEMETRY = 0x00
    IMAGE_META = 0x01
    IMAGE_DATA = 0x02
    TEXT_MSG = 0x03
    CMD_ACK = 0x10
    
    # Ground -> Air
    CMD_PING = 0x80
    CMD_SETPARAM = 0x81
    CMD_CAPTURE = 0x82
    CMD_REBOOT = 0x83


class FixType(IntEnum):
    """GPS fix type"""
    NONE = 0
    FIX_2D = 1
    FIX_3D = 2


class CommandParam(IntEnum):
    """Parameter IDs for CMD_SETPARAM"""
    TX_POWER = 0x01
    IMAGE_QUALITY = 0x02
    CAPTURE_INTERVAL = 0x03
    TELEMETRY_RATE = 0x04
    # Camera image adjustment parameters (values 0-200, 100 = default/neutral)
    CAMERA_BRIGHTNESS = 0x10
    CAMERA_CONTRAST = 0x11
    CAMERA_SATURATION = 0x12
    CAMERA_SHARPNESS = 0x13
    CAMERA_EXPOSURE_COMP = 0x14  # Exposure compensation
    CAMERA_AWB_MODE = 0x15       # Auto white balance mode (0=auto, 1=daylight, 2=cloudy, etc.)


class PacketFlags(IntEnum):
    """Packet flag bits"""
    NONE = 0x00
    URGENT = 0x01
    RETRANSMIT = 0x02
    LAST_PACKET = 0x04
    COMPRESSED = 0x08


# Radio configuration defaults
DEFAULT_FREQUENCY_MHZ = 915.0
DEFAULT_BITRATE_BPS = 200000
DEFAULT_FDEV_HZ = 125000
DEFAULT_TX_POWER_DBM = 22

# Timing constants (in seconds)
TX_PERIOD_SEC = 10
RX_PERIOD_SEC = 10
TELEMETRY_INTERVAL_PACKETS = 10
IMAGE_META_INTERVAL_PACKETS = 100

# Fountain code defaults
FOUNTAIN_SYMBOL_SIZE = 200
FOUNTAIN_OVERHEAD_PERCENT = 25

# Camera defaults
DEFAULT_CAMERA_RESOLUTION = (1280, 960)
DEFAULT_WEBP_QUALITY = 75
DEFAULT_BURST_COUNT = 5

# Image transmission
IMAGE_SYMBOL_SIZE = 200
MAX_IMAGE_SIZE_BYTES = 100000  # 100KB max

# GPS
GPS_BAUDRATE = 9600
GPS_TIMEOUT_SEC = 2.0

# Hardware watchdog timeout (seconds)
WATCHDOG_TIMEOUT_SEC = 60

# Maximum stored images on payload
MAX_STORED_IMAGES = 100
