"""
Framing for the USB link between the payload and the companion app.

One serial line carries two independent conversations -- a JSON configuration
API and a raw terminal -- so the bytes need a frame that says which is which.
Without it, console output would interleave into the middle of a JSON reply
and corrupt both.

Frame layout:

    offset  size  field
    0       2     magic, 0x52 0x48 ("RH")
    2       1     channel
    3       1     flags (reserved, must be 0)
    4       4     payload length, big-endian uint32
    8       n     payload
    8+n     4     CRC-32 of everything from offset 2 to the end of the payload

The magic lets a reader resynchronise after a partial frame -- which happens
routinely, because a serial line has no notion of message boundaries and the
host may connect halfway through a transmission. The CRC catches the case
where random console bytes happen to look like a magic sequence.

Deliberately not a text protocol: the console channel carries arbitrary bytes
including newlines and control characters, so any line-oriented framing would
need escaping, and escaping a terminal stream is a reliable source of subtle
bugs.
"""

import struct
import zlib
from enum import IntEnum
from typing import List, Optional, Tuple

MAGIC = b"\x52\x48"  # "RH"
HEADER_SIZE = 8
CRC_SIZE = 4
OVERHEAD = HEADER_SIZE + CRC_SIZE

# Bounded so a corrupt length field cannot make the reader allocate wildly.
# The largest legitimate payload is a config schema, tens of kilobytes.
MAX_PAYLOAD = 1 << 20  # 1 MiB


class Channel(IntEnum):
    """Which conversation a frame belongs to."""

    CONTROL = 0   # JSON request/response
    CONSOLE = 1   # raw PTY bytes
    EVENT = 2     # unsolicited JSON from the payload (telemetry, log lines)


class FrameError(ValueError):
    """Raised on a frame that cannot be trusted."""


def encode_frame(channel: int, payload: bytes) -> bytes:
    """Wrap a payload in a frame."""
    if len(payload) > MAX_PAYLOAD:
        raise FrameError(
            f"payload of {len(payload)} bytes exceeds the {MAX_PAYLOAD}-byte limit"
        )

    body = struct.pack(">BBI", channel & 0xFF, 0, len(payload)) + payload
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return MAGIC + body + struct.pack(">I", crc)


class FrameDecoder:
    """
    Incremental frame reader for a byte stream.

    Feed it whatever arrives; it yields complete frames and buffers the rest.
    Resynchronises on garbage by scanning forward for the next magic, which is
    what makes connecting mid-stream work.
    """

    def __init__(self, max_buffer: int = 4 * MAX_PAYLOAD):
        self._buffer = bytearray()
        self._max_buffer = max_buffer
        self.resyncs = 0
        self.crc_errors = 0

    def feed(self, data: bytes) -> List[Tuple[int, bytes]]:
        """
        Add received bytes and return any complete frames.

        Returns:
            A list of (channel, payload) tuples, possibly empty.
        """
        self._buffer.extend(data)

        # A buffer this large means we are reading noise, not frames. Drop the
        # oldest half rather than growing without bound.
        if len(self._buffer) > self._max_buffer:
            del self._buffer[: len(self._buffer) // 2]
            self.resyncs += 1

        frames = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return frames
            frames.append(frame)

    def _next_frame(self) -> Optional[Tuple[int, bytes]]:
        while True:
            if len(self._buffer) < HEADER_SIZE:
                return None

            if not self._buffer.startswith(MAGIC):
                if not self._resync():
                    return None
                continue

            channel, flags, length = struct.unpack_from(">BBI", self._buffer, 2)

            if length > MAX_PAYLOAD:
                # A length this large is corruption, not a real frame.
                self._discard_magic()
                continue

            total = OVERHEAD + length
            if len(self._buffer) < total:
                return None  # incomplete; wait for more

            body = bytes(self._buffer[2 : HEADER_SIZE + length])
            expected = struct.unpack_from(">I", self._buffer, HEADER_SIZE + length)[0]

            if (zlib.crc32(body) & 0xFFFFFFFF) != expected:
                self.crc_errors += 1
                self._discard_magic()
                continue

            payload = bytes(self._buffer[HEADER_SIZE:HEADER_SIZE + length])
            del self._buffer[:total]
            return channel, payload

    def _resync(self) -> bool:
        """Scan forward to the next plausible frame start."""
        index = self._buffer.find(MAGIC, 1)
        if index < 0:
            # Keep the last byte: it could be the first half of a magic that
            # straddles this read and the next.
            keep = 1 if self._buffer.endswith(MAGIC[:1]) else 0
            if len(self._buffer) > keep:
                self.resyncs += 1
            del self._buffer[: len(self._buffer) - keep]
            return False

        del self._buffer[:index]
        self.resyncs += 1
        return True

    def _discard_magic(self) -> None:
        """Drop the leading magic so _resync can find the next candidate."""
        del self._buffer[:2]

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()
