"""
Meshtastic regional band plans and frequency derivation.

A balloon at altitude drifts across borders, and each jurisdiction assigns a
different ISM band to Meshtastic. Transmitting on the wrong one means either
nobody can hear you or you are outside what that country permits, so this
module owns three related jobs:

  1. The band table: frequency range, transmit power ceiling, and duty cycle
     for each Meshtastic region.
  2. Frequency derivation that matches Meshtastic firmware exactly, so the
     balloon lands on the channel local nodes are actually listening to.
  3. Coarse lat/lon -> region lookup, deliberately conservative: anywhere the
     answer is not clear resolves to "no region", and the caller must then
     stay off the air rather than guess.

Frequency derivation follows RadioInterface::applyModemConfig() in the
Meshtastic firmware:

    num_channels  = floor((freq_end - freq_start) / bandwidth_mhz)
    channel_index = djb2(channel_name) % num_channels
    frequency     = freq_start + bandwidth_mhz/2 + channel_index * bandwidth_mhz

Verified against published values: US 906.875, EU_868 869.525, EU_433 433.875,
ANZ 919.875 MHz for the default "LongFast" channel at 250 kHz.
"""

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Region:
    """A Meshtastic regional band plan."""

    code: str
    description: str
    freq_start_mhz: float
    freq_end_mhz: float
    power_limit_dbm: int
    duty_cycle_percent: float = 100.0

    @property
    def width_mhz(self) -> float:
        return self.freq_end_mhz - self.freq_start_mhz

    def num_channels(self, bandwidth_khz: int) -> int:
        """How many channels of the given bandwidth fit in this band."""
        bandwidth_mhz = bandwidth_khz / 1000.0
        return max(1, int(math.floor(self.width_mhz / bandwidth_mhz)))

    def channel_frequency(self, channel_index: int, bandwidth_khz: int) -> float:
        """Centre frequency of a channel index within this band, in MHz."""
        bandwidth_mhz = bandwidth_khz / 1000.0
        return (
            self.freq_start_mhz
            + bandwidth_mhz / 2.0
            + channel_index * bandwidth_mhz
        )

    def contains(self, frequency_mhz: float) -> bool:
        return self.freq_start_mhz <= frequency_mhz <= self.freq_end_mhz


# Power ceilings are the Meshtastic firmware's per-region limits. They are a
# floor on caution, not legal advice -- the operator remains responsible for
# licensing in whatever jurisdiction the balloon is over.
REGIONS: Tuple[Region, ...] = (
    Region("US", "United States", 902.0, 928.0, 30),
    Region("EU_433", "Europe 433 MHz", 433.0, 434.0, 12, duty_cycle_percent=10.0),
    Region("EU_868", "Europe 868 MHz", 869.4, 869.65, 14, duty_cycle_percent=10.0),
    Region("CN", "China", 470.0, 510.0, 19),
    Region("JP", "Japan", 920.8, 927.8, 13),
    Region("ANZ", "Australia / New Zealand", 915.0, 928.0, 30),
    Region("KR", "South Korea", 920.0, 923.0, 0),
    Region("TW", "Taiwan", 920.0, 925.0, 27),
    Region("RU", "Russia", 868.7, 869.2, 20),
    Region("IN", "India", 865.0, 867.0, 30),
    Region("NZ_865", "New Zealand 865 MHz", 864.0, 868.0, 36),
    Region("TH", "Thailand", 920.0, 925.0, 16),
    Region("UA_433", "Ukraine 433 MHz", 433.0, 434.7, 10),
    Region("UA_868", "Ukraine 868 MHz", 868.0, 868.6, 14),
    Region("MY_433", "Malaysia 433 MHz", 433.0, 435.0, 20),
    Region("MY_919", "Malaysia 919 MHz", 919.0, 924.0, 27),
    Region("SG_923", "Singapore", 917.0, 925.0, 20),
    Region("PH_433", "Philippines 433 MHz", 433.0, 434.7, 10),
    Region("PH_868", "Philippines 868 MHz", 868.0, 869.4, 12),
    Region("PH_915", "Philippines 915 MHz", 915.0, 918.0, 27),
    Region("BR_902", "Brazil", 902.0, 907.5, 30),
    Region("NP_865", "Nepal", 865.0, 868.0, 30),
    Region("LORA_24", "2.4 GHz worldwide", 2400.0, 2483.5, 10),
)

REGIONS_BY_CODE: Dict[str, Region] = {r.code: r for r in REGIONS}

# The SX1262 is a sub-GHz part. LORA_24 is in the table for completeness of the
# Meshtastic enumeration but cannot be transmitted by this hardware.
SUB_GHZ_REGION_CODES: Tuple[str, ...] = tuple(
    r.code for r in REGIONS if r.freq_end_mhz < 1000.0
)

DEFAULT_REGION_CODE = "US"
DEFAULT_CHANNEL_NAME = "LongFast"
DEFAULT_BANDWIDTH_KHZ = 250


def get_region(code: str) -> Optional[Region]:
    return REGIONS_BY_CODE.get(code.upper()) if code else None


# --------------------------------------------------------------------------
# Hardware band limits
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HardwareBand:
    """
    The frequency range the radio module can actually transmit on.

    The SX1262 die covers 150-960 MHz, but a board is not a die: the matching
    network, filters, and PA on a Waveshare HAT are tuned for one band. Driving
    a 900 MHz board at 433 MHz means a badly matched load -- almost no radiated
    power, and a real risk of damaging the amplifier.

    So the region logic must be constrained by what the *board* can do, not by
    what the chip datasheet allows. A region the hardware cannot reach is
    treated exactly like unknown territory: no transmission.
    """

    min_mhz: float
    max_mhz: float
    description: str = ""

    def __post_init__(self):
        if self.min_mhz <= 0 or self.max_mhz <= self.min_mhz:
            raise ValueError(
                f"invalid hardware band {self.min_mhz}-{self.max_mhz} MHz"
            )

    def contains(self, frequency_mhz: float) -> bool:
        return self.min_mhz <= frequency_mhz <= self.max_mhz

    def __str__(self) -> str:
        label = f"{self.min_mhz:g}-{self.max_mhz:g} MHz"
        return f"{label} ({self.description})" if self.description else label


# Waveshare Core1262 / SX1262 HAT variants. The product is sold as two
# front-end builds, not as per-country boards: HF covers the whole 850-930 MHz
# sub-GHz range, LF covers 410-510 MHz. This payload flies the HF board, which
# reaches every Meshtastic region except the 433 MHz ones and China.
HARDWARE_BANDS = {
    "HF": HardwareBand(850.0, 930.0, "Waveshare Core1262-HF"),
    "LF": HardwareBand(410.0, 510.0, "Waveshare Core1262-LF"),
}

DEFAULT_HARDWARE_BAND = HARDWARE_BANDS["HF"]


def resolve_hardware_band(
    code: str,
    custom_min_mhz: Optional[float] = None,
    custom_max_mhz: Optional[float] = None,
) -> Optional[HardwareBand]:
    """
    Look up a hardware band, or build a custom one.

    Args:
        code: "HF", "LF", or "CUSTOM".
        custom_min_mhz, custom_max_mhz: Required when code is "CUSTOM", for a
            board whose front end is not one of the stock variants.

    Returns:
        The band, or None if the code is unrecognised or a custom range is
        invalid.
    """
    code = (code or "").upper()

    if code == "CUSTOM":
        if custom_min_mhz is None or custom_max_mhz is None:
            return None
        try:
            return HardwareBand(
                float(custom_min_mhz), float(custom_max_mhz), "custom front end"
            )
        except ValueError:
            return None

    return HARDWARE_BANDS.get(code)


def regions_within_band(
    band: HardwareBand,
    channel_name: str = DEFAULT_CHANNEL_NAME,
    bandwidth_khz: int = DEFAULT_BANDWIDTH_KHZ,
) -> List[Region]:
    """
    Regions whose derived channel frequency the hardware can actually reach.

    Checks the frequency the balloon would really transmit on, not merely
    whether the region's band overlaps the hardware's -- a partial overlap
    could still put the specific channel out of reach.
    """
    supported = []
    for region in REGIONS:
        frequency = frequency_for_channel(region, channel_name, bandwidth_khz)
        if band.contains(frequency):
            supported.append(region)
    return supported


def region_is_supported(
    region: Optional[Region],
    band: HardwareBand,
    channel_name: str = DEFAULT_CHANNEL_NAME,
    bandwidth_khz: int = DEFAULT_BANDWIDTH_KHZ,
) -> bool:
    """Whether the hardware can transmit this region's channel frequency."""
    if region is None:
        return False
    frequency = frequency_for_channel(region, channel_name, bandwidth_khz)
    return band.contains(frequency)


# --------------------------------------------------------------------------
# Frequency derivation
# --------------------------------------------------------------------------


def djb2(text: str) -> int:
    """
    The djb2 string hash used by Meshtastic firmware to pick a channel index.

    Reproduced exactly, including the 32-bit wraparound, because a different
    result puts the balloon on a frequency nobody is listening to.
    """
    value = 5381
    for byte in text.encode("utf-8"):
        value = ((value << 5) + value + byte) & 0xFFFFFFFF
    return value


def channel_index_for_name(
    region: Region, channel_name: str, bandwidth_khz: int = DEFAULT_BANDWIDTH_KHZ
) -> int:
    """Which channel slot Meshtastic assigns to a named channel in a region."""
    return djb2(channel_name) % region.num_channels(bandwidth_khz)


def frequency_for_channel(
    region: Region,
    channel_name: str = DEFAULT_CHANNEL_NAME,
    bandwidth_khz: int = DEFAULT_BANDWIDTH_KHZ,
    channel_index: Optional[int] = None,
) -> float:
    """
    Centre frequency in MHz for a named Meshtastic channel in a region.

    Args:
        region: The regional band plan.
        channel_name: Primary channel name, e.g. "LongFast".
        bandwidth_khz: LoRa bandwidth in kHz.
        channel_index: Explicit slot override. Meshtastic's `lora.channel_num`
            is 1-based and this is 0-based; pass `channel_num - 1`.
    """
    if channel_index is None:
        channel_index = channel_index_for_name(region, channel_name, bandwidth_khz)

    total = region.num_channels(bandwidth_khz)
    if not 0 <= channel_index < total:
        raise ValueError(
            f"channel index {channel_index} out of range for {region.code} "
            f"at {bandwidth_khz} kHz (0..{total - 1})"
        )

    return region.channel_frequency(channel_index, bandwidth_khz)


def clamp_power_to_region(power_dbm: int, region: Region) -> int:
    """
    Clamp a requested transmit power to the region's ceiling.

    Called on every region change: moving the balloon's frequency without also
    moving its power ceiling would put it outside what the new jurisdiction
    permits.
    """
    if power_dbm > region.power_limit_dbm:
        logger.warning(
            f"TX power {power_dbm} dBm exceeds the {region.code} limit of "
            f"{region.power_limit_dbm} dBm; clamping"
        )
        return region.power_limit_dbm
    return power_dbm


# --------------------------------------------------------------------------
# Geographic lookup
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RegionBox:
    """
    A bounding box mapping a geographic area to a Meshtastic region.

    Deliberately coarse. A country-polygon database is far more than this task
    needs and more than the payload should carry: a balloon drifts a few
    hundred kilometres, and the failure mode we care about is "which national
    band plan applies", not "which side of a river". Boxes are checked in
    ascending `priority`, so a small enclave listed at priority 0 wins over the
    continental box that contains it.
    """

    region_code: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    priority: int = 10
    note: str = ""

    def contains(self, latitude: float, longitude: float) -> bool:
        if not self.lat_min <= latitude <= self.lat_max:
            return False
        if self.lon_min <= self.lon_max:
            return self.lon_min <= longitude <= self.lon_max
        # Box straddles the antimeridian.
        return longitude >= self.lon_min or longitude <= self.lon_max


# Ordered by priority; anything not covered resolves to None, which callers
# must treat as "do not transmit".
REGION_BOXES: Tuple[RegionBox, ...] = (
    # --- Enclaves and exceptions, checked first ---
    RegionBox("EU_868", 35.0, 44.0, -10.0, 4.0, priority=0, note="Iberia"),
    RegionBox("UA_868", 44.0, 52.5, 22.0, 40.5, priority=0, note="Ukraine"),
    RegionBox("NZ_865", -48.0, -34.0, 166.0, 179.0, priority=0, note="New Zealand"),
    RegionBox("KR", 33.0, 39.0, 124.5, 131.0, priority=0, note="Korean peninsula"),
    RegionBox("JP", 24.0, 46.0, 122.0, 146.0, priority=1, note="Japan"),
    RegionBox("TW", 21.5, 25.5, 119.0, 122.5, priority=0, note="Taiwan"),
    RegionBox("PH_915", 4.5, 21.5, 116.0, 127.0, priority=0, note="Philippines"),
    RegionBox("SG_923", 0.8, 1.7, 103.4, 104.2, priority=0, note="Singapore"),
    RegionBox("MY_919", 0.5, 7.5, 99.5, 119.5, priority=1, note="Malaysia"),
    RegionBox("TH", 5.5, 20.5, 97.0, 106.0, priority=1, note="Thailand"),
    RegionBox("NP_865", 26.0, 30.5, 80.0, 88.5, priority=0, note="Nepal"),
    RegionBox("IN", 6.0, 36.0, 68.0, 97.5, priority=2, note="India"),
    RegionBox("BR_902", -34.0, 5.5, -74.0, -34.0, priority=1, note="Brazil"),

    # --- Continental blocks ---
    RegionBox("US", 24.0, 50.0, -125.0, -66.0, priority=5, note="CONUS"),
    RegionBox("US", 51.0, 72.0, -170.0, -129.0, priority=5, note="Alaska"),
    RegionBox("US", 18.5, 22.5, -161.0, -154.0, priority=5, note="Hawaii"),
    RegionBox("US", 41.0, 70.0, -142.0, -52.0, priority=6, note="Canada shares US band"),
    RegionBox("US", 14.0, 33.0, -118.0, -86.0, priority=6, note="Mexico"),
    RegionBox("CN", 18.0, 54.0, 73.0, 135.0, priority=5, note="China"),
    RegionBox("RU", 41.0, 78.0, 27.0, 180.0, priority=6, note="Russia"),
    RegionBox("ANZ", -44.0, -10.0, 112.0, 154.0, priority=5, note="Australia"),
    RegionBox("EU_868", 34.0, 72.0, -11.0, 32.0, priority=7, note="Europe"),
)


def region_for_position(latitude: float, longitude: float) -> Optional[Region]:
    """
    Determine the Meshtastic region for a geographic position.

    Returns None when the position is not inside any known box -- over ocean,
    over a country not in the table, or with nonsense coordinates. Callers must
    treat None as "stay off the air", never as "use the default".
    """
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        logger.warning(
            f"Position ({latitude}, {longitude}) is not a valid coordinate"
        )
        return None

    matches: List[RegionBox] = [
        box for box in REGION_BOXES if box.contains(latitude, longitude)
    ]
    if not matches:
        return None

    best = min(matches, key=lambda b: b.priority)
    return REGIONS_BY_CODE.get(best.region_code)


def distance_to_region_edge_km(latitude: float, longitude: float) -> Optional[float]:
    """
    Rough distance from a position to the nearest edge of its own region box.

    Used for boundary hysteresis: a balloon tracking along a national border
    should not oscillate between bands every time it wobbles across the line.
    Returns None if the position is not inside any box.
    """
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None

    matches = [box for box in REGION_BOXES if box.contains(latitude, longitude)]
    if not matches:
        return None

    box = min(matches, key=lambda b: b.priority)

    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * max(0.01, math.cos(math.radians(latitude)))

    return min(
        (latitude - box.lat_min) * km_per_deg_lat,
        (box.lat_max - latitude) * km_per_deg_lat,
        abs(longitude - box.lon_min) * km_per_deg_lon,
        abs(box.lon_max - longitude) * km_per_deg_lon,
    )
