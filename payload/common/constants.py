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
FLIGHT_SUMMARY_PAYLOAD_SIZE = 30


class PacketType(IntEnum):
    """Packet type identifiers"""
    # Air -> Ground
    TELEMETRY = 0x00
    IMAGE_META = 0x01
    IMAGE_DATA = 0x02
    TEXT_MSG = 0x03
    FLIGHT_SUMMARY = 0x04
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


# -----------------------------------------------------------------------
# Operational defaults
#
# NOTE: The authoritative source for every *configurable* payload setting is
# airborne/config.py, which is what the persisted config file and the USB
# configuration UI operate on. This module previously carried a second,
# divergent copy of those values (bitrate 200000 vs 96000, fdev 125000 vs
# 50000, tx period 10 vs 2) which was never read by anything and only served
# to mislead. Only values that are genuinely shared protocol constants, or
# that are used as fallback defaults by shared code, remain here.
# -----------------------------------------------------------------------

# Packet scheduling fallbacks, used only when PacketScheduler is constructed
# without explicit intervals. These two must not be multiples of one another:
# the scheduler offers the rarer image-metadata slot first, but keeping them
# coprime avoids the two slots repeatedly colliding.
TELEMETRY_INTERVAL_PACKETS = 10
IMAGE_META_INTERVAL_PACKETS = 101

# Fountain symbol size, in bytes.
#
# This is load-bearing for the receive path: protocol._get_expected_payload_length
# uses it to work out how much of a padded SX1262 receive buffer belongs to an
# IMAGE_DATA packet. The airborne fountain_symbol_size setting MUST match this
# value, and both ends must agree, or image packets fail CRC and are discarded.
FOUNTAIN_SYMBOL_SIZE = 200

# A RaptorQ encoded packet is the symbol plus a 4-byte payload identifier
# (SBN + ESI, RFC 6330 section 3.2). The LT encoder emits a bare symbol with
# no prefix, so IMAGE_DATA packets are one of two sizes on the wire depending
# on which encoder produced them, and the receive path must accept both.
RAPTORQ_PAYLOAD_ID_SIZE = 4

# IMAGE_DATA payload prefix: image_id(2) + symbol_id(4)
IMAGE_DATA_HEADER_SIZE = 6

# Largest image the payload will attempt to transmit.
MAX_IMAGE_SIZE_BYTES = 100000  # 100KB max

# GPS read timeout.
GPS_TIMEOUT_SEC = 2.0
