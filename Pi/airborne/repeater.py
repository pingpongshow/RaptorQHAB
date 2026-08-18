"""
Selective Meshtastic repeating and uplink command handling.

From 30 km up the balloon hears an enormous number of nodes -- its horizon is
on the order of a 400-mile radius. Repeating everything it hears would flatten
the battery and congest regional meshes, which is a real and documented
problem with high-altitude LoRa. So this repeats only what is explicitly
addressed to it.

Two ways to ask for a repeat:

    1. Send a text message to the balloon's node id.
    2. Broadcast a message whose text begins with the configured tag,
       "!RPT " by default.

Everything else the balloon hears is counted and dropped.

Rebroadcasts go out with hop_limit 0, exactly like the balloon's own beacons:
one very high, very well-heard hop, and nothing forwards it onward.

Uplink commands are separate and deliberately narrow. A message addressed to
the balloon on the *private* channel can run one of a short allowlist. Nothing
on the public channel can command anything, whatever it says.
"""

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from common.meshtastic import (
    BROADCAST_ADDR,
    PortNum,
    build_data,
    build_packet,
    build_text_message,
    parse_data,
    parse_packet,
)
from common.meshtastic.messages import parse_text_message

logger = logging.getLogger(__name__)


class DropReason(str, Enum):
    """Why a heard packet was not repeated. All of these are normal."""

    NOT_TAGGED = "not_tagged"          # the overwhelming majority
    ALREADY_SEEN = "already_seen"
    OWN_TRANSMISSION = "own"
    RATE_LIMITED = "rate_limited"
    TOO_SOON = "too_soon"
    UNDECODABLE = "undecodable"
    DISABLED = "disabled"
    WRONG_ZONE = "wrong_zone"


@dataclass
class RepeaterStats:
    heard: int = 0
    repeated: int = 0
    commands_run: int = 0
    commands_refused: int = 0
    drops: Dict[str, int] = field(default_factory=dict)

    def drop(self, reason: DropReason) -> None:
        self.drops[reason.value] = self.drops.get(reason.value, 0) + 1

    def as_dict(self) -> dict:
        return {
            "heard": self.heard,
            "repeated": self.repeated,
            "commands_run": self.commands_run,
            "commands_refused": self.commands_refused,
            "drops": dict(self.drops),
        }


@dataclass
class HeardPacket:
    """A decoded packet the balloon received."""

    sender: int
    destination: int
    packet_id: int
    port: PortNum
    payload: bytes
    channel_hash: int
    rssi: int = 0
    snr: float = 0.0
    raw: bytes = b""


class SeenCache:
    """
    Remembers recently repeated packet ids.

    A mesh delivers the same packet by several paths, and without this the
    balloon would repeat each one every time it arrived. Bounded by both size
    and age so it cannot grow without limit across a long flight.
    """

    def __init__(self, capacity: int = 512, ttl_sec: float = 600.0):
        self.capacity = capacity
        self.ttl_sec = ttl_sec
        self._entries: "OrderedDict[int, float]" = OrderedDict()

    def seen(self, packet_id: int, now: float) -> bool:
        self._expire(now)
        return packet_id in self._entries

    def add(self, packet_id: int, now: float) -> None:
        self._entries[packet_id] = now
        self._entries.move_to_end(packet_id)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    def _expire(self, now: float) -> None:
        cutoff = now - self.ttl_sec
        while self._entries:
            oldest_id, stamp = next(iter(self._entries.items()))
            if stamp >= cutoff:
                break
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


class MeshtasticRepeater:
    """Decides what to repeat, and runs uplink commands."""

    def __init__(
        self,
        node_id: int,
        primary_channel,
        private_channel=None,
        tag: str = "!RPT ",
        hop_limit: int = 0,
        max_per_hour: int = 20,
        min_spacing_sec: float = 30.0,
        enabled: bool = True,
        commands_enabled: bool = True,
        command_handlers: Optional[Dict[str, Callable[[List[str]], str]]] = None,
    ):
        """
        Args:
            node_id: This balloon's Meshtastic node id.
            primary_channel, private_channel: ChannelConfig objects.
            tag: Prefix that marks a broadcast as "please repeat this".
            hop_limit: Hops on a rebroadcast. Keep at 0.
            max_per_hour: Hard ceiling on rebroadcasts, whatever is asked.
            min_spacing_sec: Minimum gap between rebroadcasts.
            enabled: Master switch for repeating.
            commands_enabled: Master switch for uplink commands.
            command_handlers: name -> handler(args) -> reply text.
        """
        self.node_id = node_id
        self.primary_channel = primary_channel
        self.private_channel = private_channel
        self.tag = tag
        self.hop_limit = hop_limit
        self.max_per_hour = max_per_hour
        self.min_spacing_sec = min_spacing_sec
        self.enabled = enabled
        self.commands_enabled = commands_enabled
        self.command_handlers = command_handlers or {}

        self.stats = RepeaterStats()
        self._seen = SeenCache()
        self._repeat_times: List[float] = []
        self._last_repeat: float = 0.0

    # -- receiving ---------------------------------------------------------

    def decode(self, raw: bytes, rssi: int = 0, snr: float = 0.0) -> Optional[HeardPacket]:
        """Decrypt and decode a received packet against our channel keys."""
        # Try each channel in turn rather than handing parse_packet a list:
        # knowing *which* channel a packet arrived on is what gates commands,
        # and a combined attempt would lose that.
        for channel in self._channels():
            packet = parse_packet(raw, channel_key=channel.key)
            if packet is None:
                continue
            try:
                data = parse_data(packet.payload)
            except Exception:
                continue
            try:
                port = PortNum(data.portnum)
            except ValueError:
                continue
            if port == PortNum.UNKNOWN_APP and not data.payload:
                continue

            return HeardPacket(
                sender=packet.header.sender,
                destination=packet.header.destination,
                packet_id=packet.header.packet_id,
                port=port,
                payload=data.payload,
                channel_hash=packet.header.channel_hash,
                rssi=rssi, snr=snr, raw=raw,
            )

        return None

    def _channels(self):
        yield self.primary_channel
        if self.private_channel and self.private_channel.enabled:
            yield self.private_channel

    def _channel_for_hash(self, channel_hash: int):
        for channel in self._channels():
            if channel.hash == channel_hash:
                return channel
        return self.primary_channel

    # -- the decision ------------------------------------------------------

    def should_repeat(
        self, packet: HeardPacket, now: Optional[float] = None, in_cruise: bool = True
    ) -> Tuple[bool, Optional[DropReason]]:
        """
        Decide whether to rebroadcast. Returns (repeat, reason_if_not).

        Checked cheapest first, so the common case -- untagged traffic from
        strangers -- costs almost nothing.
        """
        now = time.time() if now is None else now

        if not self.enabled:
            return False, DropReason.DISABLED

        # Repeating only makes sense once the balloon is high and downrange.
        # Near the launch site it adds nothing and competes with imagery.
        if not in_cruise:
            return False, DropReason.WRONG_ZONE

        if packet.sender == self.node_id:
            return False, DropReason.OWN_TRANSMISSION

        if not self._is_tagged(packet):
            return False, DropReason.NOT_TAGGED

        # Dedupe only after the tag test: a mesh redelivers everything, and
        # filling the cache with traffic we would never repeat anyway would
        # evict the entries that matter.
        if self._seen.seen(packet.packet_id, now):
            return False, DropReason.ALREADY_SEEN

        if now - self._last_repeat < self.min_spacing_sec:
            return False, DropReason.TOO_SOON

        self._prune_rate_window(now)
        if len(self._repeat_times) >= self.max_per_hour:
            return False, DropReason.RATE_LIMITED

        return True, None

    def _is_tagged(self, packet: HeardPacket) -> bool:
        """
        A packet is tagged if it is addressed to us, or its text starts with
        the configured prefix. Everything else is somebody else's traffic.

        One exception: a message addressed to us that looks like a command is
        a command attempt, not a request to relay. Repeating it would put
        somebody's command text on the air for the whole mesh to read --
        including one that was refused because it arrived on a public channel.
        """
        text = None
        if packet.port == PortNum.TEXT_MESSAGE_APP:
            try:
                text = parse_text_message(packet.payload)
            except Exception:
                text = None

        if packet.destination == self.node_id:
            return not (text is not None and text.lstrip().startswith("!"))

        if text is None:
            return False

        return bool(self.tag) and text.startswith(self.tag)

    def _prune_rate_window(self, now: float) -> None:
        cutoff = now - 3600.0
        self._repeat_times = [t for t in self._repeat_times if t >= cutoff]

    # -- repeating ---------------------------------------------------------

    def build_repeat(self, packet: HeardPacket, now: Optional[float] = None) -> bytes:
        """
        Build the rebroadcast.

        Sent as our own packet rather than a forward of theirs: the balloon is
        the one with the enormous footprint, and the receiving mesh should see
        who actually put it on the air. The tag is stripped so the relayed text
        reads normally.
        """
        now = time.time() if now is None else now

        text = ""
        if packet.port == PortNum.TEXT_MESSAGE_APP:
            try:
                text = parse_text_message(packet.payload)
            except Exception:
                text = ""
            if self.tag and text.startswith(self.tag):
                text = text[len(self.tag):]

        channel = self._channel_for_hash(packet.channel_hash)
        payload = build_text_message(text) if text else packet.payload
        port = PortNum.TEXT_MESSAGE_APP if text else packet.port

        built = build_packet(
            build_data(port, payload),
            sender=self.node_id,
            destination=BROADCAST_ADDR,
            channel_key=channel.key,
            channel_hash=channel.hash,
            hop_limit=self.hop_limit,
        )

        self._seen.add(packet.packet_id, now)
        self._repeat_times.append(now)
        self._last_repeat = now
        self.stats.repeated += 1

        logger.info(
            f"Repeating for {packet.sender:#010x}: {text[:40]!r} "
            f"({len(self._repeat_times)}/{self.max_per_hour} this hour)"
        )
        return built

    # -- uplink commands ---------------------------------------------------

    def handle_command(self, packet: HeardPacket) -> Optional[bytes]:
        """
        Run an uplink command, if this packet is one and is allowed to be.

        Requires all of: commands enabled, a private channel configured, the
        packet arriving on that channel, and the packet addressed to us
        specifically. A broadcast on the public channel can never command the
        balloon however it is worded -- anyone can transmit on that channel.
        """
        if not self.commands_enabled:
            return None
        if packet.port != PortNum.TEXT_MESSAGE_APP:
            return None

        if self.private_channel is None or not self.private_channel.enabled:
            return None
        if packet.channel_hash != self.private_channel.hash:
            return None
        if packet.destination != self.node_id:
            return None

        try:
            text = parse_text_message(packet.payload).strip()
        except Exception:
            return None

        if not text.startswith("!"):
            return None

        parts = text[1:].split()
        if not parts:
            return None

        name, args = parts[0].lower(), parts[1:]
        handler = self.command_handlers.get(name)

        if handler is None:
            self.stats.commands_refused += 1
            logger.warning(f"Refused unknown uplink command {name!r}")
            reply = f"unknown command: {name}"
        else:
            try:
                reply = handler(args)
                self.stats.commands_run += 1
                logger.info(f"Uplink command {name!r} from {packet.sender:#010x}: {reply}")
            except Exception as e:
                self.stats.commands_refused += 1
                logger.error(f"Uplink command {name!r} failed: {e}")
                reply = f"{name} failed: {e}"

        return build_packet(
            build_data(PortNum.TEXT_MESSAGE_APP, build_text_message(reply)),
            sender=self.node_id,
            destination=packet.sender,
            channel_key=self.private_channel.key,
            channel_hash=self.private_channel.hash,
            hop_limit=self.hop_limit,
        )

    # -- reporting ---------------------------------------------------------

    def note_heard(self, decoded: bool) -> None:
        self.stats.heard += 1
        if not decoded:
            self.stats.drop(DropReason.UNDECODABLE)

    def get_status(self) -> dict:
        status = self.stats.as_dict()
        status.update({
            "enabled": self.enabled,
            "commands_enabled": self.commands_enabled,
            "tag": self.tag,
            "hop_limit": self.hop_limit,
            "max_per_hour": self.max_per_hour,
            "repeats_this_hour": len(self._repeat_times),
            "seen_cache": len(self._seen),
            "commands": sorted(self.command_handlers),
        })
        return status
