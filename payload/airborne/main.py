#!/usr/bin/env python3
"""
RaptorHab Airborne - Main Controller (Transmit-Only)

Entry point for the airborne payload. Implements the main state machine
that coordinates all subsystems: GPS, camera, radio, and telemetry.

This is a transmit-only payload - all configuration is done via config file.

State Machine:
    INITIALIZING -> TX_ACTIVE <-> TX_PAUSED (if pause configured)
                        |
                   ERROR_STATE (auto-reboot)
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from queue import Queue, Empty, Full
from typing import Optional, Dict, Any

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.constants import PacketType
from common.protocol import build_packet
from common.radio import SX1262Radio

from airborne.config import AirborneConfig
from airborne.power import (
    LaunchWiFiCutoff,
    apply_flight_power_saving,
    restore_wifi,
)
from airborne.utils import (
    setup_logging,
    get_cpu_temperature,
    get_battery_voltage,
    get_disk_usage,
    get_memory_usage,
    Watchdog,
)
from common.gps import GPSReader, GPSData
from airborne.camera import CameraModule, ImageInfo
from airborne.telemetry import TelemetryCollector, TelemetryLogger
from airborne.packets import PacketScheduler, ImageTransmission
from airborne.fountain import FountainEncoder
from airborne.meshtastic_beacon import BeaconTelemetry, ChannelConfig, MeshtasticBeacon
from airborne.region_manager import RegionManager
from airborne.repeater import MeshtasticRepeater
from airborne.transmit_scheduler import (
    Activity,
    TransmitScheduler,
    schedules_from_config,
)
from airborne.zone_manager import Zone, ZoneManager
from common.meshtastic import frequency_for_channel, get_region
from common.meshtastic.crypto import format_psk_fingerprint, parse_psk
from common.meshtastic.regions import HARDWARE_BANDS, resolve_hardware_band
from common.radio_lora import get_preset
from common.radio_manager import RadioModeManager


class State(Enum):
    """Payload state machine states."""
    INITIALIZING = auto()
    TX_ACTIVE = auto()
    TX_PAUSED = auto()
    ERROR_STATE = auto()
    SHUTDOWN = auto()


@dataclass
class SystemStatus:
    """Current system status."""
    state: State
    uptime_sec: float
    gps_fix: bool
    gps_sats: int
    altitude_m: float
    images_captured: int
    images_queued_for_tx: int
    images_dropped: int
    packets_sent: int
    error_count: int
    cpu_temp: float
    battery_mv: int


class RaptorHabAirborne:
    """
    Main controller for RaptorHab airborne payload (transmit-only).
    
    Coordinates all subsystems and implements continuous TX with
    optional pause periods.
    """
    
    def __init__(self, config: AirborneConfig, debug: bool = False):
        """
        Initialize the airborne payload.
        
        Args:
            config: Configuration object
            debug: Enable debug mode (simulation)
        """
        self.config = config
        self.debug = debug
        # Uptime is an elapsed measurement, so it comes off the monotonic
        # clock; the wall clock steps when NTP first syncs and would otherwise
        # report an uptime of several weeks on a payload that booted a minute
        # ago.
        self._start_time = time.monotonic()

        # Declared here, not only where it is built: the main loop reads it on
        # every pass, and a component that failed to initialise must leave an
        # attribute behind rather than an AttributeError.
        self._wifi_cutoff = None
        self._power_report = None
        self._mesh_log = None
        self._flight_summary = None
        self._gps_watchdog = None
        self._last_flight_summary_at = 0.0
        
        # State machine
        self._state = State.INITIALIZING
        self._state_lock = threading.Lock()
        self._state_enter_time = time.monotonic()
        
        # Shutdown flag
        self._shutdown = threading.Event()

        # Set when the main loop finishes, so the watchdog can tell a loop that
        # unwound cooperatively from one that is genuinely wedged in a driver.
        self._main_loop_exited = threading.Event()
        
        # Error tracking.
        # _consecutive_errors drives recovery and is reset by any clean cycle,
        # so a transient SPI glitch every few minutes across a long flight
        # cannot slowly accumulate into a shutdown. _error_count is a lifetime
        # total, reported in telemetry only.
        self._error_count = 0
        self._consecutive_errors = 0
        self._max_errors = config.max_consecutive_errors
        self._last_error: Optional[str] = None
        self._watchdog_fired = False
        
        # Statistics
        self._packets_sent = 0
        self._images_captured = 0
        self._images_queued_for_tx = 0
        self._images_dropped = 0
        
        # Components (initialized in start())
        self._radio: Optional[SX1262Radio] = None
        self._gps: Optional[GPSReader] = None
        self._camera: Optional[CameraModule] = None
        self._telemetry: Optional[TelemetryCollector] = None
        self._telemetry_logger: Optional[TelemetryLogger] = None
        self._scheduler: Optional[PacketScheduler] = None
        self._watchdog: Optional[Watchdog] = None
        # Pet at a quarter of the timeout: comfortably inside the window even
        # if a cycle runs long, without waking the CPU for no reason.
        self._watchdog_pet_interval: float = 5.0

        # Meshtastic (Phase 3). The zone-aware scheduling that decides how
        # often these run relative to image downlink arrives in Phase 4; for
        # now beacons go out on a fixed interval.
        self._radio_manager: Optional[RadioModeManager] = None
        self._region_manager: Optional[RegionManager] = None
        self._beacon: Optional[MeshtasticBeacon] = None
        self._last_beacon_time: float = 0.0
        self._last_region_code: Optional[str] = None

        # Zone-aware airtime scheduling (Phase 4).
        self._zone_manager: Optional[ZoneManager] = None
        self._tx_scheduler: Optional[TransmitScheduler] = None

        # Selective repeating and uplink commands (Phase 5).
        self._repeater: Optional[MeshtasticRepeater] = None
        
        # Image queue for transmission
        self._image_queue: Queue[ImageInfo] = Queue(maxsize=5)
        
        # Current GPS data
        self._current_gps: Optional[GPSData] = None
        self._gps_lock = threading.Lock()
        
        # Setup logging - use "raptorhab" logger that setup_logging configures
        self._logger = logging.getLogger("raptorhab")
        
    def start(self) -> None:
        """Start the payload systems."""
        self._logger.info("=" * 60)
        self._logger.info("RaptorHab Airborne Payload Starting (TX-Only)")
        self._logger.info(f"Callsign: {self.config.callsign}")
        self._logger.info(f"Frequency: {self.config.frequency_mhz} MHz")
        self._logger.info(f"TX Power: {self.config.tx_power_dbm} dBm")
        self._logger.info(f"TX Period: {self.config.tx_period_sec}s, Pause: {self.config.tx_pause_sec}s")
        self._logger.info(f"Debug mode: {self.debug}")
        self._logger.info("=" * 60)
        
        try:
            self._initialize_components()
            self._set_state(State.TX_ACTIVE)
            self._run_main_loop()
        except KeyboardInterrupt:
            self._logger.info("Keyboard interrupt received")
        except Exception as e:
            self._logger.critical(f"Fatal error: {e}", exc_info=True)
            self._last_error = str(e)
            self._set_state(State.ERROR_STATE)
        finally:
            self._cleanup()
            # Recovery runs after cleanup so the radio and GPIO are released
            # before the service restarts. Previously _handle_error_state was
            # never reached at all and the payload simply stopped transmitting
            # for the remainder of the flight.
            if self._state is State.ERROR_STATE:
                self._handle_error_state()
    
    def _initialize_components(self) -> None:
        """Initialize all hardware and software components."""
        self._logger.info("Initializing components...")
        
        # Create directories
        self.config.ensure_directories()

        # Initialize watchdog
        if self.config.watchdog_enabled:
            self._logger.info(
                f"Starting watchdog ({self.config.watchdog_timeout_sec}s timeout)..."
            )
            self._watchdog = Watchdog(
                timeout_sec=self.config.watchdog_timeout_sec,
                callback=self._watchdog_triggered,
            )
            self._watchdog_pet_interval = max(1.0, self.config.watchdog_timeout_sec / 4.0)
            self._watchdog.start()
        else:
            self._logger.warning("Watchdog disabled by configuration")
        
        # Initialize radio
        # Switch off what a flying payload does not use, before anything else
        # starts drawing current. Off by default; see docs/POWER.md.
        # WiFi is handled separately from the rest when the launch cutoff owns
        # it. The two settings would otherwise contradict each other: killing
        # the radio at boot leaves no way to run the pre-launch checks, which
        # are the whole reason the cutoff waits for launch in the first place.
        cutoff_owns_wifi = self.config.wifi_off_after_launch
        if cutoff_owns_wifi and self.config.power_disable_wifi:
            self._logger.info(
                "WiFi will stay up until launch: wifi_off_after_launch owns "
                "the radio, so power_disable_wifi is not applied at boot"
            )

        if self.config.flight_power_saving:
            report = apply_flight_power_saving(
                disable_wifi_radio=(
                    self.config.power_disable_wifi and not cutoff_owns_wifi
                ),
                disable_bt=self.config.power_disable_bluetooth,
                disable_video=self.config.power_disable_hdmi,
                disable_led=self.config.power_disable_led,
            )
            self._power_report = report.as_dict()
        else:
            self._power_report = None
            self._logger.info(
                "Power saving is off; WiFi, Bluetooth and HDMI stay powered "
                "(roughly 100 mA). Enable flight_power_saving before launch."
            )

        self._wifi_cutoff = None
        if cutoff_owns_wifi:
            # Undo any block left over from a previous flight before arming.
            # This is what makes a power cycle the reliable way back into a
            # recovered payload -- systemd-rfkill would otherwise restore the
            # in-flight block on every boot, forever.
            restored = restore_wifi()
            if not restored.applied:
                self._logger.warning(
                    f"Could not confirm WiFi is up at boot: {restored.detail}"
                )

            self._wifi_cutoff = LaunchWiFiCutoff(
                altitude_agl_m=self.config.wifi_off_altitude_agl_m,
                confirmations_needed=self.config.wifi_off_confirmations,
                enabled=True,
            )
            self._logger.info(
                f"WiFi cutoff armed: off after "
                f"{self.config.wifi_off_altitude_agl_m} m AGL confirmed on "
                f"{self.config.wifi_off_confirmations} consecutive 3D fixes"
            )

        self._logger.info("Initializing radio...")
        self._radio = SX1262Radio(
            frequency_mhz=self.config.frequency_mhz,
            tx_power_dbm=self.config.tx_power_dbm,
            bitrate_bps=self.config.bitrate_bps,
            fdev_hz=self.config.fdev_hz,
            pin_cs=self.config.pin_cs,
            pin_busy=self.config.pin_busy,
            pin_dio1=self.config.pin_dio1,
            pin_reset=self.config.pin_rst,
            pin_txen=self.config.pin_txen,
            simulation=self.debug,
        )
        self._radio.init()
        self._logger.info(f"Radio initialized at {self.config.frequency_mhz} MHz")
        
        # Initialize GPS
        self._logger.info("Initializing GPS...")
        self._gps = GPSReader(
            port=self.config.gps_device,
            baudrate=self.config.gps_baudrate,
            airborne_mode=self.config.gps_airborne_mode,  # Enable balloon mode on airborne unit
            callback=self._on_gps_update,
            simulation=self.debug,
        )
        self._gps.balloon_mode_on_boot = self.config.gps_balloon_mode_on_boot
        self._gps.balloon_mode_altitude_m = self.config.gps_balloon_mode_altitude_m
        if self._gps.init():
            self._gps.start()
            self._logger.info("GPS reader started")
        else:
            self._logger.warning("GPS initialization failed - continuing without GPS")
        
        # One sealed writer, shared by the camera and the telemetry logger, so
        # imagery and position history are protected the same way.
        from common.sealedwriter import SealedWriter
        self._sealed_writer = SealedWriter(
            public_key_text=self.config.recording_public_key,
            enabled=self.config.recording_encryption_enabled,
        )
        if self.config.recording_encryption_enabled and not self._sealed_writer.active:
            self._logger.error(
                "Recording encryption is enabled but no usable public key is "
                "configured; images and logs will be written UNENCRYPTED"
            )

        # Initialize camera with settings from config
        self._logger.info("Initializing camera...")
        self._camera = CameraModule(
            resolution=self.config.camera_resolution,
            burst_count=self.config.camera_burst_count,
            webp_quality=self.config.webp_quality,
            overlay_enabled=self.config.image_overlay_enabled,
            storage_path=self.config.image_storage_path,
            callsign=self.config.callsign,
            simulation=self.debug,
            sealed_writer=self._sealed_writer,
            release_when_idle=self.config.camera_release_when_idle,
            warmup_sec=self.config.camera_warmup_sec,
            warmup_frames=self.config.camera_warmup_frames,
            tuning_mode=self.config.camera_tuning,
        )
        self._camera.init()
        
        # Apply camera settings from config
        self._apply_camera_settings()
        self._logger.info("Camera initialized")
        
        # Initialize telemetry
        if self.config.mesh_log_enabled:
            import os
            from airborne.mesh_log import MeshtasticLog
            from datetime import datetime

            name = datetime.now().strftime(
                f"meshheard_{self.config.callsign}_%Y%m%d_%H%M%S.csv")
            self._mesh_log = MeshtasticLog(
                os.path.join(self.config.log_path, name),
                sealed_writer=self._sealed_writer,
            )
            self._logger.info(
                f"Mesh logging on for cruise: every packet heard goes to "
                f"{self._mesh_log.filepath}"
            )

        self._logger.info("Initializing telemetry...")
        self._telemetry = TelemetryCollector()
        self._telemetry_logger = TelemetryLogger(
            log_path=self.config.log_path,
            callsign=self.config.callsign,
            sealed_writer=self._sealed_writer,
        )
        
        # Verify the image path end to end before flight rather than
        # discovering on the first capture that nothing can decode us.
        self._preflight_check_fountain_encoder()

        # Initialize packet scheduler
        self._logger.info("Initializing packet scheduler...")
        self._scheduler = PacketScheduler(
            telemetry_interval=self.config.telemetry_interval_packets,
            image_meta_interval=self.config.image_meta_interval_packets,
            symbol_size=self.config.fountain_symbol_size,
            overhead_percent=self.config.fountain_overhead_percent,
            allow_lt_fallback=self.config.allow_lt_fallback,
        )
        
        # Radio mode arbitration. Everything that touches the radio goes
        # through this from here on, so a Meshtastic beacon can never land in
        # the middle of an image packet.
        self._radio_manager = RadioModeManager(
            self._radio, gfsk_tx_power_dbm=self.config.radio_power_dbm
        )

        if self.config.meshtastic_enabled:
            self._initialize_meshtastic()
        else:
            self._logger.info("Meshtastic disabled by configuration")

        if self.config.zone_scheduling_enabled:
            self._initialize_zone_scheduling()
        else:
            self._logger.info(
                "Zone scheduling disabled; images run continuously"
            )

        self._initialize_flight_recorders()

        self._logger.info("All components initialized successfully")

    def _initialize_flight_recorders(self) -> None:
        """The flight-scale record and the receiver watchdog."""
        from airborne.flight_summary import FlightSummary
        from airborne.gps_watchdog import GPSWatchdog

        self._flight_summary = (
            FlightSummary() if self.config.flight_summary_enabled else None
        )
        self._last_flight_summary_at = 0.0

        self._gps_watchdog = GPSWatchdog(
            self._gps,
            no_fix_timeout_sec=self.config.gps_watchdog_no_fix_sec,
            escalation_interval_sec=self.config.gps_watchdog_escalation_sec,
            enabled=self.config.gps_watchdog_enabled,
        ) if self._gps is not None else None

    def _initialize_zone_scheduling(self) -> None:
        """Set up flight zone tracking and the airtime allocator."""
        self._logger.info("Initializing zone scheduling...")

        self._zone_manager = ZoneManager(
            launch_latitude=self.config.zone_launch_latitude,
            launch_longitude=self.config.zone_launch_longitude,
            radius_m=self.config.zone_radius_m,
            hysteresis_m=self.config.zone_hysteresis_m,
            altitude_override_m=self.config.zone_altitude_override_m,
            descent_rate_mps=self.config.zone_descent_rate_mps,
            descent_dwell_sec=(
                self.config.zone_descent_dwell_sec
                if self.config.zone_descent_enabled
                # Unreachable dwell disables the zone without a second path.
                else float("inf")
            ),
            landed_altitude_m=self.config.zone_landed_altitude_m,
            landed_vertical_rate_mps=self.config.zone_landed_vertical_rate_mps,
            landed_arm_altitude_m=self.config.zone_landed_arm_altitude_m,
            launch_settle_sec=self.config.zone_launch_settle_sec,
            launch_settle_max_drift_m=self.config.zone_launch_settle_max_drift_m,
            state_path=self.config.flight_state_path,
            landed_dwell_sec=(
                self.config.zone_landed_dwell_sec
                if self.config.zone_landed_enabled
                # An unreachable dwell disables landing detection without
                # needing a second code path for it.
                else float("inf")
            ),
        )

        self._tx_scheduler = TransmitScheduler(
            schedules=schedules_from_config(self.config),
            slice_sec=self.config.zone_slice_sec,
        )
        self._tx_scheduler.set_zone(self._zone_manager.zone)
        self._apply_zone_telemetry_rate()

        if self._zone_manager.launch_point_known:
            self._logger.info(
                f"Launch point configured: "
                f"{self.config.zone_launch_latitude:.5f}, "
                f"{self.config.zone_launch_longitude:.5f}, "
                f"radius {self.config.zone_radius_m / 1000:.1f} km"
            )
        else:
            self._logger.info(
                f"Launch point will be captured from the first 3D fix; "
                f"radius {self.config.zone_radius_m / 1000:.1f} km"
            )

    def _initialize_meshtastic(self) -> None:
        """Set up the Meshtastic beacon and region tracking."""
        self._logger.info("Initializing Meshtastic...")

        preset = get_preset(self.config.meshtastic_modem_preset)
        hardware_band = resolve_hardware_band(
            self.config.radio_hardware_band,
            self.config.radio_band_min_mhz,
            self.config.radio_band_max_mhz,
        )
        if hardware_band is None:
            self._logger.error(
                f"Cannot resolve radio_hardware_band "
                f"{self.config.radio_hardware_band!r}; assuming HF (850-930 MHz)"
            )
            hardware_band = HARDWARE_BANDS["HF"]

        self._region_manager = RegionManager(
            home_region_code=self.config.meshtastic_region,
            auto_switch=self.config.meshtastic_region_auto,
            dwell_sec=self.config.meshtastic_region_dwell_sec,
            edge_margin_km=self.config.meshtastic_region_edge_margin_km,
            hardware_band=hardware_band,
            channel_name=self.config.meshtastic_channel_name,
            bandwidth_khz=int(preset.bandwidth_khz),
        )

        try:
            primary_psk = parse_psk(self.config.meshtastic_channel_psk)
        except ValueError as e:
            self._logger.error(
                f"Primary channel PSK is invalid ({e}); falling back to the "
                f"Meshtastic default key"
            )
            primary_psk = b"\x01"

        primary = ChannelConfig(
            name=self.config.meshtastic_channel_name, psk=primary_psk
        )

        private = None
        if self.config.meshtastic_private_enabled:
            try:
                private_psk = parse_psk(self.config.meshtastic_private_psk)
            except ValueError as e:
                self._logger.error(
                    f"Private channel PSK is invalid ({e}); private channel "
                    f"disabled rather than transmitting it in the clear"
                )
                private_psk = None

            if not private_psk:
                self._logger.error(
                    "Private channel is enabled but has no key. Refusing to "
                    "transmit an unencrypted 'private' channel; set "
                    "meshtastic_private_psk or disable the channel."
                )
            else:
                private = ChannelConfig(
                    name=self.config.meshtastic_private_name, psk=private_psk
                )
                self._logger.info(
                    f"Private channel key fingerprint: "
                    f"{format_psk_fingerprint(private.key)}"
                )

        self._beacon = MeshtasticBeacon(
            callsign=self.config.callsign,
            payload_id=self.config.payload_id,
            long_name=self.config.meshtastic_long_name or None,
            primary_channel=primary,
            private_channel=private,
            beacon_text=self.config.meshtastic_beacon_text,
            project_url=self.config.meshtastic_project_url,
            hop_limit=self.config.meshtastic_hop_limit,
            nodeinfo_every=self.config.meshtastic_nodeinfo_every,
        )

        if self.config.repeater_enabled:
            self._repeater = MeshtasticRepeater(
                node_id=self._beacon.node_id,
                primary_channel=primary,
                private_channel=private,
                tag=self.config.repeater_tag,
                hop_limit=self.config.meshtastic_hop_limit,
                max_per_hour=self.config.repeater_max_per_hour,
                min_spacing_sec=self.config.repeater_min_spacing_sec,
                enabled=True,
                commands_enabled=(
                    self.config.uplink_commands_enabled and private is not None
                ),
                command_handlers=self._build_command_handlers(),
            )
            self._logger.info(
                f"Repeater enabled: tag {self.config.repeater_tag!r}, "
                f"max {self.config.repeater_max_per_hour}/hour, "
                f"uplink commands "
                f"{'on' if self._repeater.commands_enabled else 'off'}"
            )
            if self.config.uplink_commands_enabled and private is None:
                self._logger.error(
                    "Uplink commands are enabled but no private channel is "
                    "configured; commands are refused. Anyone can transmit on "
                    "the public channel, so it is never accepted for commands."
                )

        if self.config.meshtastic_hop_limit > 0:
            self._logger.warning(
                f"hop_limit is {self.config.meshtastic_hop_limit}, not 0. From "
                f"altitude this balloon reaches a very large number of nodes; "
                f"letting them rebroadcast can congest regional meshes."
            )

        self._apply_region_to_radio(force=True)

    def _build_command_handlers(self):
        """
        The uplink command allowlist.

        Deliberately short, and deliberately excludes anything that could put
        the balloon off the air. There is no command to stop transmitting, to
        change frequency, or to reboot: a radio link is exactly the wrong
        place to expose controls whose failure mode is silence.
        """
        def status(_args):
            zone = self._zone_manager.zone.value if self._zone_manager else "?"
            region = self._region_manager.state.code if self._region_manager else "?"
            with self._gps_lock:
                gps = self._current_gps
            altitude = f"{gps.altitude:.0f}m" if gps else "?"
            return (f"{self.config.callsign} zone={zone} region={region} "
                    f"alt={altitude} pkts={self._packets_sent}")

        def position(_args):
            with self._gps_lock:
                gps = self._current_gps
            if not gps or gps.fix_type < 2:
                return "no GPS fix"
            return (f"{gps.latitude:.5f},{gps.longitude:.5f} "
                    f"{gps.altitude:.0f}m sats={gps.satellites}")

        def ping(_args):
            return f"{self.config.callsign} pong"

        def beacon(args):
            """Change the broadcast message, so a recovery crew can be told."""
            # Bare "!beacon" used to join zero arguments into an empty string
            # and clear the message. That is a poor thing to do by accident
            # over a link with no undo, so it now reports instead, and
            # clearing has to be asked for.
            if not args:
                current = self.config.meshtastic_beacon_text
                return f"beacon: {current}" if current else "beacon text is empty"
            if len(args) == 1 and args[0].lower() == "clear":
                text = ""
            else:
                text = " ".join(args)[:120]
            result = self.config.apply_updates({"meshtastic_beacon_text": text})
            if not result["ok"]:
                return "rejected"
            if self._beacon:
                self._beacon.beacon_text = text
            return f"beacon text set ({len(text)} chars)"

        def capture(_args):
            if not self._capture_allowed():
                return "capture disabled in this zone"
            self._trigger_capture()
            return f"capture triggered ({self._images_captured} total)"

        def help_(_args):
            """
            List what this balloon accepts.

            Worth the airtime: the operator may be running an older ground
            station, or a different one, and guessing at a command set over a
            link with no error messages is miserable.
            """
            return "commands: " + " ".join(
                "!" + name for name in sorted(handlers) if name != "position")

        handlers = {
            "help": help_,
            "commands": help_,
            "status": status,
            "pos": position,
            "position": position,
            "ping": ping,
            "beacon": beacon,
            "capture": capture,
        }
        return handlers

    def _run_listen_window(self, duration_sec: float) -> None:
        """
        Listen for LoRa, and act on anything addressed to us.

        The radio cannot hear LoRa while transmitting images, so this window
        is genuinely exclusive -- it is charged to the listen budget precisely
        so that cost is visible rather than hidden.
        """
        if not self._radio_manager:
            return
        if not (self._repeater or self._mesh_log):
            return
        if not (self._region_manager and self._region_manager.may_transmit):
            return

        try:
            received = self._radio_manager.receive_lora_window(duration_sec)
        except Exception as e:
            self._logger.error(f"Listen window failed: {e}")
            return
        finally:
            self._radio_manager.ensure_gfsk()

        # The repeater's rule is unchanged: with no zone manager, assume cruise
        # and allow repeating. The log's rule is deliberately stricter -- only
        # record when the zone is known to be cruise, because "we are not sure
        # where we are" is not a reason to start recording other people's
        # traffic.
        in_cruise = self._zone_manager is None or self._zone_manager.zone is Zone.CRUISE
        known_cruise = (self._zone_manager is not None
                        and self._zone_manager.zone is Zone.CRUISE)

        altitude = None
        with self._gps_lock:
            if self._current_gps is not None and self._current_gps.fix_type >= 2:
                altitude = self._current_gps.altitude

        for raw, rssi, snr in received:
            packet = (self._repeater.decode(raw, rssi=rssi, snr=snr)
                      if self._repeater else None)
            if self._repeater:
                self._repeater.note_heard(packet is not None)

            # Cruise only. The altitude at the moment of reception is the whole
            # point: it is what turns a list of nodes into propagation data.
            if self._mesh_log is not None and known_cruise:
                self._mesh_log.record(packet, raw=raw, rssi=rssi, snr=snr,
                                      altitude_m=altitude)

            if packet is None:
                continue
            if not self._repeater:
                continue

            reply = self._repeater.handle_command(packet)
            if reply is not None:
                self._radio_manager.transmit_lora(reply)
                self._radio_manager.ensure_gfsk()
                continue

            allowed, reason = self._repeater.should_repeat(packet, in_cruise=in_cruise)
            if not allowed:
                self._repeater.stats.drop(reason)
                continue

            self._radio_manager.transmit_lora(self._repeater.build_repeat(packet))
            self._radio_manager.ensure_gfsk()

    def _apply_region_to_radio(self, force: bool = False) -> None:
        """
        Push the active region's frequency and power ceiling to the radio.

        When the region is unknown the LoRa settings are deliberately left
        unset, which makes RadioModeManager refuse to transmit Meshtastic at
        all. Guessing a frequency over unfamiliar territory is the one outcome
        that must not happen; the image downlink is unaffected.
        """
        if not (self._region_manager and self._radio_manager):
            return

        state = self._region_manager.state
        if not force and state.code == self._last_region_code:
            return

        self._last_region_code = state.code

        if not self._region_manager.may_transmit:
            self._radio_manager.clear_lora_settings()
            self._logger.warning(
                "Meshtastic suspended: no known band plan for this position"
            )
            return

        region = state.region
        preset = get_preset(self.config.meshtastic_modem_preset)
        frequency = self._region_manager.active_frequency_mhz

        if frequency is None:
            # may_transmit was true, so this should be unreachable; refusing
            # is still better than transmitting on an unknown frequency.
            self._radio_manager.clear_lora_settings()
            self._logger.error(
                "Region reports transmittable but yielded no frequency; "
                "suspending Meshtastic"
            )
            return

        settings = self._radio_manager.set_lora_settings(
            preset,
            frequency_mhz=frequency,
            requested_power_dbm=self.config.meshtastic_tx_power_dbm,
            region=region,
        )

        self._logger.info(
            f"Meshtastic region {region.code}: {frequency:.4f} MHz at "
            f"{settings.tx_power_dbm} dBm "
            f"(source: {state.source.value})"
        )
    
    def _preflight_check_fountain_encoder(self) -> None:
        """
        Confirm the payload can produce image symbols the ground can decode.

        The encoder used to fall back silently from RaptorQ to LT whenever the
        raptorq wheel failed to import. The ground station has no LT decoding
        path, so that fallback cost the entire flight's imagery while logging
        only a single INFO line at startup. Failing here instead means the
        problem is found on the bench.
        """
        from airborne.fountain import raptorq_available

        if raptorq_available():
            self._logger.info("Fountain encoder preflight: RaptorQ available")
            return

        message = (
            "RaptorQ is not available. The ground station cannot decode the LT "
            "fallback, so no image transmitted this flight would be "
            "recoverable. Install the wheel from payload/raptor_wheel/."
        )

        if self.config.allow_lt_fallback:
            self._logger.error(f"{message} Continuing because allow_lt_fallback is set.")
            return

        raise RuntimeError(message)

    def _apply_camera_settings(self) -> None:
        """Apply camera settings from config."""
        if not self._camera:
            return
        
        self._logger.info("Applying camera settings from config...")
        self._camera.set_brightness(self.config.camera_brightness)
        self._camera.set_contrast(self.config.camera_contrast)
        self._camera.set_saturation(self.config.camera_saturation)
        self._camera.set_sharpness(self.config.camera_sharpness)
        self._camera.set_exposure_comp(self.config.camera_exposure_comp)
        self._camera.set_awb_mode(self.config.camera_awb_mode)
        self._camera.set_red_gain(self.config.camera_red_gain)
        self._camera.set_blue_gain(self.config.camera_blue_gain)
        
        self._logger.info(
            f"Camera settings: brightness={self.config.camera_brightness}, "
            f"contrast={self.config.camera_contrast}, "
            f"saturation={self.config.camera_saturation}, "
            f"sharpness={self.config.camera_sharpness}, "
            f"exposure={self.config.camera_exposure_comp}, "
            f"awb={self.config.camera_awb_mode}, "
            f"red_gain={self.config.camera_red_gain}, "
            f"blue_gain={self.config.camera_blue_gain}"
        )
    
    def _run_main_loop(self) -> None:
        """Main control loop implementing TX with duty cycle.

        Alternates between TX_ACTIVE and TX_PAUSED using the tx_period_sec and
        tx_pause_sec configuration values.
        """
        self._logger.info(f"Entering main loop (TX: {self.config.tx_period_sec}s, Pause: {self.config.tx_pause_sec}s)")

        # Initial image capture
        self._trigger_capture()
        last_capture_time = time.monotonic()
        last_status_time = time.monotonic()

        try:
            self._main_loop_body(last_capture_time, last_status_time)
        finally:
            self._main_loop_exited.set()

    def _main_loop_body(self, last_capture_time: float, last_status_time: float) -> None:
        while not self._shutdown.is_set():
            try:
                # Pet the watchdog
                if self._watchdog:
                    self._watchdog.pet()
                
                now = time.monotonic()

                # Keep the flight zone current before deciding what to do.
                self._update_zone()

                # Capture on interval, unless the zone says not to. A landed
                # payload should be spending its remaining battery on beacons.
                if self._capture_allowed() and (
                    now - last_capture_time >= self.config.capture_interval_sec
                ):
                    self._trigger_capture()
                    last_capture_time = now

                # Status logging (every 10 seconds)
                if now - last_status_time >= 10.0:
                    self._log_status()
                    last_status_time = now

                if self._tx_scheduler is not None:
                    self._run_scheduled_slice()
                else:
                    # Zone scheduling disabled: the original fixed duty cycle.
                    self._set_state(State.TX_ACTIVE)
                    self._run_tx_cycle()
                    self._run_beacon_if_due()

                    if self.config.tx_pause_sec > 0:
                        self._set_state(State.TX_PAUSED)
                        self._run_pause_cycle()

                # A complete cycle without an exception clears the consecutive
                # error run.
                if self._consecutive_errors:
                    self._logger.info(
                        f"Recovered after {self._consecutive_errors} consecutive "
                        f"error(s); resetting counter"
                    )
                    self._consecutive_errors = 0

            except Exception as e:
                self._error_count += 1
                self._consecutive_errors += 1
                self._last_error = str(e)
                self._logger.error(
                    f"Error in main loop "
                    f"({self._consecutive_errors}/{self._max_errors} consecutive, "
                    f"{self._error_count} total): {e}",
                    exc_info=True,
                )

                if self._consecutive_errors >= self._max_errors:
                    self._set_state(State.ERROR_STATE)
                    break

                # Back off briefly so a tight failure loop cannot starve the
                # watchdog thread or spin the CPU.
                time.sleep(min(1.0 * self._consecutive_errors, 5.0))
    
    def _update_zone(self) -> None:
        """Refresh the flight zone and point the scheduler at its budget."""
        if self._zone_manager is None:
            return

        with self._gps_lock:
            gps = self._current_gps

        if gps is not None:
            self._zone_manager.update(
                latitude=gps.latitude,
                longitude=gps.longitude,
                altitude_m=gps.altitude,
                fix_type=gps.fix_type,
            )
        else:
            self._zone_manager.update(fix_type=0)

        if self._tx_scheduler is not None:
            self._tx_scheduler.set_zone(self._zone_manager.zone)
            self._apply_zone_telemetry_rate()

        state = self._zone_manager.state

        # The receiver needs its relaxed dynamic model before it reaches the
        # ceiling of the default one. Altitude is the trigger; a zone that
        # means "airborne" is the backstop for a receiver that lost its fix
        # on the way up and so cannot report an altitude to trigger on.
        if self._gps is not None:
            self._gps.ensure_high_altitude_mode(
                altitude_agl_m=state.altitude_agl_m,
                # The backstop is for a receiver that cannot report an
                # altitude to trigger on -- airborne by flight state, blind
                # by fix. Forcing on the zone alone would defeat the
                # threshold entirely, since cruise begins far below it.
                force=(
                    state.altitude_agl_m is None
                    and self._zone_manager.zone in (Zone.CRUISE, Zone.DESCENT)
                ),
            )

        if self._gps_watchdog is not None:
            self._gps_watchdog.update(
                has_fix=(gps is not None and gps.fix_type >= 2)
            )

        if self._flight_summary is not None and gps is not None and gps.fix_type >= 2:
            self._flight_summary.note_position(
                latitude=gps.latitude,
                longitude=gps.longitude,
                altitude_m=gps.altitude,
                vertical_rate_mps=state.vertical_rate_mps,
            )

        # Fed from here rather than straight off the GPS, because the height
        # that matters is above the *launch site* and that reference is the
        # zone manager's -- settled, not the receiver's first guess.
        if self._wifi_cutoff is not None:
            self._wifi_cutoff.update(
                altitude_agl_m=self._zone_manager.state.altitude_agl_m,
                fix_type=(gps.fix_type if gps is not None else 0),
            )

    def _maybe_queue_flight_summary(self) -> None:
        """Queue the flight summary packet when its interval comes round.

        Queued rather than sent directly, so it takes an ordinary packet slot
        and cannot displace an overdue beacon or stall the image stream.
        """
        if self._flight_summary is None or self._scheduler is None:
            return
        now = time.monotonic()
        if now - self._last_flight_summary_at < self.config.flight_summary_interval_sec:
            return
        self._last_flight_summary_at = now

        try:
            zone_index = list(Zone).index(self._zone_manager.zone) if self._zone_manager else 0
            payload = self._flight_summary.as_payload(
                packets_sent=self._packets_sent,
                images_captured=self._images_captured,
                zone_index=zone_index,
            )
            self._scheduler.queue_packet(PacketType.FLIGHT_SUMMARY, payload)
            self._logger.info(
                f"Flight summary queued: apogee {payload.max_altitude_m:.0f} m, "
                f"{payload.distance_travelled_m / 1000:.1f} km travelled, "
                f"{payload.flight_time_sec // 60} min aloft"
            )
        except Exception as exc:
            self._logger.error(f"Could not build the flight summary: {exc}")

    def _apply_zone_telemetry_rate(self) -> None:
        """Let the zone set how often telemetry rides the packet stream.

        Descent thins the image stream right down, so a telemetry interval
        counted in packets would stretch out just as position updates start
        mattering most. The zone's own interval wins where it has one; every
        other zone keeps the configured default.
        """
        if self._tx_scheduler is None or self._scheduler is None:
            return
        schedule = self._tx_scheduler.schedule_for()
        interval = (
            schedule.telemetry_interval_packets
            or self.config.telemetry_interval_packets
        )
        if interval != self._scheduler.telemetry_interval:
            self._scheduler.telemetry_interval = interval
            self._logger.info(
                f"Telemetry cadence now every {interval} packets "
                f"({self._tx_scheduler.zone.value})"
            )

    def _capture_allowed(self) -> bool:
        """Whether image capture should be running in the current zone."""
        if self._tx_scheduler is None:
            return True
        return self._tx_scheduler.capture_enabled

    def _run_scheduled_slice(self) -> None:
        """
        Execute one grant of airtime.

        The scheduler says what to do and roughly for how long; this runs it
        and reports back the real duration, because a slice always finishes
        the packet it started and so routinely overruns.
        """
        scheduler = self._tx_scheduler
        grant = scheduler.next_slice()
        started = time.monotonic()

        try:
            if grant.activity is Activity.IMAGES:
                self._set_state(State.TX_ACTIVE)
                self._run_tx_cycle(duration_sec=grant.duration_sec)

            elif grant.activity is Activity.MESHTASTIC:
                self._set_state(State.TX_ACTIVE)
                self._run_beacon_cycle()
                scheduler.record_beacon()

            elif grant.activity is Activity.LISTEN:
                self._set_state(State.TX_PAUSED)
                self._run_listen_window(
                    min(grant.duration_sec, self.config.repeater_rx_window_sec)
                )

            else:
                self._set_state(State.TX_PAUSED)
                self._run_pause_cycle(duration_sec=grant.duration_sec)

        finally:
            scheduler.record(grant.activity, time.monotonic() - started)

    def _log_status(self) -> None:
        """Periodic one-line summary of what the payload is doing."""
        parts = [
            f"{self._packets_sent} packets",
            f"{self._images_captured} images",
        ]

        if self._zone_manager is not None:
            state = self._zone_manager.state
            distance = (
                f"{state.distance_from_launch_m / 1000:.1f}km"
                if state.distance_from_launch_m is not None
                else "?"
            )
            altitude = (
                f"{state.altitude_agl_m:.0f}m"
                if state.altitude_agl_m is not None
                else "?"
            )
            parts.append(f"zone={state.zone.value} ({distance}, {altitude} AGL)")

        if self._tx_scheduler is not None:
            fractions = self._tx_scheduler.stats.fractions()
            parts.append(
                f"airtime img/mesh/idle="
                f"{fractions['images']:.0f}/{fractions['meshtastic']:.0f}/"
                f"{fractions['idle']:.0f}%"
            )

        if self._region_manager is not None:
            parts.append(f"region={self._region_manager.state.code}")

        if self._repeater is not None:
            stats = self._repeater.stats
            parts.append(f"mesh heard={stats.heard} repeated={stats.repeated}")

        self._logger.info("Status: " + ", ".join(parts))

    def _run_tx_cycle(self, duration_sec: Optional[float] = None) -> None:
        """
        Transmit telemetry and image data for a bounded period.

        Args:
            duration_sec: How long to transmit. Defaults to the fixed
                tx_period_sec; the zone scheduler passes its slice length.
        """
        cycle_start = time.monotonic()
        tx_duration = (
            self.config.tx_period_sec if duration_sec is None else duration_sec
        )
        
        self._logger.debug(f"Starting TX cycle ({tx_duration}s)")
        
        # Process any queued images
        self._process_image_queue()
        
        # Update telemetry with current data
        self._update_telemetry()

        # And the flight-scale record, on its own slower cadence.
        self._maybe_queue_flight_summary()
        
        # Transmit packets until time expires
        packets_this_cycle = 0
        last_pet = 0.0
        while time.monotonic() - cycle_start < tx_duration:
            if self._shutdown.is_set():
                break
            
            # Get next packet from scheduler
            packet = self._scheduler.get_next_packet(self._get_telemetry_payload())
            
            if packet:
                success = self._transmit_packet(packet)
                if success:
                    packets_this_cycle += 1
                    self._packets_sent += 1
            else:
                # Nothing ready. The old 1 ms sleep spun the CPU a thousand
                # times a second waiting for a scheduler that grants packets on
                # a far slower cadence, which burns power to no purpose. 20 ms
                # is still far below the interval between packets and costs
                # fifty wake-ups a second instead of a thousand.
                time.sleep(0.02)
            
            # Pet on a clock, not on a packet count. The old
            # `packets_this_cycle % 100 == 0` petted on every single iteration
            # while the counter sat at zero, and then stopped petting entirely
            # if the counter came to rest on a non-multiple of 100 -- which is
            # exactly what happens when the radio starts failing partway
            # through a cycle. Wrong in both directions.
            now = time.monotonic()
            if self._watchdog and now - last_pet >= self._watchdog_pet_interval:
                self._watchdog.pet()
                last_pet = now
        
        self._logger.debug(f"TX cycle complete: {packets_this_cycle} packets sent")
    
    def _run_pause_cycle(self, duration_sec: Optional[float] = None) -> None:
        """
        Hold the radio idle.

        In cruise and landed zones this is where most of the wall clock goes,
        and it is the whole point: an idle radio is what leaves enough battery
        for the descent and for beaconing once the payload is down.
        """
        pause_duration = (
            self.config.tx_pause_sec if duration_sec is None else duration_sec
        )

        if pause_duration <= 0:
            return
        
        self._logger.debug(f"Starting pause cycle ({pause_duration}s)")
        
        # Put radio in standby during pause
        if self._radio:
            self._radio.set_standby()
        
        # The watchdog times out after tens of seconds; petting it ten times a
        # second was six hundred times more often than it needed, and each
        # wake-up keeps the core out of its deeper idle states. Sleep in
        # half-second slices instead: still responsive to shutdown, still far
        # inside the watchdog window, and an order of magnitude fewer wakes.
        #
        # This is the payload's largest single block of idle time -- in cruise
        # it is most of the flight -- so it is the one worth getting right.
        PAUSE_SLICE_SEC = 0.5
        pause_start = time.monotonic()
        last_pet = 0.0
        while True:
            remaining = pause_duration - (time.monotonic() - pause_start)
            if remaining <= 0 or self._shutdown.is_set():
                break

            now = time.monotonic()
            if self._watchdog and now - last_pet >= self._watchdog_pet_interval:
                self._watchdog.pet()
                last_pet = now

            time.sleep(min(PAUSE_SLICE_SEC, remaining))
        
        self._logger.debug("Pause cycle complete")
    
    def _transmit_packet(self, packet: bytes) -> bool:
        """Transmit a single RAPTOR packet on the GFSK downlink."""
        if not self._radio_manager:
            return False

        try:
            return self._radio_manager.transmit_gfsk(packet)
        except Exception as e:
            self._logger.error(f"TX error: {e}")
            return False

    def _run_beacon_if_due(self) -> None:
        """
        Send a Meshtastic beacon cycle if the interval has elapsed.

        Runs between TX cycles rather than inside one, so a mode switch can
        never interrupt an image packet mid-flight. Phase 4 replaces this
        fixed interval with the zone-aware schedule.
        """
        if not (self._beacon and self._radio_manager):
            return

        now = time.monotonic()
        if now - self._last_beacon_time < self.config.meshtastic_beacon_interval_sec:
            return

        self._last_beacon_time = now
        self._run_beacon_cycle()

    def _run_beacon_cycle(self) -> None:
        """
        Send one Meshtastic beacon cycle unconditionally.

        Called either by the fixed-interval path or by the zone scheduler,
        which owns the cadence itself.
        """
        if not (self._beacon and self._radio_manager):
            return

        # Refresh the region from the current fix before transmitting, so a
        # border crossing retunes the radio before the beacon rather than
        # after it.
        if self._region_manager:
            with self._gps_lock:
                gps = self._current_gps

            if gps is not None:
                self._region_manager.update(
                    latitude=gps.latitude,
                    longitude=gps.longitude,
                    fix_type=gps.fix_type,
                )
            else:
                self._region_manager.update(fix_type=0)

            self._apply_region_to_radio()

            if not self._region_manager.may_transmit:
                return

        try:
            self._beacon.transmit_cycle(
                self._radio_manager,
                self._collect_beacon_telemetry(),
                region_manager=self._region_manager,
                inter_packet_delay_sec=(
                    self.config.meshtastic_inter_packet_delay_ms / 1000.0
                ),
            )
        except Exception as e:
            self._logger.error(f"Beacon cycle failed: {e}", exc_info=True)
        finally:
            # Always hand the radio back to the image downlink, even if the
            # beacon threw partway through.
            self._radio_manager.ensure_gfsk()

    def _collect_beacon_telemetry(self) -> BeaconTelemetry:
        """Snapshot the payload state a Meshtastic beacon reports."""
        telemetry = BeaconTelemetry(
            battery_mv=get_battery_voltage(),
            cpu_temp_c=get_cpu_temperature(),
            uptime_sec=int(time.monotonic() - self._start_time),
        )

        # Meshtastic's battery_level is a percentage. Map a single-cell
        # lithium range onto it; 101 would mean "externally powered".
        millivolts = telemetry.battery_mv
        if millivolts:
            percent = (millivolts - 3300) / (4200 - 3300) * 100
            telemetry.battery_percent = max(0, min(100, int(percent)))

        with self._gps_lock:
            gps = self._current_gps

        if gps is not None:
            telemetry.latitude = gps.latitude
            telemetry.longitude = gps.longitude
            telemetry.altitude_m = gps.altitude
            telemetry.satellites = gps.satellites
            telemetry.fix_type = gps.fix_type
            telemetry.ground_speed_mps = gps.speed
            telemetry.ground_track_deg = gps.heading

        return telemetry
    
    def _process_image_queue(self) -> None:
        """Hand captured images to the transmit scheduler."""
        while True:
            try:
                image_info = self._image_queue.get_nowait()
            except Empty:
                return

            if not image_info.webp_data:
                continue

            self._logger.info(f"Adding image {image_info.image_id} to scheduler")
            queued = self._scheduler.add_image(
                image_id=image_info.image_id,
                image_data=image_info.webp_data,
                width=image_info.width,
                height=image_info.height,
                timestamp=image_info.timestamp,
            )

            if queued:
                self._images_queued_for_tx += 1
            else:
                # The scheduler's queue is bounded. Put the image back and stop
                # draining, rather than discarding it and still counting it as
                # transmitted the way the previous version did.
                self._images_dropped += 1
                self._logger.warning(
                    f"Scheduler queue full; image {image_info.image_id} deferred"
                )
                try:
                    self._image_queue.put_nowait(image_info)
                except Full:
                    self._logger.error(
                        f"Image {image_info.image_id} dropped: both queues full"
                    )
                return
    
    def _trigger_capture(self) -> None:
        """Trigger image capture."""
        if not self._camera:
            return
        
        # Get current GPS position
        latitude = 0.0
        longitude = 0.0
        altitude = 0.0
        
        with self._gps_lock:
            if self._current_gps:
                latitude = self._current_gps.latitude
                longitude = self._current_gps.longitude
                altitude = self._current_gps.altitude
        
        try:
            image_info = self._camera.capture(latitude, longitude, altitude)
            
            if image_info:
                self._images_captured += 1
                self._logger.info(f"Captured image {image_info.image_id}: {image_info.size_bytes} bytes")

                # Let the sensor go until the next capture. Does nothing unless
                # camera_release_when_idle is set.
                if self._camera:
                    self._camera.release()
                
                # Queue for transmission
                try:
                    self._image_queue.put_nowait(image_info)
                except Full:
                    self._images_dropped += 1
                    self._logger.warning(
                        f"Capture queue full; dropping image {image_info.image_id}"
                    )


        except Exception as e:
            self._logger.error(f"Capture error: {e}")
    
    def _force_capture(self) -> bool:
        """Force immediate image capture."""
        self._logger.info("Forcing image capture")
        self._trigger_capture()
        return True
    
    def _on_gps_update(self, gps_data: GPSData) -> None:
        """Callback for GPS updates."""
        with self._gps_lock:
            self._current_gps = gps_data
        
        # Log telemetry
        if self._telemetry_logger and gps_data.fix_type >= 1:
            self._telemetry_logger.log(
                timestamp=gps_data.time_utc,
                latitude=gps_data.latitude,
                longitude=gps_data.longitude,
                altitude=gps_data.altitude,
                speed=gps_data.speed,
                heading=gps_data.heading,
                satellites=gps_data.satellites,
                fix_type=gps_data.fix_type,
            )
    
    def _update_telemetry(self) -> None:
        """Update telemetry collector with current data."""
        if not self._telemetry:
            return
        
        # GPS data
        with self._gps_lock:
            if self._current_gps:
                self._telemetry.update_gps(self._current_gps)
        
        # System data
        self._telemetry.update_system(
            battery_mv=get_battery_voltage(),
            cpu_temp=get_cpu_temperature(),
            radio_temp=self._radio.get_temperature() if self._radio else 0,
        )
        
        # Image progress
        if self._scheduler:
            progress = self._scheduler.get_image_progress()
            self._telemetry.update_image_status(
                image_id=progress.get("image_id", 0),
                progress=progress.get("progress", 0),
            )
        
        # RSSI (not applicable for TX-only, set to 0)
        self._telemetry.update_rssi(0)
    
    def _get_telemetry_payload(self) -> bytes:
        """Get current telemetry as payload bytes."""
        if self._telemetry:
            return self._telemetry.get_payload_bytes()
        return b"\x00" * 36  # Empty telemetry
    
    def _get_status_dict(self) -> Dict[str, Any]:
        """Get current status as dictionary."""
        return {
            "uptime": time.monotonic() - self._start_time,
            "state": self._state.name,
            "cpu_temp": get_cpu_temperature(),
            "free_memory_kb": get_memory_usage().get("available_kb", 0),
            "packets_sent": self._packets_sent,
            "images_captured": self._images_captured,
            "error_count": self._error_count,
        }
    
    def _set_state(self, new_state: State) -> None:
        """Set new state machine state."""
        with self._state_lock:
            old_state = self._state
            self._state = new_state
            self._state_enter_time = time.monotonic()
            if old_state != new_state:
                self._logger.info(f"State change: {old_state.name} -> {new_state.name}")
    
    def _handle_error_state(self) -> None:
        """
        Handle error state: exit non-zero so systemd restarts the service, and
        only fall back to a full reboot if the service manager is not running
        us.

        A service restart is dramatically cheaper than a reboot -- roughly a
        second of lost transmit time instead of thirty -- and it clears the
        same failure modes. The reboot path stays as a last resort for cases
        where the process was started by hand.
        """
        self._logger.critical(
            f"Error state entered after {self._consecutive_errors} consecutive "
            f"errors ({self._error_count} total). Last error: {self._last_error}"
        )

        if not self.config.reboot_on_fatal_error:
            self._logger.critical("Automatic recovery disabled by configuration")
            return

        # INVOCATION_ID is set by systemd for every unit it starts.
        if os.getenv("INVOCATION_ID"):
            self._logger.critical(
                "Exiting non-zero; systemd will restart the payload service"
            )
            sys.exit(1)

        self._logger.critical("Not under systemd - initiating system reboot")
        time.sleep(5)
        try:
            subprocess.run(["sudo", "systemctl", "reboot"], check=True, timeout=30)
        except (subprocess.SubprocessError, OSError) as e:
            self._logger.critical(f"Reboot command failed: {e}")

    def _watchdog_triggered(self) -> None:
        """
        Callback when the watchdog times out.

        Runs on the watchdog thread. It sets the shutdown event so a wedged
        main loop actually unwinds -- previously this only set a state field
        that nothing ever read, so a genuine hang stayed hung for the rest of
        the flight.
        """
        self._logger.critical("WATCHDOG TIMEOUT - main loop is not making progress")
        self._watchdog_fired = True
        self._last_error = "watchdog timeout"
        self._set_state(State.ERROR_STATE)
        self._shutdown.set()

        # A cooperative shutdown only works if the main loop can still reach a
        # check of the shutdown event. If it is blocked in a driver call it
        # never will, so give it a grace period and then terminate hard.
        # os._exit skips atexit and cleanup deliberately: we are already in an
        # unrecoverable state and systemd restarting us is the fastest way back
        # on the air.
        grace_sec = 15.0
        if self._main_loop_exited.wait(timeout=grace_sec):
            return  # Main loop unwound cleanly; normal recovery path takes over.

        self._logger.critical(
            f"Main loop still wedged {grace_sec:.0f}s after watchdog; "
            f"terminating process"
        )
        os._exit(1)
    
    def _cleanup(self) -> None:
        """Clean up resources."""
        self._logger.info("Cleaning up...")
        
        self._shutdown.set()
        
        if self._watchdog:
            self._watchdog.stop()
        
        if self._gps:
            self._gps.stop()
        
        if self._camera:
            self._camera.close()
        
        if self._radio:
            self._radio.close()
        
        if self._telemetry_logger:
            self._telemetry_logger.close()
        
        self._logger.info("Cleanup complete")
    
    def get_status(self) -> SystemStatus:
        """Get current system status."""
        gps_fix = False
        gps_sats = 0
        altitude = 0.0
        
        with self._gps_lock:
            if self._current_gps:
                gps_fix = self._current_gps.fix_type >= 2
                gps_sats = self._current_gps.satellites
                altitude = self._current_gps.altitude
        
        return SystemStatus(
            state=self._state,
            uptime_sec=time.monotonic() - self._start_time,
            gps_fix=gps_fix,
            gps_sats=gps_sats,
            altitude_m=altitude,
            images_captured=self._images_captured,
            images_queued_for_tx=self._images_queued_for_tx,
            images_dropped=self._images_dropped,
            packets_sent=self._packets_sent,
            error_count=self._error_count,
            cpu_temp=get_cpu_temperature(),
            battery_mv=get_battery_voltage(),
        )
    
    def request_shutdown(self) -> None:
        """Request graceful shutdown."""
        self._logger.info("Shutdown requested")
        self._set_state(State.SHUTDOWN)
        self._shutdown.set()


def signal_handler(signum, frame, controller: RaptorHabAirborne):
    """Handle shutdown signals."""
    logging.info(f"Received signal {signum}")
    controller.request_shutdown()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="RaptorHab Airborne Payload (Transmit-Only)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug/simulation mode",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to the JSON configuration file",
    )
    parser.add_argument(
        "--no-config-file",
        action="store_true",
        help="Ignore the persisted config file; use defaults plus environment only",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print the resolved configuration as JSON and exit",
    )
    parser.add_argument(
        "--print-schema",
        action="store_true",
        help="Print the parameter schema as JSON and exit",
    )
    parser.add_argument(
        "--save-config",
        action="store_true",
        help="Write the resolved configuration back to the config file and exit",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    parser.add_argument(
        "--callsign",
        type=str,
        default=None,
        help="Override callsign",
    )
    parser.add_argument(
        "--frequency",
        type=float,
        default=None,
        help="Override frequency (MHz)",
    )
    parser.add_argument(
        "--power",
        type=int,
        default=None,
        help="Override TX power (dBm)",
    )
    parser.add_argument(
        "--tx-pause",
        type=int,
        default=None,
        help="Pause between TX bursts (seconds, 0=continuous)",
    )
    
    args = parser.parse_args()

    # Schema does not depend on the resolved config, so serve it first.
    if args.print_schema:
        print(json.dumps(AirborneConfig().schema(), indent=2))
        return

    # Load configuration: defaults -> file -> environment.
    if args.no_config_file:
        config = AirborneConfig.from_env()
    else:
        config = AirborneConfig.load(path=args.config)

    # Apply command line overrides last; they win over everything.
    cli_overrides = {}
    if args.callsign:
        cli_overrides["callsign"] = args.callsign
    if args.frequency:
        cli_overrides["radio_frequency_mhz"] = args.frequency
    if args.power:
        cli_overrides["radio_power_dbm"] = args.power
    if args.tx_pause is not None:
        cli_overrides["tx_pause_sec"] = args.tx_pause

    if cli_overrides:
        result = config.apply_updates(cli_overrides)
        if not result["ok"]:
            for name, reason in result["rejected"].items():
                print(f"error: invalid --{name.replace('_', '-')}: {reason}",
                      file=sys.stderr)
            sys.exit(2)

    if args.print_config:
        print(json.dumps(config.to_dict(redact_secrets=True), indent=2, sort_keys=True))
        return

    if args.save_config:
        ok = config.save()
        print(f"{'Saved' if ok else 'FAILED to save'} config to {config.config_path}")
        sys.exit(0 if ok else 1)

    # Setup logging
    log_level = getattr(logging, args.log_level.upper())
    setup_logging(
        log_path=config.log_path,
        level=log_level,
        name="raptorhab",
    )
    
    # Create controller
    controller = RaptorHabAirborne(config, debug=args.debug)
    
    # Setup signal handlers
    signal.signal(
        signal.SIGTERM,
        lambda s, f: signal_handler(s, f, controller),
    )
    signal.signal(
        signal.SIGINT,
        lambda s, f: signal_handler(s, f, controller),
    )
    
    # Start the payload
    controller.start()


if __name__ == "__main__":
    main()
