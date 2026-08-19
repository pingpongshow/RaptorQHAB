"""
Meshtastic radio packet framing.

Every Meshtastic LoRa transmission is a 16-byte header followed by the
encrypted payload:

    offset  size  field
    0       4     destination node id   (uint32 LE, 0xFFFFFFFF = broadcast)
    4       4     sender node id        (uint32 LE)
    8       4     packet id             (uint32 LE)
    12      1     flags                 (hop_limit, want_ack, via_mqtt, hop_start)
    13      1     channel hash
    14      1     next-hop              (last byte of the next hop's node id)
    15      1     relay node            (last byte of the relaying node's id)

Flags byte layout:
    bits 0-2  hop_limit    remaining hops
    bit  3    want_ack
    bit  4    via_mqtt
    bits 5-7  hop_start    hop_limit as originally sent
"""

import logging
import os
import struct
from dataclasses import dataclass, field
from typing import Optional

from .crypto import decrypt_payload, encrypt_payload

logger = logging.getLogger(__name__)

HEADER_SIZE = 16
BROADCAST_ADDR = 0xFFFFFFFF

# The SX1262 caps a LoRa packet at 255 bytes.
MAX_LORA_PACKET = 255
MAX_PAYLOAD_SIZE = MAX_LORA_PACKET - HEADER_SIZE

FLAG_HOP_LIMIT_MASK = 0x07
FLAG_WANT_ACK = 0x08
FLAG_VIA_MQTT = 0x10
FLAG_HOP_START_SHIFT = 5
FLAG_HOP_START_MASK = 0xE0


class MeshPacketError(ValueError):
    """Raised on a malformed Meshtastic packet."""


@dataclass
class MeshHeader:
    """The 16-byte Meshtastic packet header."""

    destination: int = BROADCAST_ADDR
    sender: int = 0
    packet_id: int = 0
    hop_limit: int = 0
    want_ack: bool = False
    via_mqtt: bool = False
    hop_start: int = 0
    channel_hash: int = 0
    next_hop: int = 0
    relay_node: int = 0

    def __post_init__(self):
        if not 0 <= self.hop_limit <= 7:
            raise ValueError(f"hop_limit must be 0-7, got {self.hop_limit}")
        if not 0 <= self.hop_start <= 7:
            raise ValueError(f"hop_start must be 0-7, got {self.hop_start}")

    @property
    def is_broadcast(self) -> bool:
        return self.destination == BROADCAST_ADDR

    def serialize(self) -> bytes:
        flags = self.hop_limit & FLAG_HOP_LIMIT_MASK
        if self.want_ack:
            flags |= FLAG_WANT_ACK
        if self.via_mqtt:
            flags |= FLAG_VIA_MQTT
        flags |= (self.hop_start << FLAG_HOP_START_SHIFT) & FLAG_HOP_START_MASK

        return struct.pack(
            "<IIIBBBB",
            self.destination & 0xFFFFFFFF,
            self.sender & 0xFFFFFFFF,
            self.packet_id & 0xFFFFFFFF,
            flags,
            self.channel_hash & 0xFF,
            self.next_hop & 0xFF,
            self.relay_node & 0xFF,
        )

    @classmethod
    def deserialize(cls, data: bytes) -> "MeshHeader":
        if len(data) < HEADER_SIZE:
            raise MeshPacketError(
                f"header needs {HEADER_SIZE} bytes, got {len(data)}"
            )

        dest, sender, packet_id, flags, chan, next_hop, relay = struct.unpack(
            "<IIIBBBB", data[:HEADER_SIZE]
        )

        return cls(
            destination=dest,
            sender=sender,
            packet_id=packet_id,
            hop_limit=flags & FLAG_HOP_LIMIT_MASK,
            want_ack=bool(flags & FLAG_WANT_ACK),
            via_mqtt=bool(flags & FLAG_VIA_MQTT),
            hop_start=(flags & FLAG_HOP_START_MASK) >> FLAG_HOP_START_SHIFT,
            channel_hash=chan,
            next_hop=next_hop,
            relay_node=relay,
        )


@dataclass
class MeshPacket:
    """A complete Meshtastic packet: header plus (encrypted) payload."""

    header: MeshHeader
    payload: bytes = b""

    def serialize(self) -> bytes:
        packet = self.header.serialize() + self.payload
        if len(packet) > MAX_LORA_PACKET:
            raise MeshPacketError(
                f"packet is {len(packet)} bytes, over the {MAX_LORA_PACKET}-byte "
                f"LoRa limit"
            )
        return packet

    @classmethod
    def deserialize(cls, data: bytes) -> "MeshPacket":
        header = MeshHeader.deserialize(data)
        return cls(header=header, payload=data[HEADER_SIZE:])


def generate_packet_id() -> int:
    """
    A random 32-bit packet id.

    Meshtastic uses the packet id for duplicate suppression across the mesh
    and as part of the AES counter block, so it must not repeat and must not
    be predictable enough to collide with another node's sequence. Random is
    what the firmware does.
    """
    return struct.unpack("<I", os.urandom(4))[0]


def node_id_from_callsign(callsign: str, payload_id: int = 0) -> int:
    """
    Derive a stable 32-bit Meshtastic node id from the payload's callsign.

    Deterministic on purpose: the balloon keeps the same identity across
    reflashes and across flights, so receivers accumulate a continuous history
    for it rather than seeing an unfamiliar node every launch.

    Node ids 0 and 0xFFFFFFFF are reserved (unset and broadcast), and the
    Meshtastic convention reserves the low range for special use, so the
    result is folded into the range above 0x1000.
    """
    import hashlib

    seed = f"{callsign.strip().upper()}#{payload_id}".encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    value = struct.unpack("<I", digest[:4])[0]

    # Keep clear of the reserved low ids and of the broadcast address.
    span = 0xFFFFFFFE - 0x00001000
    return 0x00001000 + (value % span)


def node_id_to_string(node_id: int) -> str:
    """Format a node id the way Meshtastic displays it: !a1b2c3d4."""
    return f"!{node_id & 0xFFFFFFFF:08x}"


def short_name_from_callsign(callsign: str) -> str:
    """
    A four-character short name, which is what appears on a handheld's map
    pin. Meshtastic truncates anything longer.
    """
    cleaned = "".join(c for c in callsign.upper() if c.isalnum())
    return (cleaned[-4:] if len(cleaned) > 4 else cleaned) or "HAB"


def build_packet(
    payload: bytes,
    sender: int,
    destination: int = BROADCAST_ADDR,
    channel_key: bytes = b"",
    channel_hash: int = 0,
    hop_limit: int = 0,
    want_ack: bool = False,
    packet_id: Optional[int] = None,
) -> bytes:
    """
    Build a complete, encrypted Meshtastic packet ready for the radio.

    Args:
        payload: Serialized Data protobuf.
        sender: This node's id.
        destination: Target node id, or BROADCAST_ADDR.
        channel_key: Expanded AES key; empty means send in the clear.
        channel_hash: Channel hash byte, from crypto.channel_hash().
        hop_limit: Remaining hops. Zero means no other node rebroadcasts it,
            which is what a balloon with a several-hundred-mile footprint
            should almost always use.
        want_ack: Request an acknowledgement.
        packet_id: Explicit id, otherwise randomly generated.

    Raises:
        MeshPacketError: if the resulting packet exceeds the LoRa limit.
    """
    if packet_id is None:
        packet_id = generate_packet_id()

    if len(payload) > MAX_PAYLOAD_SIZE:
        raise MeshPacketError(
            f"payload is {len(payload)} bytes; the maximum after the "
            f"{HEADER_SIZE}-byte header is {MAX_PAYLOAD_SIZE}"
        )

    body = payload
    if channel_key:
        body = encrypt_payload(channel_key, packet_id, sender, payload)

    header = MeshHeader(
        destination=destination,
        sender=sender,
        packet_id=packet_id,
        hop_limit=hop_limit,
        hop_start=hop_limit,
        want_ack=want_ack,
        channel_hash=channel_hash,
    )

    return MeshPacket(header=header, payload=body).serialize()


def parse_packet(data: bytes, channel_key: bytes = b"") -> "ParsedPacket":
    """
    Parse a received Meshtastic packet and decrypt its payload.

    Decryption cannot fail loudly: AES-CTR with the wrong key yields plausible
    bytes rather than an error. Callers must validate the decrypted payload as
    a protobuf before trusting it.
    """
    packet = MeshPacket.deserialize(data)

    plaintext = packet.payload
    if channel_key and packet.payload:
        plaintext = decrypt_payload(
            channel_key, packet.header.packet_id, packet.header.sender, packet.payload
        )

    return ParsedPacket(header=packet.header, payload=plaintext, raw=data)


@dataclass
class ParsedPacket:
    """A received packet with its payload decrypted."""

    header: MeshHeader
    payload: bytes
    raw: bytes = field(default=b"", repr=False)

    @property
    def sender_string(self) -> str:
        return node_id_to_string(self.header.sender)
