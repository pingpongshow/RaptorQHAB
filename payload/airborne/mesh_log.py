"""
Record every Meshtastic packet the balloon hears, in cruise.

Nobody has much data on what a LoRa mesh looks like from 30 km. The balloon's
radio horizon up there is on the order of a 400-mile radius, and for the length
of a flight it is the best-placed receiver in the region. That is worth writing
down.

It is affordable because the limit is the channel, not the number of nodes.
A LongFast position beacon occupies 600 ms of airtime, so the band physically
cannot deliver more than about 1.7 packets a second however many thousands of
radios are in range. Measured against that ceiling, a four-hour flight is at
most a few megabytes, and realistically a few hundred kilobytes -- against a
card that already takes a 47 KB image every thirty seconds. Receiving costs
about 4.6 mA against a payload drawing 150, and cruise is 90% idle anyway.

Cruise only, deliberately. On the pad the recovery crew is standing next to the
balloon and the airtime belongs to imagery; once landed the battery belongs to
being found. Cruise is where the balloon is high, idle, and hearing things
nothing on the ground can hear.

Two decisions worth stating:

  - **Undecryptable traffic is logged too.** Most of what the balloon hears
    will be on channels it holds no key for, and it would be easy to treat that
    as noise. It is not: sender, signal strength and the balloon's own altitude
    at that moment are exactly the propagation data this is for. What was said
    matters less than that it was heard at all, and from how high.

  - **Every reception is a row, duplicates included.** A mesh redelivers the
    same packet by several paths; recording each arrival is how the rebroadcast
    pattern becomes visible. A `dup` column marks repeats so analysis can
    collapse them, but the timing is kept rather than thrown away.

The file is sealed like any other recording. This is a log of other people's
positions, and a stranger who recovers the payload should not be able to read
it.
"""

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

COLUMNS = [
    "timestamp", "balloon_alt_m", "sender", "destination", "packet_id",
    "port", "channel_hash", "rssi_dbm", "snr_db", "decrypted", "dup", "detail",
]

# How many packet ids to remember for duplicate marking. A mesh redelivers
# within seconds, so this only has to cover a short window -- and it is bounded
# because a flight hears far more distinct packets than a Pi Zero should hold.
SEEN_CAPACITY = 4096


@dataclass
class MeshLogStats:
    heard: int = 0
    decrypted: int = 0
    undecryptable: int = 0
    duplicates: int = 0
    write_errors: int = 0


class MeshtasticLog:
    """Appends one row per packet heard. Thread-safe; never raises at callers."""

    def __init__(self, path: str, sealed_writer=None, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self.stats = MeshLogStats()

        self._lock = threading.Lock()
        self._seen: "OrderedDict[int, int]" = OrderedDict()
        self._header_written = False

        from common.sealedwriter import SealedWriter
        self._writer = sealed_writer or SealedWriter(enabled=False)
        self.filepath = self._writer.path_for(path)

    def _ensure_header(self) -> None:
        if self._header_written:
            return
        self.filepath = self._writer.append_line(self.path, ",".join(COLUMNS) + "\n")
        self._header_written = True

    def record(self, packet, raw: bytes = b"", rssi: int = 0, snr: float = 0.0,
               altitude_m: Optional[float] = None, timestamp: Optional[float] = None) -> None:
        """
        Write one reception.

        `packet` is a decoded HeardPacket, or None when the balloon heard
        something it could not decrypt -- which is still worth a row.
        """
        if not self.enabled:
            return

        import time as _time
        now = _time.time() if timestamp is None else timestamp

        with self._lock:
            self.stats.heard += 1

            if packet is None:
                self.stats.undecryptable += 1
                row = [
                    f"{now:.3f}",
                    "" if altitude_m is None else f"{altitude_m:.1f}",
                    "", "", "", "", "",
                    str(int(rssi)), f"{snr:.2f}", "0", "0",
                    f"{len(raw)} bytes",
                ]
            else:
                self.stats.decrypted += 1
                dup = packet.packet_id in self._seen
                if dup:
                    self.stats.duplicates += 1
                self._seen[packet.packet_id] = self._seen.get(packet.packet_id, 0) + 1
                self._seen.move_to_end(packet.packet_id)
                while len(self._seen) > SEEN_CAPACITY:
                    self._seen.popitem(last=False)

                row = [
                    f"{now:.3f}",
                    "" if altitude_m is None else f"{altitude_m:.1f}",
                    f"{packet.sender:#010x}",
                    f"{packet.destination:#010x}",
                    str(packet.packet_id),
                    str(int(packet.port)),
                    str(packet.channel_hash),
                    str(int(packet.rssi or rssi)),
                    f"{(packet.snr if packet.snr is not None else snr):.2f}",
                    "1",
                    "1" if dup else "0",
                    _describe(packet),
                ]

            try:
                self._ensure_header()
                self._writer.append_line(self.path, ",".join(row) + "\n")
            except Exception as e:
                # A full card or a crypto fault must not disturb the listen
                # window, which is doing the balloon's actual job.
                self.stats.write_errors += 1
                if self.stats.write_errors <= 3:
                    logger.error(f"Could not write the mesh log: {e}")

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "path": self.filepath,
            "heard": self.stats.heard,
            "decrypted": self.stats.decrypted,
            "undecryptable": self.stats.undecryptable,
            "duplicates": self.stats.duplicates,
            "write_errors": self.stats.write_errors,
        }


def _describe(packet) -> str:
    """
    A short, CSV-safe note about what the packet was.

    Text is recorded because a message relayed through the balloon is the one
    thing a reader will want to see; commas and quotes are stripped rather than
    escaped, since this is a note and not a field anything parses.
    """
    from airborne.repeater import parse_text_message
    from common.meshtastic.messages import PortNum

    try:
        if packet.port == PortNum.TEXT_MESSAGE_APP:
            text = parse_text_message(packet.payload)
            clean = "".join(c for c in text if c.isprintable() and c not in ',"')
            return f"text: {clean[:80]}"
        if packet.port == PortNum.POSITION_APP:
            return "position"
        if packet.port == PortNum.NODEINFO_APP:
            return "nodeinfo"
        if packet.port == PortNum.TELEMETRY_APP:
            return "telemetry"
    except Exception:
        pass
    return f"port {int(packet.port)}"
