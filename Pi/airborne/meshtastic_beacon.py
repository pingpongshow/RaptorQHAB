"""
Builds and transmits the balloon's Meshtastic beacons.

Each beacon cycle emits, on the primary (broadcast) channel:

    - Position: where the balloon is
    - Telemetry: battery, uptime, and CPU temperature as an environment metric
    - NodeInfo: identity, sent less often since it rarely changes
    - An optional operator text message

and, when a private channel is configured, the same position and text on that
channel too.

Every broadcast goes out with hop_limit = 0. From 30 km up the balloon's
footprint is on the order of a 400-mile radius, covering a very large number of
nodes; if each of those rebroadcast, one balloon could congest whole regional
meshes. hop_limit = 0 means nodes hear it directly and nothing forwards it.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from common.meshtastic import (
    BROADCAST_ADDR,
    PortNum,
    build_data,
    build_device_metrics,
    build_packet,
    build_position,
    build_telemetry,
    build_text_message,
    build_user,
    channel_hash,
    expand_psk,
    node_id_from_callsign,
    node_id_to_string,
    short_name_from_callsign,
)
from common.meshtastic.messages import HardwareModel, build_environment_metrics

logger = logging.getLogger(__name__)


@dataclass
class ChannelConfig:
    """A Meshtastic channel the balloon transmits on."""

    name: str
    psk: bytes = b""
    enabled: bool = True

    def __post_init__(self):
        self._key = expand_psk(self.psk)
        self._hash = channel_hash(self.name, self._key)

    @property
    def key(self) -> bytes:
        return self._key

    @property
    def hash(self) -> int:
        return self._hash

    @property
    def is_encrypted(self) -> bool:
        return bool(self._key)


@dataclass
class BeaconTelemetry:
    """The payload state a beacon reports."""

    latitude: float = 0.0
    longitude: float = 0.0
    altitude_m: float = 0.0
    satellites: int = 0
    fix_type: int = 0
    ground_speed_mps: float = 0.0
    ground_track_deg: float = 0.0
    battery_mv: int = 0
    battery_percent: int = 0
    cpu_temp_c: float = 0.0
    uptime_sec: int = 0

    @property
    def has_position(self) -> bool:
        return self.fix_type >= 2 and (self.latitude != 0.0 or self.longitude != 0.0)


@dataclass
class BeaconStats:
    packets_built: int = 0
    packets_sent: int = 0
    packets_failed: int = 0
    suppressed_no_region: int = 0
    bytes_sent: int = 0
    last_beacon_time: float = 0.0


class MeshtasticBeacon:
    """Assembles and transmits the balloon's Meshtastic beacons."""

    def __init__(
        self,
        callsign: str,
        payload_id: int = 0,
        long_name: Optional[str] = None,
        primary_channel: Optional[ChannelConfig] = None,
        private_channel: Optional[ChannelConfig] = None,
        beacon_text: str = "",
        hop_limit: int = 0,
        nodeinfo_every: int = 6,
    ):
        """
        Args:
            callsign: Payload callsign; the node id is derived from it.
            payload_id: Distinguishes multiple payloads sharing a callsign.
            long_name: Display name, defaults to the callsign.
            primary_channel: Public broadcast channel, defaults to LongFast.
            private_channel: Optional second channel with its own key.
            beacon_text: Operator message included in each cycle.
            hop_limit: Hops for broadcasts. Keep at 0 unless you have a
                specific reason not to.
            nodeinfo_every: Send NodeInfo once per this many beacon cycles.
        """
        self.callsign = callsign
        self.node_id = node_id_from_callsign(callsign, payload_id)
        self.long_name = long_name or f"RaptorHAB {callsign}"
        self.short_name = short_name_from_callsign(callsign)

        self.primary_channel = primary_channel or ChannelConfig(name="LongFast", psk=b"\x01")
        self.private_channel = private_channel
        self.beacon_text = beacon_text
        self.hop_limit = hop_limit
        self.nodeinfo_every = max(1, nodeinfo_every)

        self._cycle = 0
        self.stats = BeaconStats()

        logger.info(
            f"Meshtastic identity: {node_id_to_string(self.node_id)} "
            f"\"{self.long_name}\" [{self.short_name}] on channel "
            f"\"{self.primary_channel.name}\" "
            f"(hash 0x{self.primary_channel.hash:02X}, "
            f"{'encrypted' if self.primary_channel.is_encrypted else 'plaintext'})"
        )
        if self.private_channel:
            logger.info(
                f"Private channel \"{self.private_channel.name}\" "
                f"(hash 0x{self.private_channel.hash:02X}, "
                f"{'encrypted' if self.private_channel.is_encrypted else 'PLAINTEXT'})"
            )

    # -- packet construction ----------------------------------------------

    def _wrap(
        self,
        portnum: PortNum,
        payload: bytes,
        channel: ChannelConfig,
        destination: int = BROADCAST_ADDR,
    ) -> bytes:
        return build_packet(
            build_data(portnum, payload),
            sender=self.node_id,
            destination=destination,
            channel_key=channel.key,
            channel_hash=channel.hash,
            hop_limit=self.hop_limit,
        )

    def build_position_packet(
        self, telemetry: BeaconTelemetry, channel: ChannelConfig
    ) -> bytes:
        payload = build_position(
            latitude=telemetry.latitude,
            longitude=telemetry.longitude,
            altitude_m=telemetry.altitude_m,
            timestamp=int(time.time()),
            satellites=telemetry.satellites,
            ground_speed_mps=telemetry.ground_speed_mps,
            ground_track_deg=telemetry.ground_track_deg,
        )
        return self._wrap(PortNum.POSITION_APP, payload, channel)

    def build_telemetry_packet(
        self, telemetry: BeaconTelemetry, channel: ChannelConfig
    ) -> bytes:
        payload = build_telemetry(
            device_metrics=build_device_metrics(
                battery_level=telemetry.battery_percent,
                voltage=telemetry.battery_mv / 1000.0,
                uptime_seconds=telemetry.uptime_sec,
            )
        )
        return self._wrap(PortNum.TELEMETRY_APP, payload, channel)

    def build_environment_packet(
        self, telemetry: BeaconTelemetry, channel: ChannelConfig
    ) -> bytes:
        """
        Environment metrics carrying the payload's own temperature.

        A stock Meshtastic client graphs this, which turns out to be a genuinely
        useful readout of how cold the electronics got at altitude.
        """
        payload = build_telemetry(
            environment_metrics=build_environment_metrics(
                temperature_c=telemetry.cpu_temp_c
            )
        )
        return self._wrap(PortNum.TELEMETRY_APP, payload, channel)

    def build_nodeinfo_packet(self, channel: ChannelConfig) -> bytes:
        payload = build_user(
            node_id=node_id_to_string(self.node_id),
            long_name=self.long_name,
            short_name=self.short_name,
            hw_model=HardwareModel.PRIVATE_HW,
        )
        return self._wrap(PortNum.NODEINFO_APP, payload, channel)

    def build_text_packet(
        self, text: str, channel: ChannelConfig, destination: int = BROADCAST_ADDR
    ) -> bytes:
        return self._wrap(
            PortNum.TEXT_MESSAGE_APP,
            build_text_message(text),
            channel,
            destination=destination,
        )

    def build_beacon_cycle(self, telemetry: BeaconTelemetry) -> List[bytes]:
        """
        Build every packet for one beacon cycle, in transmit order.

        Position goes first: it is the packet that matters most, and if the
        cycle is cut short by a mode switch or a schedule boundary, it is the
        one that should already be on the air.
        """
        packets: List[bytes] = []
        channel = self.primary_channel

        if telemetry.has_position:
            packets.append(self.build_position_packet(telemetry, channel))
        else:
            logger.debug("No GPS fix; beacon cycle omits position")

        packets.append(self.build_telemetry_packet(telemetry, channel))

        if telemetry.cpu_temp_c:
            packets.append(self.build_environment_packet(telemetry, channel))

        if self._cycle % self.nodeinfo_every == 0:
            packets.append(self.build_nodeinfo_packet(channel))

        if self.beacon_text:
            packets.append(self.build_text_packet(self.beacon_text, channel))

        if self.private_channel and self.private_channel.enabled:
            packets.extend(self._build_private_packets(telemetry))

        self._cycle += 1
        self.stats.packets_built += len(packets)
        return packets

    def _build_private_packets(self, telemetry: BeaconTelemetry) -> List[bytes]:
        """Position and text on the private channel."""
        channel = self.private_channel
        packets: List[bytes] = []

        if telemetry.has_position:
            packets.append(self.build_position_packet(telemetry, channel))
        if self.beacon_text:
            packets.append(self.build_text_packet(self.beacon_text, channel))

        return packets

    # -- transmission ------------------------------------------------------

    def transmit_cycle(
        self,
        radio_manager,
        telemetry: BeaconTelemetry,
        region_manager=None,
        inter_packet_delay_sec: float = 0.5,
    ) -> int:
        """
        Build and transmit one beacon cycle.

        Args:
            radio_manager: A RadioModeManager.
            telemetry: Current payload state.
            region_manager: Optional RegionManager. When it says the balloon is
                over unknown territory, nothing is transmitted.
            inter_packet_delay_sec: Gap between packets, so the balloon does
                not monopolise the channel with a back-to-back burst.

        Returns:
            Number of packets successfully transmitted.
        """
        if region_manager is not None and not region_manager.may_transmit:
            self.stats.suppressed_no_region += 1
            logger.debug(
                "Beacon suppressed: no valid Meshtastic region for this position"
            )
            return 0

        packets = self.build_beacon_cycle(telemetry)
        sent = 0

        for index, packet in enumerate(packets):
            if radio_manager.transmit_lora(packet):
                sent += 1
                self.stats.packets_sent += 1
                self.stats.bytes_sent += len(packet)
            else:
                self.stats.packets_failed += 1
                logger.warning(f"Beacon packet {index + 1}/{len(packets)} failed")

            if index < len(packets) - 1 and inter_packet_delay_sec > 0:
                time.sleep(inter_packet_delay_sec)

        self.stats.last_beacon_time = time.time()
        logger.info(
            f"Beacon cycle: {sent}/{len(packets)} packets, "
            f"{self.stats.bytes_sent} bytes total this flight"
        )
        return sent

    def get_status(self) -> dict:
        return {
            "node_id": node_id_to_string(self.node_id),
            "long_name": self.long_name,
            "short_name": self.short_name,
            "primary_channel": self.primary_channel.name,
            "primary_encrypted": self.primary_channel.is_encrypted,
            "private_channel": (
                self.private_channel.name if self.private_channel else None
            ),
            "private_encrypted": (
                self.private_channel.is_encrypted if self.private_channel else None
            ),
            "hop_limit": self.hop_limit,
            "cycles": self._cycle,
            "packets_sent": self.stats.packets_sent,
            "packets_failed": self.stats.packets_failed,
            "suppressed_no_region": self.stats.suppressed_no_region,
            "bytes_sent": self.stats.bytes_sent,
        }
