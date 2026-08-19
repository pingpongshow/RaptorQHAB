"""
Meshtastic over the RaptorHAB modem.

The dual-E22 modem carries two radios: one listening for RAPTOR image traffic,
one sitting on the Meshtastic channel. It forwards whole LoRa packets, still
encrypted, on their own frame delimiter, and it accepts packets to transmit.

The modem holds no channel keys, deliberately -- a borrowed board never carries
them. So everything Meshtastic-shaped happens here: decrypt, parse, and on the
way out, build and encrypt.

This is the ground station's *own* radio, which is what makes it different from
the two Meshtastic sources that already exist. A node plugged into the USB port
hears what a node on the ground hears. MQTT hears what the internet has been
told. This hears the balloon directly, at whatever range the ground station's
own antenna reaches, with no node and no internet in between -- and it is the
only one of the three that can transmit to the balloon on the private channel
without a second radio.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .meshtastic.crypto import channel_hash, expand_psk, parse_psk
from .meshtastic.messages import PortNum, build_data, build_text_message, parse_data
from .meshtastic.packet import (
    BROADCAST_ADDR,
    build_packet,
    generate_packet_id,
    node_id_from_callsign,
    node_id_to_string,
    parse_packet,
)

logger = logging.getLogger(__name__)

# The modem answers MTX: with one of these.
TX_OK = "MTX_OK"
TX_ERR = "MTX_ERR"


@dataclass
class HeardPacket:
    """One Meshtastic packet the ground station's own radio heard."""

    sender: int
    destination: int
    packet_id: int
    port: int
    payload: bytes
    channel_hash: int
    rssi: float
    snr: float
    received_at: float
    text: Optional[str] = None
    decrypted: bool = False

    @property
    def sender_id(self) -> str:
        return node_id_to_string(self.sender)


@dataclass
class LinkStats:
    heard: int = 0
    decrypted: int = 0
    undecryptable: int = 0
    malformed: int = 0
    sent: int = 0
    send_failed: int = 0


class ModemMeshtasticLink:
    """
    Decodes Meshtastic packets from the modem, and transmits through it.

    Transmit is synchronous by design: the modem replies MTX_OK or MTX_ERR and
    the caller wants to know which. An uplink to a balloon is not fire and
    forget -- if it did not leave the ground station, the operator needs to see
    that immediately rather than wonder why nothing happened.
    """

    def __init__(
        self,
        writer: Optional[Callable[[bytes], bool]] = None,
        callsign: str = "GROUND",
        channel_name: str = "LongFast",
        channel_key: str = "AQ==",
        private_channel_name: str = "",
        private_channel_key: str = "",
    ):
        """
        Args:
            writer: Sends bytes to the modem. Injectable so tests never need a
                serial port.
            callsign: Ours, for the node id we transmit under.
            channel_name/channel_key: The public channel, LongFast by default
                so any stock radio hears us.
            private_channel_name/private_channel_key: Optional second channel.
                Commands to the balloon go here -- the payload refuses them on
                the public channel, because anyone can transmit there.
        """
        self._writer = writer
        self.callsign = callsign
        self.node_id = node_id_from_callsign(callsign)

        self.channels: List[Tuple[str, bytes]] = []
        self._add_channel(channel_name, channel_key)
        if private_channel_name and private_channel_key:
            self._add_channel(private_channel_name, private_channel_key)

        self.stats = LinkStats()
        self._lock = threading.Lock()
        self._pending_reply: Optional[str] = None
        self._reply_event = threading.Event()

        self.on_packet: Optional[Callable[[HeardPacket], None]] = None

    def _add_channel(self, name: str, key: str) -> None:
        try:
            # parse_psk gives the PSK as configured; expand_psk turns
            # Meshtastic's one-byte shorthand ("AQ==" for the default LongFast
            # key) into the actual AES key. Skipping the expansion hands AES a
            # one-byte key and everything downstream fails on a channel the
            # operator has configured perfectly correctly.
            self.channels.append((name, expand_psk(parse_psk(key))))
        except ValueError as e:
            # A bad key must not be silently equivalent to no key: the operator
            # asked for that channel and would otherwise never find out.
            logger.error(f"Meshtastic channel {name!r} key is invalid ({e}); skipping it")

    @property
    def private_channel(self) -> Optional[Tuple[str, bytes]]:
        return self.channels[1] if len(self.channels) > 1 else None

    def set_writer(self, writer: Optional[Callable[[bytes], bool]]) -> None:
        self._writer = writer

    # -- receive ------------------------------------------------------------

    def handle_frames(
        self, frames: List[Tuple[float, float, bytes]]
    ) -> List[HeardPacket]:
        """
        Decode frames taken from the modem, newest last.

        Tries every configured channel. A packet on a channel we do not hold
        the key for is counted, not discarded silently -- hearing traffic you
        cannot read is useful information about whether the radio is working.
        """
        heard = []
        for rssi, snr, raw in frames:
            packet = self._decode(raw, rssi, snr)
            if packet is not None:
                heard.append(packet)
                if self.on_packet:
                    try:
                        self.on_packet(packet)
                    except Exception as e:
                        logger.error(f"Meshtastic packet handler failed: {e}")
        return heard

    def _decode(self, raw: bytes, rssi: float, snr: float) -> Optional[HeardPacket]:
        self.stats.heard += 1

        for name, key in self.channels:
            try:
                parsed = parse_packet(raw, key)
            except Exception:
                continue

            data = None
            try:
                data = parse_data(parsed.payload) if parsed.payload else None
            except Exception:
                data = None

            if data is None:
                continue

            text = None
            if data.portnum == PortNum.TEXT_MESSAGE_APP:
                try:
                    text = data.payload.decode("utf-8", errors="replace")
                except Exception:
                    text = None

            self.stats.decrypted += 1
            return HeardPacket(
                sender=parsed.header.sender,
                destination=parsed.header.destination,
                packet_id=parsed.header.packet_id,
                port=int(data.portnum),
                payload=data.payload,
                channel_hash=parsed.header.channel_hash,
                rssi=rssi,
                snr=snr,
                received_at=time.time(),
                text=text,
                decrypted=True,
            )

        # Heard, but not for us -- another channel, or another mesh entirely.
        self.stats.undecryptable += 1
        return None

    # -- transmit -----------------------------------------------------------

    def send_text(
        self,
        text: str,
        destination: int = BROADCAST_ADDR,
        private: bool = False,
        hop_limit: int = 3,
        timeout_sec: float = 5.0,
    ) -> Tuple[bool, str]:
        """
        Transmit a text message. Returns (sent, detail).

        `private=True` uses the private channel, which is the only one the
        balloon accepts commands on.
        """
        if private and self.private_channel is None:
            return False, "no private channel is configured"

        name, key = self.private_channel if private else self.channels[0]
        packet = build_packet(
            build_data(PortNum.TEXT_MESSAGE_APP, build_text_message(text)),
            sender=self.node_id,
            destination=destination,
            channel_key=key,
            channel_hash=channel_hash(name, key),
            hop_limit=hop_limit,
            packet_id=generate_packet_id(),
        )
        return self.send_raw(packet, timeout_sec=timeout_sec)

    def send_command(self, command: str, timeout_sec: float = 5.0) -> Tuple[bool, str]:
        """
        Send an uplink command to the balloon on the private channel.

        Commands go nowhere else. The payload refuses them on the public
        channel because anyone can transmit there, so sending one publicly
        would be a message the balloon reads and ignores -- and rebroadcasts
        to the whole mesh if it were tagged for relaying.
        """
        if self.private_channel is None:
            return False, (
                "commands need a private channel; the balloon refuses them on "
                "the public one because anyone can transmit there"
            )
        text = command if command.startswith("!") else f"!{command}"
        return self.send_text(text, private=True, hop_limit=0, timeout_sec=timeout_sec)

    def send_raw(self, packet: bytes, timeout_sec: float = 5.0) -> Tuple[bool, str]:
        """Hand an already-built packet to the modem and wait for its verdict."""
        if self._writer is None:
            self.stats.send_failed += 1
            return False, "no modem connected"

        if len(packet) > 255:
            self.stats.send_failed += 1
            return False, f"packet is {len(packet)} bytes; the radio takes 255"

        line = f"MTX:{packet.hex()}\n".encode("ascii")

        with self._lock:
            self._pending_reply = None
            self._reply_event.clear()
            if not self._writer(line):
                self.stats.send_failed += 1
                return False, "write to the modem failed"

        if not self._reply_event.wait(timeout_sec):
            self.stats.send_failed += 1
            return False, f"the modem did not answer within {timeout_sec:.0f}s"

        reply = self._pending_reply or ""
        if reply.startswith(TX_OK):
            self.stats.sent += 1
            return True, "sent"

        self.stats.send_failed += 1
        return False, reply or "the modem refused it"

    def handle_modem_line(self, line: str) -> bool:
        """
        Feed the modem's text output in. Returns True if it was ours.

        The transmit reply arrives on the same text channel as everything else
        the modem prints, so the serial layer passes each line through here.
        """
        stripped = line.strip()
        if not (stripped.startswith(TX_OK) or stripped.startswith(TX_ERR)):
            return False
        self._pending_reply = stripped
        self._reply_event.set()
        return True

    # -- configuration ------------------------------------------------------

    def configure_slot(
        self,
        frequency_mhz: float,
        bandwidth_khz: float = 250.0,
        spreading_factor: int = 11,
        coding_rate: int = 5,
        power_dbm: int = 30,
    ) -> bool:
        """
        Point the modem's Meshtastic radio at a region's channel.

        Sent as MCFG:. Without it the modem uses its built-in default, which is
        correct for exactly one region.
        """
        if self._writer is None:
            return False
        line = (
            f"MCFG:{frequency_mhz:.4f},{bandwidth_khz:.1f},"
            f"{spreading_factor},{coding_rate},{power_dbm}\n"
        )
        return bool(self._writer(line.encode("ascii")))

    def get_status(self) -> dict:
        return {
            "callsign": self.callsign,
            "node_id": node_id_to_string(self.node_id),
            "channels": [name for name, _ in self.channels],
            "has_private_channel": self.private_channel is not None,
            "can_transmit": self._writer is not None,
            "heard": self.stats.heard,
            "decrypted": self.stats.decrypted,
            "undecryptable": self.stats.undecryptable,
            "sent": self.stats.sent,
            "send_failed": self.stats.send_failed,
        }
