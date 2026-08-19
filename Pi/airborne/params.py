"""
RaptorHab Airborne - Parameter Schema

Single source of truth describing every configurable payload parameter:
its type, valid range, which UI category it belongs to, whether it can be
applied in flight or requires a restart, and which environment variable
overrides it.

The macOS companion app builds its configuration form from ``get_schema()``,
so adding a parameter here is sufficient to expose it in the UI -- no Swift
changes required.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


class Apply(str, Enum):
    """When a parameter change takes effect."""

    LIVE = "live"        # applied immediately by the running payload
    RESTART = "restart"  # stored, but only takes effect on service restart


class Kind(str, Enum):
    """Value type, used by the UI to pick a control."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STRING = "string"
    ENUM = "enum"
    RESOLUTION = "resolution"  # [width, height]


@dataclass(frozen=True)
class ParamSpec:
    """Description of one configurable parameter."""

    name: str
    kind: Kind
    category: str
    description: str
    apply: Apply = Apply.RESTART
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Optional[Sequence[Any]] = None
    choice_labels: Optional[Sequence[str]] = None
    unit: str = ""
    env: Optional[str] = None
    advanced: bool = False
    secret: bool = False

    def coerce(self, value: Any) -> Any:
        """
        Convert an arbitrary input (JSON value, env string) to this
        parameter's native type.

        Raises:
            ValueError: if the value cannot be interpreted as this type.
        """
        if self.kind is Kind.BOOL:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            text = str(value).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off"):
                return False
            raise ValueError(f"{self.name}: cannot interpret {value!r} as a boolean")

        if self.kind is Kind.ENUM:
            # An enum's value type follows its choices. Several parameters are
            # string enums (region codes, modem presets, board variants), and
            # coercing those to int rejects every valid value.
            if self.choices and all(isinstance(c, str) for c in self.choices):
                return str(value)
            if isinstance(value, bool):
                raise ValueError(f"{self.name}: expected a number, got a boolean")
            return int(value)

        if self.kind is Kind.INT:
            if isinstance(value, bool):
                raise ValueError(f"{self.name}: expected a number, got a boolean")
            return int(value)

        if self.kind is Kind.FLOAT:
            if isinstance(value, bool):
                raise ValueError(f"{self.name}: expected a number, got a boolean")
            return float(value)

        if self.kind is Kind.STRING:
            return str(value)

        if self.kind is Kind.RESOLUTION:
            if isinstance(value, str):
                parts = value.replace("x", ",").split(",")
            else:
                parts = list(value)
            if len(parts) != 2:
                raise ValueError(f"{self.name}: expected width,height; got {value!r}")
            return (int(parts[0]), int(parts[1]))

        raise ValueError(f"{self.name}: unhandled kind {self.kind}")

    def validate(self, value: Any) -> Any:
        """
        Coerce and range-check a value.

        Raises:
            ValueError: if the value is out of range or not an allowed choice.
        """
        value = self.coerce(value)

        if self.kind is Kind.RESOLUTION:
            width, height = value
            if width <= 0 or height <= 0:
                raise ValueError(f"{self.name}: resolution must be positive")
            if width % 2 or height % 2:
                raise ValueError(f"{self.name}: resolution must be even in both axes")
            return value

        if self.choices is not None and value not in self.choices:
            raise ValueError(
                f"{self.name}: {value!r} is not one of {list(self.choices)}"
            )

        if self.minimum is not None and value < self.minimum:
            raise ValueError(
                f"{self.name}: {value} is below the minimum of {self.minimum}"
            )

        if self.maximum is not None and value > self.maximum:
            raise ValueError(
                f"{self.name}: {value} is above the maximum of {self.maximum}"
            )

        return value

    def to_json(self) -> Dict[str, Any]:
        """Serialize this spec for the configuration UI."""
        out: Dict[str, Any] = {
            "name": self.name,
            "kind": self.kind.value,
            "category": self.category,
            "description": self.description,
            "apply": self.apply.value,
            "advanced": self.advanced,
            "secret": self.secret,
        }
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.choices is not None:
            out["choices"] = list(self.choices)
        if self.choice_labels is not None:
            out["choice_labels"] = list(self.choice_labels)
        if self.unit:
            out["unit"] = self.unit
        if self.env:
            out["env"] = self.env
        return out


_AWB_LABELS = ("Auto", "Daylight", "Cloudy", "Tungsten", "Fluorescent", "Indoor")

# Ordered so the UI renders categories in a sensible sequence.
CATEGORY_ORDER: Tuple[str, ...] = (
    "Identification",
    "Radio",
    "Timing",
    "Camera",
    "Image Quality",
    "GPS",
    "Fountain Coding",
    "Recording Encryption",
    "Flight Zones",
    "Zone Launch",
    "Zone Cruise",
    "Zone Landed",
    "Meshtastic",
    "Meshtastic Region",
    "Meshtastic Private",
    "Meshtastic Repeater",
    "Storage",
    "Reliability",
    "Debug",
    "Hardware Pins",
)


def _spec(*args, **kwargs) -> ParamSpec:
    return ParamSpec(*args, **kwargs)


PARAM_SPECS: Tuple[ParamSpec, ...] = (
    # === Identification ===
    _spec(
        "callsign", Kind.STRING, "Identification",
        "Payload callsign included in telemetry and image overlays.",
        apply=Apply.LIVE, env="RAPTORHAB_CALLSIGN",
    ),
    _spec(
        "payload_id", Kind.INT, "Identification",
        "Numeric payload identifier, for flying more than one payload.",
        apply=Apply.LIVE, minimum=0, maximum=255,
    ),

    # === Radio ===
    _spec(
        "radio_frequency_mhz", Kind.FLOAT, "Radio",
        "GFSK image/telemetry downlink centre frequency. Must match the ground "
        "modem, and must fall inside the radio board's hardware band -- the "
        "bounds here are the SX1262 chip's full range, while the real limit is "
        "the fitted board's, enforced against radio_hardware_band.",
        apply=Apply.LIVE, minimum=150.0, maximum=960.0, unit="MHz",
        env="RAPTORHAB_FREQUENCY",
    ),
    _spec(
        "radio_power_dbm", Kind.INT, "Radio",
        "Transmit power. 22 dBm is the SX1262 maximum.",
        apply=Apply.LIVE, minimum=-9, maximum=22, unit="dBm",
        env="RAPTORHAB_TX_POWER",
    ),
    _spec(
        "radio_bitrate_bps", Kind.INT, "Radio",
        "GFSK bitrate. Higher is faster but less sensitive.",
        apply=Apply.RESTART, minimum=600, maximum=300000, unit="bps",
    ),
    _spec(
        "radio_fdev_hz", Kind.INT, "Radio",
        "GFSK frequency deviation. Should be roughly half the bitrate.",
        apply=Apply.RESTART, minimum=600, maximum=200000, unit="Hz",
    ),

    # === Timing ===
    _spec(
        "tx_period_sec", Kind.INT, "Timing",
        "Duration of each transmit burst.",
        apply=Apply.LIVE, minimum=1, maximum=300, unit="s",
        env="RAPTORHAB_TX_PERIOD",
    ),
    _spec(
        "tx_pause_sec", Kind.INT, "Timing",
        "Idle time between transmit bursts. 0 means transmit continuously.",
        apply=Apply.LIVE, minimum=0, maximum=300, unit="s",
        env="RAPTORHAB_TX_PAUSE",
    ),
    _spec(
        "telemetry_interval_packets", Kind.INT, "Timing",
        "Send a telemetry packet every N packets.",
        apply=Apply.LIVE, minimum=1, maximum=1000,
    ),
    _spec(
        "image_meta_interval_packets", Kind.INT, "Timing",
        "Repeat the image metadata packet every N packets so a receiver "
        "joining mid-image can still decode it. Must not be a multiple of "
        "the telemetry interval.",
        apply=Apply.LIVE, minimum=2, maximum=1000,
    ),
    _spec(
        "auto_capture_interval_sec", Kind.INT, "Timing",
        "Seconds between automatic image captures.",
        apply=Apply.LIVE, minimum=5, maximum=3600, unit="s",
        env="RAPTORHAB_CAPTURE_INTERVAL",
    ),

    # === Camera ===
    _spec(
        "camera_resolution", Kind.RESOLUTION, "Camera",
        "Capture resolution as width,height.",
        apply=Apply.RESTART,
    ),
    _spec(
        "camera_burst_count", Kind.INT, "Camera",
        "Frames captured per burst; the sharpest is kept.",
        apply=Apply.LIVE, minimum=1, maximum=20,
    ),
    _spec(
        "webp_quality", Kind.INT, "Camera",
        "WebP compression quality. Lower means smaller and faster to downlink.",
        apply=Apply.LIVE, minimum=1, maximum=100,
        env="RAPTORHAB_WEBP_QUALITY",
    ),
    _spec(
        "image_overlay_enabled", Kind.BOOL, "Camera",
        "Burn callsign, time, and position into the captured image.",
        apply=Apply.LIVE,
    ),

    # === Image Quality ===
    _spec(
        "camera_brightness", Kind.INT, "Image Quality",
        "0 = dark, 100 = normal, 200 = bright.",
        apply=Apply.LIVE, minimum=0, maximum=200,
        env="RAPTORHAB_CAMERA_BRIGHTNESS",
    ),
    _spec(
        "camera_contrast", Kind.INT, "Image Quality",
        "0 = flat, 100 = normal, 200 = high contrast.",
        apply=Apply.LIVE, minimum=0, maximum=200,
        env="RAPTORHAB_CAMERA_CONTRAST",
    ),
    _spec(
        "camera_saturation", Kind.INT, "Image Quality",
        "0 = greyscale, 100 = normal, 200 = vivid.",
        apply=Apply.LIVE, minimum=0, maximum=200,
        env="RAPTORHAB_CAMERA_SATURATION",
    ),
    _spec(
        "camera_sharpness", Kind.INT, "Image Quality",
        "0 = soft, 100 = normal, 200 = sharp.",
        apply=Apply.LIVE, minimum=0, maximum=200,
        env="RAPTORHAB_CAMERA_SHARPNESS",
    ),
    _spec(
        "camera_exposure_comp", Kind.INT, "Image Quality",
        "Exposure compensation: 0 = -2EV, 100 = 0EV, 200 = +2EV.",
        apply=Apply.LIVE, minimum=0, maximum=200,
        env="RAPTORHAB_CAMERA_EXPOSURE",
    ),
    _spec(
        "camera_awb_mode", Kind.ENUM, "Image Quality",
        "Auto white balance mode.",
        apply=Apply.LIVE, choices=(0, 1, 2, 3, 4, 5), choice_labels=_AWB_LABELS,
        env="RAPTORHAB_CAMERA_AWB",
    ),
    _spec(
        "camera_red_gain", Kind.INT, "Image Quality",
        "Red channel gain; lower values reduce a red/pink tint.",
        apply=Apply.LIVE, minimum=50, maximum=200,
        env="RAPTORHAB_CAMERA_RED_GAIN",
    ),
    _spec(
        "camera_blue_gain", Kind.INT, "Image Quality",
        "Blue channel gain; higher values counteract a red/pink tint.",
        apply=Apply.LIVE, minimum=50, maximum=200,
        env="RAPTORHAB_CAMERA_BLUE_GAIN",
    ),

    # === GPS ===
    _spec(
        "gps_device", Kind.STRING, "GPS",
        "Serial device for the GPS receiver.",
        apply=Apply.RESTART, env="RAPTORHAB_GPS_DEVICE",
    ),
    _spec(
        "gps_device_alt", Kind.STRING, "GPS",
        "Fallback serial device if the primary is unavailable.",
        apply=Apply.RESTART, advanced=True,
    ),
    _spec(
        "gps_baudrate", Kind.ENUM, "GPS",
        "GPS serial baud rate.",
        apply=Apply.RESTART, choices=(4800, 9600, 19200, 38400, 57600, 115200),
    ),
    _spec(
        "gps_airborne_mode", Kind.BOOL, "GPS",
        "Request the airborne dynamic model. Required above 18 km on uBlox "
        "receivers; the L76K ignores it.",
        apply=Apply.RESTART,
    ),

    # === Fountain Coding ===
    _spec(
        "fountain_symbol_size", Kind.INT, "Fountain Coding",
        "Bytes per fountain symbol. Must match the ground station.",
        apply=Apply.RESTART, minimum=32, maximum=237, unit="bytes",
    ),
    _spec(
        "fountain_overhead_percent", Kind.INT, "Fountain Coding",
        "Extra symbols transmitted beyond the minimum needed to decode.",
        apply=Apply.LIVE, minimum=0, maximum=300, unit="%",
    ),

    # === Recording Encryption ===
    _spec(
        "recording_encryption_enabled", Kind.BOOL, "Recording Encryption",
        "Seal captured images and telemetry logs to a public key as they are "
        "written. The payload holds only the public half and cannot read the "
        "files back, so a stranger who recovers the balloon gets ciphertext. "
        "The SD card itself cannot usefully be encrypted -- the payload must "
        "boot unattended, so any key it could use would travel with it.",
        apply=Apply.RESTART,
    ),
    _spec(
        "recording_public_key", Kind.STRING, "Recording Encryption",
        "X25519 public key, base64 or 64-char hex. Generate a keypair with "
        "tools/recording_key.py on the ground station and put only the public "
        "half here. Keep the private half off the balloon -- it is the one "
        "thing that must not fly.",
        apply=Apply.RESTART,
    ),

    # === Storage ===
    _spec(
        "image_storage_path", Kind.STRING, "Storage",
        "Directory for captured images.",
        apply=Apply.RESTART, env="RAPTORHAB_IMAGE_PATH",
    ),
    _spec(
        "log_path", Kind.STRING, "Storage",
        "Directory for log files.",
        apply=Apply.RESTART, env="RAPTORHAB_LOG_PATH",
    ),
    _spec(
        "max_stored_images", Kind.INT, "Storage",
        "Oldest images are deleted beyond this count.",
        apply=Apply.LIVE, minimum=10, maximum=100000,
    ),

    # === Reliability ===
    _spec(
        "watchdog_enabled", Kind.BOOL, "Reliability",
        "Monitor the main loop and recover if it stops making progress.",
        apply=Apply.RESTART,
    ),
    _spec(
        "watchdog_timeout_sec", Kind.INT, "Reliability",
        "Declare the main loop hung after this long without progress.",
        apply=Apply.RESTART, minimum=10, maximum=600, unit="s",
    ),
    _spec(
        "reboot_on_fatal_error", Kind.BOOL, "Reliability",
        "Restart the payload service after an unrecoverable error.",
        apply=Apply.LIVE,
    ),
    _spec(
        "max_consecutive_errors", Kind.INT, "Reliability",
        "Consecutive main-loop errors tolerated before entering recovery. "
        "The counter resets after a clean cycle.",
        apply=Apply.LIVE, minimum=1, maximum=1000,
    ),

    # === Meshtastic ===
    _spec(
        "meshtastic_enabled", Kind.BOOL, "Meshtastic",
        "Transmit Meshtastic beacons in addition to the RAPTOR image downlink.",
        apply=Apply.RESTART,
    ),
    _spec(
        "meshtastic_modem_preset", Kind.ENUM, "Meshtastic",
        "LoRa modem preset. Must match what local nodes use, or nobody hears "
        "the balloon. LONG_FAST is the Meshtastic default.",
        apply=Apply.LIVE,
        choices=(
            "LONG_FAST", "LONG_SLOW", "MEDIUM_SLOW", "MEDIUM_FAST",
            "SHORT_SLOW", "SHORT_FAST", "SHORT_TURBO", "VERY_LONG_SLOW",
        ),
    ),
    _spec(
        "meshtastic_channel_name", Kind.STRING, "Meshtastic",
        "Primary broadcast channel name. This determines the frequency slot "
        "within the region's band, so it must match local nodes exactly.",
        apply=Apply.LIVE,
    ),
    _spec(
        "meshtastic_channel_psk", Kind.STRING, "Meshtastic",
        "Primary channel key, base64 or hex. \"AQ==\" is the well-known default "
        "that every Meshtastic client ships with -- traffic using it is "
        "readable by anyone.",
        apply=Apply.LIVE, secret=True,
    ),
    _spec(
        "meshtastic_tx_power_dbm", Kind.INT, "Meshtastic",
        "Requested Meshtastic transmit power. Automatically clamped down to "
        "the active region's legal ceiling.",
        apply=Apply.LIVE, minimum=-9, maximum=30, unit="dBm",
    ),
    _spec(
        "meshtastic_hop_limit", Kind.INT, "Meshtastic",
        "Hops for the balloon's broadcasts. Keep at 0: from altitude the "
        "balloon reaches an enormous number of nodes, and letting them all "
        "rebroadcast can congest entire regional meshes.",
        apply=Apply.LIVE, minimum=0, maximum=7,
    ),
    _spec(
        "meshtastic_beacon_interval_sec", Kind.INT, "Meshtastic",
        "Seconds between beacon cycles. The zone scheduler may lengthen this, "
        "never shorten it.",
        apply=Apply.LIVE, minimum=30, maximum=3600, unit="s",
    ),
    _spec(
        "meshtastic_project_url", Kind.STRING, "Meshtastic",
        "Project link broadcast periodically on the public channel, at the same "
        "cadence as node info. Kept separate from the beacon text so that "
        "changing the operator message over the uplink cannot remove it -- "
        "somebody who hears this balloon and wants to know what it is should "
        "always be able to find out. Leave empty to send none.",
        apply=Apply.LIVE,
    ),
    _spec(
        "meshtastic_beacon_text", Kind.STRING, "Meshtastic",
        "Operator message broadcast with each beacon cycle. Leave empty to "
        "send none.",
        apply=Apply.LIVE,
    ),
    _spec(
        "meshtastic_inter_packet_delay_ms", Kind.INT, "Meshtastic",
        "Gap between the packets of one beacon cycle. A back-to-back burst "
        "from a balloon heard across several hundred miles monopolises the "
        "channel for everyone below it.",
        apply=Apply.LIVE, minimum=0, maximum=10000, unit="ms",
    ),
    _spec(
        "meshtastic_nodeinfo_every", Kind.INT, "Meshtastic",
        "Send the node identity once per this many beacon cycles.",
        apply=Apply.LIVE, minimum=1, maximum=100,
    ),
    _spec(
        "meshtastic_long_name", Kind.STRING, "Meshtastic",
        "Display name shown on receiving nodes. Empty derives it from the "
        "callsign.",
        apply=Apply.LIVE,
    ),

    # === Meshtastic Private Channel ===
    _spec(
        "meshtastic_private_enabled", Kind.BOOL, "Meshtastic Private",
        "Also transmit position and text on a second, private channel.",
        apply=Apply.LIVE,
    ),
    _spec(
        "meshtastic_private_name", Kind.STRING, "Meshtastic Private",
        "Private channel name.",
        apply=Apply.LIVE,
    ),
    _spec(
        "meshtastic_private_psk", Kind.STRING, "Meshtastic Private",
        "Private channel key, base64 or hex, 16/24/32 bytes. Generate a fresh "
        "one rather than reusing a default -- a default key means the channel "
        "is not private at all. Never transmitted over the radio and never "
        "read back by the configuration interface.",
        apply=Apply.LIVE, secret=True,
    ),

    # === Meshtastic Repeater ===
    _spec(
        "repeater_enabled", Kind.BOOL, "Meshtastic Repeater",
        "Rebroadcast messages explicitly tagged for the balloon while it is in "
        "cruise. Never repeats traffic it merely overhears -- from altitude "
        "that would flatten the battery and congest regional meshes.",
        apply=Apply.LIVE,
    ),
    _spec(
        "repeater_tag", Kind.STRING, "Meshtastic Repeater",
        "Text prefix that marks a broadcast for repeating. A message addressed "
        "directly to the balloon's node id is always eligible regardless.",
        apply=Apply.LIVE,
    ),
    _spec(
        "repeater_max_per_hour", Kind.INT, "Meshtastic Repeater",
        "Hard ceiling on rebroadcasts per hour, whatever is asked of it.",
        apply=Apply.LIVE, minimum=1, maximum=200,
    ),
    _spec(
        "repeater_min_spacing_sec", Kind.INT, "Meshtastic Repeater",
        "Minimum gap between rebroadcasts, so a burst of requests cannot "
        "monopolise the channel below.",
        apply=Apply.LIVE, minimum=0, maximum=3600, unit="s",
    ),
    _spec(
        "repeater_rx_window_sec", Kind.FLOAT, "Meshtastic Repeater",
        "How long the radio listens for LoRa each time the scheduler grants it "
        "a receive slot. While listening the balloon is deaf to nothing else -- "
        "the image downlink is paused for the duration.",
        apply=Apply.LIVE, minimum=0.5, maximum=60.0, unit="s",
    ),
    _spec(
        "repeater_rx_percent", Kind.FLOAT, "Meshtastic Repeater",
        "Share of cruise airtime spent listening. Taken from the idle budget, "
        "so it costs battery rather than imagery.",
        apply=Apply.LIVE, minimum=0.0, maximum=50.0, unit="%",
    ),
    _spec(
        "uplink_commands_enabled", Kind.BOOL, "Meshtastic Repeater",
        "Allow a message addressed to the balloon on the PRIVATE channel to run "
        "one of a short allowlist of commands. Requires a private channel with "
        "a real key: nothing on the public channel can ever command the "
        "balloon, whatever it says.",
        apply=Apply.LIVE,
    ),

    # === Meshtastic Region ===
    _spec(
        "radio_hardware_band", Kind.ENUM, "Meshtastic Region",
        "Which front end this SX1262 board has. The chip spans 150-960 MHz but "
        "the board's matching network, filters and PA are tuned for one range "
        "-- driving an HF board at 433 MHz radiates almost nothing and can "
        "damage the amplifier. Regions outside the band are unavailable, and "
        "flying over one silences Meshtastic rather than transmitting out of "
        "band.",
        apply=Apply.RESTART, choices=("HF", "LF", "CUSTOM"),
        choice_labels=(
            "HF - Waveshare Core1262-HF (850-930 MHz)",
            "LF - Waveshare Core1262-LF (410-510 MHz)",
            "Custom range",
        ),
    ),
    _spec(
        "radio_band_min_mhz", Kind.FLOAT, "Meshtastic Region",
        "Lower edge of the front end's range. Used only when "
        "radio_hardware_band is CUSTOM.",
        apply=Apply.RESTART, minimum=150.0, maximum=960.0, unit="MHz",
        advanced=True,
    ),
    _spec(
        "radio_band_max_mhz", Kind.FLOAT, "Meshtastic Region",
        "Upper edge of the front end's range. Used only when "
        "radio_hardware_band is CUSTOM.",
        apply=Apply.RESTART, minimum=150.0, maximum=960.0, unit="MHz",
        advanced=True,
    ),
    _spec(
        "meshtastic_region", Kind.ENUM, "Meshtastic Region",
        "Home region band plan, used when auto-switching is off and before the "
        "first GPS fix. Must be reachable by the configured hardware band.",
        apply=Apply.LIVE,
        choices=(
            "US", "EU_433", "EU_868", "CN", "JP", "ANZ", "KR", "TW", "RU",
            "IN", "NZ_865", "TH", "UA_433", "UA_868", "MY_433", "MY_919",
            "SG_923", "PH_433", "PH_868", "PH_915", "BR_902", "NP_865",
        ),
    ),
    _spec(
        "meshtastic_region_auto", Kind.BOOL, "Meshtastic Region",
        "Follow the balloon's position and switch to the local band plan, so "
        "stations in the region it is over can actually hear it. Over "
        "territory with no known band plan, Meshtastic transmission stops "
        "rather than guessing.",
        apply=Apply.LIVE,
    ),
    _spec(
        "meshtastic_region_dwell_sec", Kind.INT, "Meshtastic Region",
        "How long a new region must be observed before the balloon retunes. "
        "Prevents band thrashing along a border.",
        apply=Apply.LIVE, minimum=0, maximum=3600, unit="s",
    ),
    _spec(
        "meshtastic_region_edge_margin_km", Kind.INT, "Meshtastic Region",
        "How far inside a new region the balloon must be before the change "
        "counts. 0 disables the margin test.",
        apply=Apply.LIVE, minimum=0, maximum=500, unit="km",
    ),

    # === Flight Zones ===
    _spec(
        "zone_scheduling_enabled", Kind.BOOL, "Flight Zones",
        "Vary the image/Meshtastic airtime split by flight zone. With this "
        "off, images run continuously and Meshtastic uses its fixed interval.",
        apply=Apply.LIVE,
    ),
    _spec(
        "zone_launch_latitude", Kind.FLOAT, "Flight Zones",
        "Launch site latitude. Leave both coordinates at 0 to capture the "
        "launch point automatically from the first 3D fix.",
        apply=Apply.LIVE, minimum=-90.0, maximum=90.0, unit="deg",
    ),
    _spec(
        "zone_launch_longitude", Kind.FLOAT, "Flight Zones",
        "Launch site longitude. Leave both coordinates at 0 to capture the "
        "launch point automatically from the first 3D fix.",
        apply=Apply.LIVE, minimum=-180.0, maximum=180.0, unit="deg",
    ),
    _spec(
        "zone_radius_m", Kind.INT, "Flight Zones",
        "Radius of the launch zone. Inside it the balloon spends almost all "
        "its airtime on images, because the recovery crew and ground station "
        "are here and the link is short.",
        apply=Apply.LIVE, minimum=100, maximum=200000, unit="m",
    ),
    _spec(
        "zone_hysteresis_m", Kind.INT, "Flight Zones",
        "How far past the radius the balloon must go to leave the launch "
        "zone, and how far back inside to return. Stops a balloon tracking "
        "the boundary from thrashing between schedules.",
        apply=Apply.LIVE, minimum=0, maximum=50000, unit="m",
    ),
    _spec(
        "zone_altitude_override_m", Kind.INT, "Flight Zones",
        "Above this height over the launch site, force cruise mode whatever "
        "the ground distance. A balloon at altitude is no longer a "
        "launch-site problem even if it is still overhead. 0 disables.",
        apply=Apply.LIVE, minimum=0, maximum=40000, unit="m",
    ),

    # === Zone: Launch ===
    _spec(
        "zone_launch_image_percent", Kind.FLOAT, "Zone Launch",
        "Share of airtime spent on images inside the launch zone.",
        apply=Apply.LIVE, minimum=0.0, maximum=100.0, unit="%",
    ),
    _spec(
        "zone_launch_mesh_percent", Kind.FLOAT, "Zone Launch",
        "Share of airtime spent on Meshtastic inside the launch zone.",
        apply=Apply.LIVE, minimum=0.0, maximum=100.0, unit="%",
    ),
    _spec(
        "zone_launch_beacon_interval_sec", Kind.INT, "Zone Launch",
        "Meshtastic beacon interval inside the launch zone. This is a hard "
        "floor: a beacon that comes due wins over any image backlog.",
        apply=Apply.LIVE, minimum=30, maximum=7200, unit="s",
    ),

    # === Zone: Cruise ===
    _spec(
        "zone_cruise_image_percent", Kind.FLOAT, "Zone Cruise",
        "Share of airtime spent on images outside the launch zone.",
        apply=Apply.LIVE, minimum=0.0, maximum=100.0, unit="%",
    ),
    _spec(
        "zone_cruise_mesh_percent", Kind.FLOAT, "Zone Cruise",
        "Share of airtime spent on Meshtastic outside the launch zone. The "
        "remainder is idle, which is what conserves battery for the descent.",
        apply=Apply.LIVE, minimum=0.0, maximum=100.0, unit="%",
    ),
    _spec(
        "zone_cruise_beacon_interval_sec", Kind.INT, "Zone Cruise",
        "Meshtastic beacon interval outside the launch zone.",
        apply=Apply.LIVE, minimum=30, maximum=7200, unit="s",
    ),

    # === Zone: Landed ===
    _spec(
        "zone_landed_enabled", Kind.BOOL, "Zone Landed",
        "Detect landing and switch to a Meshtastic-only recovery beacon. When "
        "the payload is on the ground the image link is usually blocked by "
        "terrain, and a low-rate LoRa beacon is often what actually finds it.",
        apply=Apply.LIVE,
    ),
    _spec(
        "zone_landed_altitude_m", Kind.INT, "Zone Landed",
        "Below this height above the launch site, landing becomes possible.",
        apply=Apply.LIVE, minimum=0, maximum=10000, unit="m",
    ),
    _spec(
        "zone_landed_vertical_rate_mps", Kind.FLOAT, "Zone Landed",
        "Below this ascent or descent rate counts as stationary.",
        apply=Apply.LIVE, minimum=0.05, maximum=20.0, unit="m/s",
    ),
    _spec(
        "zone_landed_dwell_sec", Kind.INT, "Zone Landed",
        "How long the landed conditions must hold before switching. A false "
        "landing before apogee would stop image capture for the whole flight, "
        "so this is deliberately hard to trigger.",
        apply=Apply.LIVE, minimum=10, maximum=3600, unit="s",
    ),
    _spec(
        "zone_landed_arm_altitude_m", Kind.INT, "Zone Landed",
        "Landing detection stays disarmed until the balloon has been this "
        "high above the launch site. Without it a payload sitting on the pad "
        "during setup -- low and stationary for far longer than any dwell "
        "period -- declares itself landed before launch and stops capturing.",
        apply=Apply.LIVE, minimum=100, maximum=20000, unit="m",
    ),
    _spec(
        "zone_landed_mesh_percent", Kind.FLOAT, "Zone Landed",
        "Share of airtime spent on Meshtastic after landing. Keep it low: on "
        "the ground the battery has to outlast the drive to the site.",
        apply=Apply.LIVE, minimum=0.0, maximum=100.0, unit="%",
    ),
    _spec(
        "zone_landed_beacon_interval_sec", Kind.INT, "Zone Landed",
        "Recovery beacon interval after landing.",
        apply=Apply.LIVE, minimum=15, maximum=3600, unit="s",
    ),
    _spec(
        "zone_slice_sec", Kind.FLOAT, "Flight Zones",
        "Length of one airtime grant. Shorter interleaves images and beacons "
        "more finely at the cost of more radio mode switches; measure the "
        "switch cost with tools/bench_lora.py before reducing it.",
        apply=Apply.LIVE, minimum=0.25, maximum=60.0, unit="s", advanced=True,
    ),

    # === Debug ===
    _spec(
        "debug_mode", Kind.BOOL, "Debug",
        "Verbose logging.",
        apply=Apply.LIVE, env="RAPTORHAB_DEBUG",
    ),
    _spec(
        "simulate_gps", Kind.BOOL, "Debug",
        "Generate a synthetic GPS track instead of reading hardware.",
        apply=Apply.RESTART, env="RAPTORHAB_SIMULATE_GPS",
    ),
    _spec(
        "simulate_camera", Kind.BOOL, "Debug",
        "Generate synthetic images instead of using the camera.",
        apply=Apply.RESTART, env="RAPTORHAB_SIMULATE_CAMERA",
    ),
    _spec(
        "allow_lt_fallback", Kind.BOOL, "Debug",
        "Permit the LT fountain encoder when RaptorQ is unavailable. The "
        "ground station cannot decode LT symbols, so images transmitted this "
        "way are unrecoverable. Bench testing only -- never enable for flight.",
        apply=Apply.RESTART, advanced=True,
    ),

    # === Hardware Pins (advanced) ===
    _spec("pin_cs", Kind.INT, "Hardware Pins", "SX1262 chip select (BCM).",
          apply=Apply.RESTART, minimum=0, maximum=27, advanced=True),
    _spec("pin_clk", Kind.INT, "Hardware Pins", "SPI clock (BCM).",
          apply=Apply.RESTART, minimum=0, maximum=27, advanced=True),
    _spec("pin_mosi", Kind.INT, "Hardware Pins", "SPI MOSI (BCM).",
          apply=Apply.RESTART, minimum=0, maximum=27, advanced=True),
    _spec("pin_miso", Kind.INT, "Hardware Pins", "SPI MISO (BCM).",
          apply=Apply.RESTART, minimum=0, maximum=27, advanced=True),
    _spec("pin_busy", Kind.INT, "Hardware Pins", "SX1262 BUSY (BCM).",
          apply=Apply.RESTART, minimum=0, maximum=27, advanced=True),
    _spec("pin_dio1", Kind.INT, "Hardware Pins", "SX1262 DIO1 interrupt (BCM).",
          apply=Apply.RESTART, minimum=0, maximum=27, advanced=True),
    _spec("pin_txen", Kind.INT, "Hardware Pins", "PA transmit enable (BCM).",
          apply=Apply.RESTART, minimum=0, maximum=27, advanced=True),
    _spec("pin_rst", Kind.INT, "Hardware Pins", "SX1262 reset (BCM).",
          apply=Apply.RESTART, minimum=0, maximum=27, advanced=True),
)


SPECS_BY_NAME: Dict[str, ParamSpec] = {s.name: s for s in PARAM_SPECS}


def get_spec(name: str) -> Optional[ParamSpec]:
    return SPECS_BY_NAME.get(name)


def get_schema(defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Build the JSON schema consumed by the configuration UI.

    Args:
        defaults: Optional mapping of parameter name to default value, included
            so the UI can offer a "reset to default" action.
    """
    params: List[Dict[str, Any]] = []
    for spec in PARAM_SPECS:
        entry = spec.to_json()
        if defaults is not None and spec.name in defaults:
            value = defaults[spec.name]
            entry["default"] = list(value) if isinstance(value, tuple) else value
        params.append(entry)

    present = {s.category for s in PARAM_SPECS}
    categories = [c for c in CATEGORY_ORDER if c in present]
    categories += sorted(present - set(CATEGORY_ORDER))

    return {"categories": categories, "parameters": params}


def validate_cross_field(values: Dict[str, Any]) -> List[str]:
    """
    Check constraints that span more than one parameter.

    Returns a list of human-readable problems; empty means valid.
    """
    problems: List[str] = []

    telemetry = values.get("telemetry_interval_packets")
    meta = values.get("image_meta_interval_packets")
    if isinstance(telemetry, int) and isinstance(meta, int) and telemetry > 0:
        if meta % telemetry == 0:
            problems.append(
                f"image_meta_interval_packets ({meta}) is a multiple of "
                f"telemetry_interval_packets ({telemetry}); image metadata would "
                f"never be scheduled. Choose values that are not multiples, "
                f"e.g. {telemetry} and {telemetry * 4 + 1}."
            )

    symbol_size = values.get("fountain_symbol_size")
    if isinstance(symbol_size, int):
        # IMAGE_DATA payload = image_id(2) + symbol_id(4) + symbol bytes,
        # and the whole payload must fit MAX_PAYLOAD_SIZE (243).
        if symbol_size + 6 > 243:
            problems.append(
                f"fountain_symbol_size ({symbol_size}) leaves no room for the "
                f"6-byte image data header within the 243-byte maximum payload; "
                f"use {243 - 6} or less."
            )

    # The radio board can only transmit inside one band. Catching a mismatch
    # here means the operator finds out at configuration time rather than
    # discovering mid-flight that Meshtastic never came up, or worse, keying
    # the PA into a matching network tuned for a different band.
    band_code = values.get("radio_hardware_band")
    if isinstance(band_code, str) and band_code:
        from common.meshtastic.regions import (
            frequency_for_channel,
            get_region,
            regions_within_band,
            resolve_hardware_band,
        )

        band = resolve_hardware_band(
            band_code,
            values.get("radio_band_min_mhz"),
            values.get("radio_band_max_mhz"),
        )
        if band is None and band_code.upper() == "CUSTOM":
            problems.append(
                "radio_hardware_band is CUSTOM but radio_band_min_mhz / "
                "radio_band_max_mhz do not describe a valid range."
            )
        if band is not None:
            channel = values.get("meshtastic_channel_name") or "LongFast"

            home_code = values.get("meshtastic_region")
            home = get_region(home_code) if isinstance(home_code, str) else None
            if home is not None:
                frequency = frequency_for_channel(home, channel)
                if not band.contains(frequency):
                    reachable = [
                        r.code for r in regions_within_band(band, channel)
                    ]
                    problems.append(
                        f"meshtastic_region {home.code} puts channel "
                        f"{channel!r} at {frequency:.3f} MHz, outside the "
                        f"{band_code} board's {band}. Reachable regions: "
                        f"{', '.join(reachable) or 'none'}."
                    )

            image_freq = values.get("radio_frequency_mhz")
            if isinstance(image_freq, (int, float)) and not band.contains(image_freq):
                problems.append(
                    f"radio_frequency_mhz {image_freq} MHz is outside the "
                    f"{band_code} board's {band}."
                )

    # Uplink commands run only on the private channel. Enabling them without
    # one is not a partial configuration, it is a no-op the operator would
    # reasonably assume was working.
    if values.get("uplink_commands_enabled"):
        if not values.get("meshtastic_private_enabled"):
            problems.append(
                "uplink_commands_enabled requires meshtastic_private_enabled: "
                "commands are only accepted on the private channel, so nothing "
                "on the public channel can command the balloon."
            )
        elif not (values.get("meshtastic_private_psk") or "").strip():
            problems.append(
                "uplink_commands_enabled requires a private channel key. "
                "Without one the channel is unencrypted and anyone could issue "
                "commands."
            )

    fdev = values.get("radio_fdev_hz")
    bitrate = values.get("radio_bitrate_bps")
    if isinstance(fdev, int) and isinstance(bitrate, int):
        # Carson's rule: the occupied bandwidth must stay inside the SX1262's
        # widest receive filter (467 kHz).
        occupied = 2 * (fdev + bitrate / 2)
        if occupied > 467000:
            problems.append(
                f"radio_bitrate_bps ({bitrate}) with radio_fdev_hz ({fdev}) needs "
                f"{occupied / 1000:.0f} kHz of bandwidth, beyond the 467 kHz "
                f"receiver filter. Reduce one of them."
            )

    return problems
