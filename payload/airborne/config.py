"""
RaptorHab Airborne Configuration

Configuration is resolved with the following precedence, lowest first:

    1. Built-in defaults (the dataclass field defaults below)
    2. Persisted JSON config file  (/RaptorHAB/config/airborne.json)
    3. RAPTORHAB_* environment variables
    4. Command line arguments

Every parameter is described in ``airborne.params``, which is also what the
USB configuration UI renders its form from.

Note: constructing a Config has no side effects. Directories are created by
the payload during startup, not at import time -- importing this module must
be safe for tests and for tools running as an unprivileged user.
"""

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from common.configstore import ConfigStore
from airborne.params import (
    Apply,
    PARAM_SPECS,
    SPECS_BY_NAME,
    get_schema,
    validate_cross_field,
)

logger = logging.getLogger(__name__)

# State lives under /var/lib, which is where a system service's writable data
# belongs and, more practically, is a directory the unprivileged service user
# can actually own. The previous /RaptorHAB default could only be created by
# root, so the service failed at startup on any fresh install.
STATE_ROOT = os.environ.get("RAPTORHAB_STATE_ROOT", "/var/lib/raptorhab")

DEFAULT_CONFIG_PATH = os.path.join(STATE_ROOT, "config", "airborne.json")


@dataclass
class Config:
    """Airborne payload configuration."""

    # === Identification ===
    callsign: str = "RPHAB1"
    payload_id: int = 1

    # === Radio Configuration ===
    radio_frequency_mhz: float = 915.0
    radio_power_dbm: int = 22
    radio_bitrate_bps: int = 96000
    radio_fdev_hz: int = 50000

    # === SX1262 Pin Configuration (BCM numbering) ===
    pin_cs: int = 21
    pin_clk: int = 11
    pin_mosi: int = 10
    pin_miso: int = 9
    pin_busy: int = 20
    pin_dio1: int = 16
    pin_txen: int = 6
    pin_rst: int = 18

    # === Timing ===
    tx_period_sec: int = 2           # Duration of each TX burst
    tx_pause_sec: int = 2            # Pause between TX bursts (0 = continuous)
    telemetry_interval_packets: int = 5
    # Must NOT be a multiple of telemetry_interval_packets, or the metadata
    # slot is always shadowed by the telemetry slot and never fires.
    image_meta_interval_packets: int = 23

    # === Camera Configuration ===
    camera_resolution: Tuple[int, int] = (1280, 960)
    camera_burst_count: int = 5
    webp_quality: int = 75

    # Releasing the sensor between captures. Measured on a Pi Zero 2 W: an
    # open, idle camera keeps the SoC about 2 C warmer, and releasing costs
    # about 160 ms per capture against an interval of tens of seconds.
    # Auto-exposure survives the restart, so no settling sleep is needed.
    camera_release_when_idle: bool = False
    camera_warmup_sec: float = 0.0
    camera_warmup_frames: int = 1
    image_overlay_enabled: bool = True

    # Camera image adjustments (0-200 scale, 100 = neutral/normal)
    camera_brightness: int = 100
    camera_contrast: int = 100
    camera_saturation: int = 100
    camera_sharpness: int = 100
    camera_exposure_comp: int = 100
    camera_awb_mode: int = 0

    # Color gain adjustments (50-200, 100 = no adjustment)
    camera_red_gain: int = 100
    camera_blue_gain: int = 100

    # === GPS Configuration ===
    # L76K GPS on Pi hardware UART (GPIO 14=TX, GPIO 15=RX)
    gps_device: str = "/dev/serial0"
    gps_device_alt: str = "/dev/ttyAMA0"
    gps_baudrate: int = 9600
    gps_airborne_mode: bool = True

    # === Fountain Code Configuration ===
    fountain_symbol_size: int = 200
    fountain_overhead_percent: int = 25

    # === Meshtastic ===
    meshtastic_enabled: bool = False
    meshtastic_modem_preset: str = "LONG_FAST"
    meshtastic_channel_name: str = "LongFast"
    # "AQ==" is the well-known default key every Meshtastic client ships with;
    # it obfuscates but does not protect.
    meshtastic_channel_psk: str = "AQ=="
    meshtastic_tx_power_dbm: int = 22
    meshtastic_hop_limit: int = 0
    meshtastic_beacon_interval_sec: int = 300
    meshtastic_project_url: str = "https://github.com/pingpongshow/RaptorQHAB"
    meshtastic_beacon_text: str = ""
    meshtastic_inter_packet_delay_ms: int = 500
    meshtastic_nodeinfo_every: int = 6
    meshtastic_long_name: str = ""

    # === Meshtastic Private Channel ===
    meshtastic_private_enabled: bool = False
    meshtastic_private_name: str = "RaptorHAB"
    meshtastic_private_psk: str = ""

    # === Meshtastic Repeater ===
    repeater_enabled: bool = False
    repeater_tag: str = "!RPT "
    repeater_max_per_hour: int = 20
    repeater_min_spacing_sec: int = 30
    repeater_rx_window_sec: float = 4.0
    repeater_rx_percent: float = 5.0

    # Record every Meshtastic packet heard while in cruise. Nobody has much
    # data on what a mesh looks like from 30 km, and the balloon is the
    # best-placed receiver in the region for the length of a flight.
    #
    # Cheap: the channel caps delivery at about 1.7 packets/s regardless of how
    # many nodes are in range, so a four-hour flight is a few hundred kilobytes;
    # receiving costs ~4.6 mA against a payload drawing 150; and cruise is 90%
    # idle already. Cruise only -- on the pad the airtime belongs to imagery,
    # and once landed the battery belongs to being found.
    #
    # Sealed with the recording key like any other log: it holds other people's
    # positions.
    mesh_log_enabled: bool = False
    mesh_log_rx_percent: float = 10.0
    uplink_commands_enabled: bool = False

    # === Meshtastic Region ===
    # Which SX1262 front end is fitted. The Waveshare Core1262-HF covers
    # 850-930 MHz and so cannot reach the 433 MHz Meshtastic regions or China
    # -- see params.py.
    radio_hardware_band: str = "HF"
    radio_band_min_mhz: float = 850.0
    radio_band_max_mhz: float = 930.0
    meshtastic_region: str = "US"
    meshtastic_region_auto: bool = True
    meshtastic_region_dwell_sec: int = 120
    meshtastic_region_edge_margin_km: int = 25

    # === Flight Zones ===
    zone_scheduling_enabled: bool = True
    # 0,0 means "capture the launch point from the first 3D fix".
    zone_launch_latitude: float = 0.0
    zone_launch_longitude: float = 0.0
    zone_radius_m: int = 8000
    zone_hysteresis_m: int = 800
    zone_altitude_override_m: int = 3000
    zone_slice_sec: float = 2.0

    # Inside the launch radius: almost everything to images.
    zone_launch_image_percent: float = 98.0
    zone_launch_mesh_percent: float = 1.0
    zone_launch_beacon_interval_sec: int = 600

    # Outside it: mostly idle, to conserve battery for the descent.
    zone_cruise_image_percent: float = 5.0
    zone_cruise_mesh_percent: float = 5.0
    zone_cruise_beacon_interval_sec: int = 300

    # On the ground: a slow recovery beacon and nothing else.
    zone_landed_enabled: bool = True
    zone_landed_altitude_m: int = 1000
    zone_landed_vertical_rate_mps: float = 0.5
    zone_landed_dwell_sec: int = 120
    zone_landed_arm_altitude_m: int = 2000

    # The first 3D fix a receiver produces is the worst one it will produce.
    # Measured on the bench: 202 m at 6 satellites, 173 m at 10 satellites two
    # minutes later, without moving. Every AGL figure is measured against the
    # launch altitude, so the reference is refined over this window while the
    # payload is still on the pad. Zero disables it.
    zone_launch_settle_sec: int = 180
    zone_launch_settle_max_drift_m: int = 50
    zone_landed_mesh_percent: float = 5.0
    zone_landed_beacon_interval_sec: int = 60

    # === Recording Encryption ===
    # Off by default: it changes how files are written, and a flight should
    # never fail because of a feature the operator did not ask for.
    recording_encryption_enabled: bool = False
    recording_public_key: str = ""

    # === Storage ===
    image_storage_path: str = os.path.join(STATE_ROOT, "images")
    log_path: str = os.path.join(STATE_ROOT, "logs")

    # Survives a restart in flight. Without it a payload that restarts at 20 km
    # captures a "launch point" 20 km up and every AGL figure afterwards is
    # measured from the wrong datum.
    flight_state_path: str = os.path.join(STATE_ROOT, "flight_state.json")
    max_stored_images: int = 20000

    # === Operational ===
    auto_capture_interval_sec: int = 30
    watchdog_enabled: bool = True
    watchdog_timeout_sec: int = 60

    # Power saving. Off by default: disabling WiFi takes away SSH, and doing
    # that to someone's bench Pi by surprise would be hostile.
    flight_power_saving: bool = False
    power_disable_wifi: bool = True
    power_disable_bluetooth: bool = True
    power_disable_hdmi: bool = True
    power_disable_led: bool = True

    # WiFi is the largest controllable draw in flight, and at altitude it is
    # worse than useless: there is no access point up there, so NetworkManager
    # scans, fails and scans again for the whole flight. It cannot simply be
    # off at boot, because the pre-launch checklist is run over it. So it stays
    # up until the balloon proves it has launched, then goes down for good.
    # A power cycle brings it back -- that is the way into a recovered payload.
    wifi_off_after_launch: bool = True
    wifi_off_altitude_agl_m: int = 300
    wifi_off_confirmations: int = 3
    reboot_on_fatal_error: bool = True
    max_consecutive_errors: int = 10

    # === Debug ===
    debug_mode: bool = False
    simulate_gps: bool = False
    simulate_camera: bool = False
    allow_lt_fallback: bool = False

    # --- non-persisted runtime state -------------------------------------
    # Where this config was loaded from; not itself a configurable parameter.
    config_path: str = DEFAULT_CONFIG_PATH

    # ------------------------------------------------------------------
    # Property aliases for cleaner access in main.py
    # ------------------------------------------------------------------

    @property
    def frequency_mhz(self) -> float:
        return self.radio_frequency_mhz

    @property
    def tx_power_dbm(self) -> int:
        return self.radio_power_dbm

    @tx_power_dbm.setter
    def tx_power_dbm(self, value: int) -> None:
        self.radio_power_dbm = value

    @property
    def bitrate_bps(self) -> int:
        return self.radio_bitrate_bps

    @property
    def fdev_hz(self) -> int:
        return self.radio_fdev_hz

    @property
    def capture_interval_sec(self) -> int:
        return self.auto_capture_interval_sec

    @capture_interval_sec.setter
    def capture_interval_sec(self, value: int) -> None:
        self.auto_capture_interval_sec = value

    # ------------------------------------------------------------------
    # Directories
    # ------------------------------------------------------------------

    def ensure_directories(self) -> None:
        """
        Create the storage directories.

        Called explicitly during payload startup -- never at import time, so
        that importing this module stays side-effect free.
        """
        for path in (self.image_storage_path, self.log_path):
            os.makedirs(path, exist_ok=True)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self, redact_secrets: bool = False) -> Dict[str, Any]:
        """
        Return the configurable parameters as a JSON-friendly dict.

        Args:
            redact_secrets: Replace secret values with None. Used when sending
                configuration to a client that should not see key material.
        """
        raw = asdict(self)
        out: Dict[str, Any] = {}
        for spec in PARAM_SPECS:
            if spec.name not in raw:
                continue
            value = raw[spec.name]
            if redact_secrets and spec.secret:
                out[spec.name] = None
                continue
            out[spec.name] = list(value) if isinstance(value, tuple) else value
        return out

    def schema(self) -> Dict[str, Any]:
        """Parameter schema including this build's defaults, for the UI."""
        return get_schema(defaults=Config().to_dict())

    # ------------------------------------------------------------------
    # Applying updates
    # ------------------------------------------------------------------

    def apply_updates(
        self, updates: Dict[str, Any], allow_unknown: bool = False
    ) -> Dict[str, Any]:
        """
        Validate and apply a batch of parameter changes.

        The batch is all-or-nothing: if any value is invalid, or a cross-field
        constraint fails, nothing is applied. That matters because a partially
        applied radio configuration can be worse than no change at all.

        Returns a dict with:
            applied: list of parameter names that changed
            rejected: {name: reason} for values that failed validation
            unknown: list of names with no matching parameter
            restart_required: names whose change needs a service restart
            ok: True if the batch was applied
        """
        rejected: Dict[str, str] = {}
        unknown: List[str] = []
        candidates: Dict[str, Any] = {}

        for name, value in updates.items():
            spec = SPECS_BY_NAME.get(name)
            if spec is None:
                unknown.append(name)
                if not allow_unknown:
                    rejected[name] = "unknown parameter"
                continue
            try:
                candidates[name] = spec.validate(value)
            except (ValueError, TypeError) as e:
                rejected[name] = str(e)

        if rejected:
            return {
                "ok": False,
                "applied": [],
                "rejected": rejected,
                "unknown": unknown,
                "restart_required": [],
            }

        # Cross-field validation runs against the post-change state.
        prospective = self.to_dict()
        prospective.update(candidates)
        problems = validate_cross_field(prospective)
        if problems:
            return {
                "ok": False,
                "applied": [],
                "rejected": {"_cross_field": "; ".join(problems)},
                "unknown": unknown,
                "restart_required": [],
            }

        applied: List[str] = []
        restart_required: List[str] = []
        for name, value in candidates.items():
            if getattr(self, name) == value:
                continue
            setattr(self, name, value)
            applied.append(name)
            if SPECS_BY_NAME[name].apply is Apply.RESTART:
                restart_required.append(name)

        if applied:
            logger.info(f"Config updated: {', '.join(sorted(applied))}")
        if restart_required:
            logger.info(
                f"Restart required for: {', '.join(sorted(restart_required))}"
            )

        return {
            "ok": True,
            "applied": applied,
            "rejected": {},
            "unknown": unknown,
            "restart_required": restart_required,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Optional[str] = None) -> bool:
        """Persist the current configuration atomically."""
        store = ConfigStore(path or self.config_path)
        return store.save(self.to_dict())

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        path: Optional[str] = None,
        use_env: bool = True,
    ) -> "Config":
        """
        Build a configuration from defaults, then the persisted file, then
        the environment.

        Never raises: an unreadable file or a malformed environment value is
        logged and skipped rather than taking the payload down at startup.
        """
        config = cls()
        config.config_path = path or DEFAULT_CONFIG_PATH

        store = ConfigStore(config.config_path)
        persisted = store.load()
        if persisted:
            result = config.apply_updates(persisted, allow_unknown=True)
            if not result["ok"]:
                # Fall back to applying whatever is individually valid, so one
                # bad key in the file doesn't discard the whole config.
                logger.error(
                    f"Config file rejected as a batch: {result['rejected']}. "
                    f"Applying valid keys individually."
                )
                for name, value in persisted.items():
                    single = config.apply_updates({name: value}, allow_unknown=True)
                    if not single["ok"]:
                        logger.error(
                            f"Ignoring bad config value {name}={value!r}: "
                            f"{single['rejected'].get(name, 'invalid')}"
                        )
            if result.get("unknown"):
                logger.warning(
                    f"Config file has unrecognised keys (preserved on save): "
                    f"{', '.join(sorted(result['unknown']))}"
                )

        if use_env:
            config._apply_env()

        problems = validate_cross_field(config.to_dict())
        if problems:
            for problem in problems:
                logger.error(f"CONFIG PROBLEM: {problem}")

        return config

    def _apply_env(self) -> None:
        """Apply RAPTORHAB_* environment overrides, skipping bad values."""
        for spec in PARAM_SPECS:
            if not spec.env:
                continue
            raw = os.getenv(spec.env)
            if raw is None or raw == "":
                continue
            try:
                setattr(self, spec.name, spec.validate(raw))
                logger.info(f"Config override from {spec.env}: {spec.name}={raw}")
            except (ValueError, TypeError) as e:
                logger.error(
                    f"Ignoring invalid environment override {spec.env}={raw!r}: {e}"
                )

    @classmethod
    def from_env(cls) -> "Config":
        """
        Backwards-compatible entry point: defaults plus environment only, with
        no config file.
        """
        config = cls()
        config._apply_env()
        return config


# Alias kept for existing imports.
AirborneConfig = Config


def default_config() -> Config:
    """
    A fresh default configuration.

    Replaces the old module-level ``DEFAULT_CONFIG`` singleton, which created
    directories as an import side effect and crashed for unprivileged callers.
    """
    return Config()
