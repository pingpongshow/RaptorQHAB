"""
An attached Meshtastic node, used as a second receiver for the balloon.

The balloon beacons standard Meshtastic LoRa: LongFast parameters (SF11,
250 kHz, CR 4/5) on the standard 0x2B sync word, with the standard packet
header and AES-256-CTR channel encryption. Any stock Meshtastic radio hears it
-- no custom firmware, no patched build. This module speaks to such a radio
over its USB serial port and turns what it hears into positions and messages.

Two things worth knowing about the design:

Outgoing packets are built and encrypted here, then handed to the node in the
MeshPacket `encrypted` field. The node forwards them without re-encrypting, so
the channel key the ground station uses is the one that matters and the
attached radio does not have to be configured with the balloon's private
channel at all. That is what lets a borrowed node send a command.

Incoming packets may arrive either already decoded (field 4) or still
encrypted (field 8), depending on whether the node holds the key. Both paths
are handled, so a node that knows nothing about the private channel still
delivers the ciphertext for us to open.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import serial
import serial.tools.list_ports

from .meshtastic.crypto import decrypt_payload
from .meshtastic.messages import (
    PortNum, build_data, build_text_message, parse_data, parse_position,
    parse_text_message, parse_user, parse_device_metrics,
)
from .meshtastic.packet import (
    BROADCAST_ADDR, MeshHeader, build_packet, generate_packet_id,
    node_id_to_string,
)
from .meshtastic.protobuf import ProtobufReader, ProtobufWriter

logger = logging.getLogger(__name__)

# Serial framing: every ToRadio/FromRadio message is prefixed with this magic
# and a 16-bit big-endian length. Debug text shares the same port, so the
# reader has to pick framed messages out of a mixed stream.
FRAME_MAGIC = bytes([0x94, 0xC3])
MAX_FRAME = 512

# Meshtastic nodes present as USB serial. These are the common bridge chips.
NODE_HINTS = ("cp210", "ch340", "ch910", "silicon labs", "wch", "usb serial",
              "ttyusb", "ttyacm", "usbserial", "wchusbserial")


@dataclass
class MeshNode:
    """A node we have heard from."""
    node_id: int
    long_name: str = ""
    short_name: str = ""
    last_heard: float = 0.0
    snr: Optional[float] = None
    rssi: Optional[int] = None
    battery_percent: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None

    @property
    def display_name(self) -> str:
        return self.long_name or self.short_name or node_id_to_string(self.node_id)


@dataclass
class MeshMessage:
    timestamp: float
    sender: int
    sender_name: str
    destination: int
    text: str
    channel_hash: int
    rssi: Optional[int] = None
    snr: Optional[float] = None
    outgoing: bool = False


@dataclass
class ChannelConfig:
    """A channel the ground station can listen on and transmit to."""
    name: str
    key: bytes
    hash: int


def discover_meshtastic_ports() -> List[dict]:
    """Serial ports that plausibly have a Meshtastic node behind them."""
    found = []
    for port in serial.tools.list_ports.comports():
        blob = f"{port.device} {port.description or ''} {getattr(port, 'product', '') or ''}".lower()
        # Exclude the payload's own gadget and the ground modem, which sit on
        # the same bus and would otherwise look like candidates.
        if "raptorhab" in blob or "jtag" in blob:
            continue
        if any(hint in blob for hint in NODE_HINTS):
            found.append({"device": port.device,
                          "description": port.description or "serial device"})
    return found


class MeshtasticManager:
    """Talks to an attached Meshtastic node over USB serial."""

    def __init__(self):
        self._serial: Optional[serial.Serial] = None
        self._reader: Optional[threading.Thread] = None
        self._running = False
        self._buffer = bytearray()
        self._write_lock = threading.Lock()

        self.local_node_id: int = 0x7FFFFFFF     # replaced by the node's own id
        self.balloon_node_id: Optional[int] = None
        self.channels: List[ChannelConfig] = []

        self.nodes: Dict[int, MeshNode] = {}
        self.messages: List[MeshMessage] = []
        self.max_messages = 500

        self.packets_received = 0
        self.decrypt_failures = 0
        self.port: Optional[str] = None
        self.last_error: Optional[str] = None

        # Callbacks, all optional.
        self.on_message: Optional[Callable[[MeshMessage], None]] = None
        self.on_position: Optional[Callable[[int, dict], None]] = None
        self.on_node: Optional[Callable[[MeshNode], None]] = None

    # -- connection --------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def connect(self, port: str, baud: int = 115200) -> None:
        self.disconnect()
        self._serial = serial.Serial(port, baud, timeout=0.2)
        self.port = port
        self._buffer.clear()
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="meshtastic-node")
        self._reader.start()
        # Asking for the node's configuration makes it announce itself and
        # dump its node database, which is how we learn our own id.
        self._send_want_config()

    def disconnect(self) -> None:
        self._running = False
        if self._reader and self._reader.is_alive() and \
                threading.current_thread() is not self._reader:
            self._reader.join(timeout=1.0)
        self._reader = None
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        self.port = None

    # -- transmit ----------------------------------------------------------

    def _write_frame(self, to_radio: bytes) -> None:
        if not self.connected:
            raise ConnectionError("no Meshtastic node connected")
        frame = FRAME_MAGIC + bytes([(len(to_radio) >> 8) & 0xFF,
                                     len(to_radio) & 0xFF]) + to_radio
        with self._write_lock:
            self._serial.write(frame)
            self._serial.flush()

    def _send_want_config(self) -> None:
        writer = ProtobufWriter()
        writer.uint32(3, 1)          # ToRadio.want_config_id
        try:
            self._write_frame(writer.to_bytes())
        except Exception as exc:
            logger.debug(f"want_config failed: {exc}")

    def send_text(self, text: str, channel: ChannelConfig,
                  destination: int = BROADCAST_ADDR,
                  hop_limit: int = 3, want_ack: bool = False) -> MeshMessage:
        """
        Send a text message, encrypting it ourselves.

        The node relays the ciphertext untouched, so this works even when the
        attached radio has never been told the balloon's private channel key.
        """
        packet = build_packet(
            payload=build_data(PortNum.TEXT_MESSAGE_APP,
                               build_text_message(text)),
            sender=self.local_node_id,
            destination=destination,
            channel_key=channel.key,
            channel_hash=channel.hash,
            packet_id=generate_packet_id(),
            hop_limit=hop_limit,
            want_ack=want_ack,
        )
        self._write_frame(self._wrap_to_radio(packet, hop_limit, want_ack))

        message = MeshMessage(
            timestamp=time.time(), sender=self.local_node_id,
            sender_name="This ground station", destination=destination,
            text=text, channel_hash=channel.hash, outgoing=True,
        )
        self._record_message(message)
        return message

    def send_command_to_balloon(self, command: str,
                                private_channel: ChannelConfig) -> MeshMessage:
        """
        Send an uplink command.

        The payload only accepts commands that are on its private channel and
        addressed to it specifically, and that start with '!'. Anything else is
        treated as ordinary traffic, so this refuses to send something the
        balloon would silently ignore.
        """
        if self.balloon_node_id is None:
            raise ValueError("the balloon's node id is not known yet; "
                             "wait until it has been heard from")
        if not command.startswith("!"):
            raise ValueError("commands must start with '!' or the payload "
                             "will not treat them as commands")
        return self.send_text(command, private_channel,
                              destination=self.balloon_node_id,
                              hop_limit=0, want_ack=False)

    def _wrap_to_radio(self, mesh_packet: bytes, hop_limit: int,
                       want_ack: bool) -> bytes:
        """Wrap a built packet in the ToRadio envelope the node expects."""
        header = MeshHeader.deserialize(mesh_packet)
        body = mesh_packet[16:]

        packet = ProtobufWriter()
        packet.fixed32(1, header.sender)
        packet.fixed32(2, header.destination)
        packet.uint32(3, header.channel_hash)
        packet.bytes(8, bytes(body))              # encrypted
        packet.fixed32(6, header.packet_id)
        packet.uint32(10, hop_limit)
        if want_ack:
            packet.bool(11, True)

        to_radio = ProtobufWriter()
        to_radio.bytes(1, packet.to_bytes())      # ToRadio.packet
        return to_radio.to_bytes()

    # -- receive -----------------------------------------------------------

    def _read_loop(self) -> None:
        while self._running and self._serial:
            try:
                data = self._serial.read(4096)
            except Exception as exc:
                self.last_error = str(exc)
                self._running = False
                return
            if not data:
                continue
            self._buffer.extend(data)
            for frame in self._extract_frames():
                try:
                    self._handle_from_radio(frame)
                except Exception:
                    logger.debug("malformed FromRadio frame", exc_info=True)

    def _extract_frames(self) -> List[bytes]:
        """Pull complete frames out of a stream that also carries debug text."""
        frames = []
        while True:
            start = self._buffer.find(FRAME_MAGIC)
            if start < 0:
                # Keep only a possible partial magic at the tail.
                if len(self._buffer) > 1:
                    del self._buffer[:-1]
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 4:
                break
            length = (self._buffer[2] << 8) | self._buffer[3]
            if length > MAX_FRAME:
                # Not a real frame; skip this magic and resynchronise.
                del self._buffer[:2]
                continue
            if len(self._buffer) < 4 + length:
                break
            frames.append(bytes(self._buffer[4:4 + length]))
            del self._buffer[:4 + length]
        return frames

    def _handle_from_radio(self, data: bytes) -> None:
        fields = ProtobufReader(data).to_dict()

        # FromRadio.my_info (field 3) carries our own node number.
        if 3 in fields:
            info = ProtobufReader(fields[3][-1]).to_dict()
            if 1 in info:
                self.local_node_id = int.from_bytes(info[1][-1], "little") \
                    if isinstance(info[1][-1], bytes) else int(info[1][-1])

        # FromRadio.node_info (field 4): the node database dump.
        if 4 in fields:
            self._handle_node_info(fields[4][-1])

        # FromRadio.packet (field 2): traffic from the air.
        if 2 in fields:
            self.packets_received += 1
            self._handle_mesh_packet(fields[2][-1])

    def _handle_node_info(self, raw: bytes) -> None:
        try:
            fields = ProtobufReader(raw).to_dict()
        except Exception:
            return
        num = fields.get(1, [None])[-1]
        if num is None:
            return
        node_id = int(num) if not isinstance(num, bytes) else int.from_bytes(num, "little")
        node = self.nodes.setdefault(node_id, MeshNode(node_id=node_id))
        if 2 in fields:
            try:
                user = parse_user(fields[2][-1])
                node.long_name = user.long_name or node.long_name
                node.short_name = user.short_name or node.short_name
            except Exception:
                pass
        node.last_heard = time.time()
        if self.on_node:
            self.on_node(node)

    def _handle_mesh_packet(self, raw: bytes) -> None:
        fields = ProtobufReader(raw).to_dict()

        def scalar(number, default=0):
            values = fields.get(number)
            if not values:
                return default
            value = values[-1]
            if isinstance(value, bytes):
                return int.from_bytes(value, "little")
            return int(value)

        sender = scalar(1)
        destination = scalar(2, BROADCAST_ADDR)
        channel_hash = scalar(3)
        packet_id = scalar(6)
        rssi = scalar(7, 0) or None
        snr = None
        if 9 in fields:
            try:
                snr = float(fields[9][-1])
            except Exception:
                snr = None

        payload: Optional[bytes] = None
        if 4 in fields:                     # already decoded by the node
            payload = fields[4][-1]
        elif 8 in fields:                   # still encrypted: open it ourselves
            ciphertext = fields[8][-1]
            for channel in self.channels:
                if channel.hash != channel_hash:
                    continue
                try:
                    candidate = decrypt_payload(channel.key, packet_id,
                                                sender, ciphertext)
                    parse_data(candidate)   # rejects a wrong key as garbage
                    payload = candidate
                    break
                except Exception:
                    continue
            if payload is None:
                self.decrypt_failures += 1
                return

        if payload is None:
            return

        try:
            decoded = parse_data(payload)
        except Exception:
            return

        node = self.nodes.setdefault(sender, MeshNode(node_id=sender))
        node.last_heard = time.time()
        node.snr = snr if snr is not None else node.snr
        node.rssi = rssi if rssi is not None else node.rssi

        self._dispatch(decoded, node, sender, destination, channel_hash,
                       rssi, snr)

    def _dispatch(self, decoded, node, sender, destination, channel_hash,
                  rssi, snr) -> None:
        portnum = decoded.portnum

        if portnum == PortNum.TEXT_MESSAGE_APP:
            message = MeshMessage(
                timestamp=time.time(), sender=sender,
                sender_name=node.display_name, destination=destination,
                text=parse_text_message(decoded.payload),
                channel_hash=channel_hash, rssi=rssi, snr=snr,
            )
            self._record_message(message)

        elif portnum == PortNum.POSITION_APP:
            position = parse_position(decoded.payload)
            if position.latitude is None or position.longitude is None:
                return
            node.latitude = position.latitude
            node.longitude = position.longitude
            node.altitude = position.altitude_m
            if self.on_position:
                self.on_position(sender, {
                    "latitude": position.latitude,
                    "longitude": position.longitude,
                    "altitude": position.altitude_m or 0.0,
                    "satellites": getattr(position, "satellites", None),
                    "timestamp": getattr(position, "timestamp", None),
                    "rssi": rssi,
                    "snr": snr,
                    "name": node.display_name,
                })

        elif portnum == PortNum.TELEMETRY_APP:
            try:
                metrics = parse_device_metrics(decoded.payload)
                node.battery_percent = metrics.battery_level
            except Exception:
                pass

        elif portnum == PortNum.NODEINFO_APP:
            try:
                user = parse_user(decoded.payload)
                node.long_name = user.long_name or node.long_name
                node.short_name = user.short_name or node.short_name
            except Exception:
                pass

        if self.on_node:
            self.on_node(node)

    def _record_message(self, message: MeshMessage) -> None:
        self.messages.append(message)
        if len(self.messages) > self.max_messages:
            del self.messages[:len(self.messages) - self.max_messages]
        if self.on_message:
            self.on_message(message)

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "port": self.port,
            "local_node_id": node_id_to_string(self.local_node_id),
            "balloon_node_id": (node_id_to_string(self.balloon_node_id)
                                if self.balloon_node_id else None),
            "packets_received": self.packets_received,
            "decrypt_failures": self.decrypt_failures,
            "nodes": len(self.nodes),
            "messages": len(self.messages),
            "channels": [c.name for c in self.channels],
            "last_error": self.last_error,
        }
