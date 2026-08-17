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
        self._start_time = time.time()
        
        # State machine
        self._state = State.INITIALIZING
        self._state_lock = threading.Lock()
        self._state_enter_time = time.time()
        
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
            self._watchdog.start()
        else:
            self._logger.warning("Watchdog disabled by configuration")
        
        # Initialize radio
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
        if self._gps.init():
            self._gps.start()
            self._logger.info("GPS reader started")
        else:
            self._logger.warning("GPS initialization failed - continuing without GPS")
        
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
        )
        self._camera.init()
        
        # Apply camera settings from config
        self._apply_camera_settings()
        self._logger.info("Camera initialized")
        
        # Initialize telemetry
        self._logger.info("Initializing telemetry...")
        self._telemetry = TelemetryCollector()
        self._telemetry_logger = TelemetryLogger(
            log_path=self.config.log_path,
            callsign=self.config.callsign,
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
        
        self._logger.info("All components initialized successfully")
    
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
            "recoverable. Install the wheel from Pi/raptor_wheel/."
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
        last_capture_time = time.time()
        last_status_time = time.time()

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
                
                # Check if it's time for a new capture
                now = time.time()
                if now - last_capture_time >= self.config.capture_interval_sec:
                    self._trigger_capture()
                    last_capture_time = now
                
                # Status logging (every 10 seconds)
                if now - last_status_time >= 10.0:
                    self._logger.info(f"TX status: {self._packets_sent} packets sent, {self._images_captured} images")
                    last_status_time = now
                
                # === TX CYCLE ===
                self._set_state(State.TX_ACTIVE)
                self._run_tx_cycle()
                
                # === PAUSE CYCLE (if configured) ===
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
    
    def _run_tx_cycle(self) -> None:
        """Execute one TX cycle (transmit telemetry and images)."""
        cycle_start = time.time()
        tx_duration = self.config.tx_period_sec
        
        self._logger.debug(f"Starting TX cycle ({tx_duration}s)")
        
        # Process any queued images
        self._process_image_queue()
        
        # Update telemetry with current data
        self._update_telemetry()
        
        # Transmit packets until time expires
        packets_this_cycle = 0
        while time.time() - cycle_start < tx_duration:
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
                # No packet ready, small delay
                time.sleep(0.001)
            
            # Pet watchdog periodically
            if packets_this_cycle % 100 == 0 and self._watchdog:
                self._watchdog.pet()
        
        self._logger.debug(f"TX cycle complete: {packets_this_cycle} packets sent")
    
    def _run_pause_cycle(self) -> None:
        """Execute pause cycle (radio idle)."""
        pause_duration = self.config.tx_pause_sec
        
        if pause_duration <= 0:
            return
        
        self._logger.debug(f"Starting pause cycle ({pause_duration}s)")
        
        # Put radio in standby during pause
        if self._radio:
            self._radio.set_standby()
        
        pause_start = time.time()
        while time.time() - pause_start < pause_duration:
            if self._shutdown.is_set():
                break
            
            # Pet watchdog during pause
            if self._watchdog:
                self._watchdog.pet()
            
            time.sleep(0.1)
        
        self._logger.debug("Pause cycle complete")
    
    def _transmit_packet(self, packet: bytes) -> bool:
        """Transmit a single packet."""
        if not self._radio:
            return False
        
        try:
            return self._radio.transmit(packet)
        except Exception as e:
            self._logger.error(f"TX error: {e}")
            return False
    
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
            "uptime": time.time() - self._start_time,
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
            self._state_enter_time = time.time()
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
            uptime_sec=time.time() - self._start_time,
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
