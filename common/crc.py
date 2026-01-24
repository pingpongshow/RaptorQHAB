"""
RaptorHab CRC-32 Implementation
Standard CRC-32 (IEEE 802.3 polynomial)
"""

import struct

# CRC-32 polynomial (IEEE 802.3)
CRC32_POLYNOMIAL = 0xEDB88320

# Pre-computed CRC-32 lookup table
_crc32_table = None


def _init_crc32_table() -> list:
    """Initialize CRC-32 lookup table"""
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ CRC32_POLYNOMIAL
            else:
                crc >>= 1
        table.append(crc)
    return table


def crc32(data: bytes, initial: int = 0xFFFFFFFF) -> int:
    """
    Calculate CRC-32 checksum
    
    Args:
        data: Input bytes
        initial: Initial CRC value (default 0xFFFFFFFF)
        
    Returns:
        32-bit CRC value
    """
    global _crc32_table
    
    if _crc32_table is None:
        _crc32_table = _init_crc32_table()
    
    crc = initial
    for byte in data:
        crc = _crc32_table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    
    return crc ^ 0xFFFFFFFF


def crc32_bytes(data: bytes, initial: int = 0xFFFFFFFF) -> bytes:
    """
    Calculate CRC-32 and return as 4 bytes (big-endian)
    
    Args:
        data: Input bytes
        initial: Initial CRC value
        
    Returns:
        4-byte CRC value (big-endian)
    """
    return struct.pack('>I', crc32(data, initial))


def verify_crc32(data: bytes, expected_crc: int) -> bool:
    """
    Verify CRC-32 checksum
    
    Args:
        data: Input bytes (without CRC)
        expected_crc: Expected CRC value
        
    Returns:
        True if CRC matches
    """
    return crc32(data) == expected_crc


def verify_crc32_packet(packet: bytes) -> bool:
    """
    Verify CRC-32 for a complete packet (CRC at end)
    
    Args:
        packet: Complete packet with CRC-32 at the end (4 bytes)
        
    Returns:
        True if CRC matches
    """
    if len(packet) < 4:
        return False
    
    data = packet[:-4]
    expected_crc = struct.unpack('>I', packet[-4:])[0]
    
    return crc32(data) == expected_crc
