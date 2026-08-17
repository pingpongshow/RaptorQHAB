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
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from queue import Queue, Empty
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
    images_transmitted: int
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
        
        # Error tracking
        self._error_count = 0
        self._max_errors = 10
        self._last_error: Optional[str] = None
        
        # Statistics
        self._packets_sent = 0
        self._images_captured = 0
        self._images_transmitted = 0
        
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
            self._set_state(State.ERROR_STATE)
        finally:
            self._cleanup()
    
    def _initialize_components(self) -> None:
        """Initialize all hardware and software components."""
        self._logger.info("Initializing components...")
        
        # Create directories
        os.makedirs(self.config.image_storage_path, exist_ok=True)
        os.makedirs(self.config.log_path, exist_ok=True)
        
        # Initialize watchdog
        self._logger.info("Starting watchdog...")
        self._watchdog = Watchdog(
            timeout_sec=60,
            callback=self._watchdog_triggered,
        )
        self._watchdog.start()
        
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
        
        # Initialize packet scheduler
        self._logger.info("Initializing packet scheduler...")
        self._scheduler = PacketScheduler(
            telemetry_interval=self.config.telemetry_interval_packets,
            image_meta_interval=self.config.image_meta_interval_packets,
            symbol_size=self.config.fountain_symbol_size,
            overhead_percent=self.config.fountain_overhead_percent,
        )
        
        self._logger.info("All components initialized successfully")
    
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
        
        FIXED: Now properly alternates between TX_ACTIVE and TX_PAUSED states
        using the tx_period_sec and tx_pause_sec configuration values.
        """
        self._logger.info(f"Entering main loop (TX: {self.config.tx_period_sec}s, Pause: {self.config.tx_pause_sec}s)")
        
        # Initial image capture
        self._trigger_capture()
        last_capture_time = time.time()
        last_status_time = time.time()
        
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
                
            except Exception as e:
                self._logger.error(f"Error in main loop: {e}", exc_info=True)
                self._error_count += 1
                self._last_error = str(e)
                
                if self._error_count >= self._max_errors:
                    self._set_state(State.ERROR_STATE)
                    break
    
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
        """Process queued images for transmission."""
        try:
            while not self._image_queue.empty():
                image_info = self._image_queue.get_nowait()
                
                if image_info.webp_data:
                    self._logger.info(f"Adding image {image_info.image_id} to scheduler")
                    self._scheduler.add_image(
                        image_id=image_info.image_id,
                        image_data=image_info.webp_data,
                        width=image_info.width,
                        height=image_info.height,
                        timestamp=image_info.timestamp,
                    )
                    self._images_transmitted += 1
        except Empty:
            pass
    
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
                except:
                    self._logger.warning("Image queue full, dropping image")
                    
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
        """Handle error state - attempt recovery or reboot."""
        self._logger.critical(f"Error state entered. Error count: {self._error_count}")
        self._logger.critical(f"Last error: {self._last_error}")
        
        # Wait a bit then reboot
        time.sleep(5)
        
        if self.config.reboot_on_fatal_error:
            self._logger.critical("Initiating reboot due to error state")
            os.system("sudo reboot")
    
    def _watchdog_triggered(self) -> None:
        """Callback when watchdog times out."""
        self._logger.critical("WATCHDOG TIMEOUT - System appears hung")
        self._set_state(State.ERROR_STATE)
    
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
            images_transmitted=self._images_transmitted,
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
        help="Path to configuration file",
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
    
    # Load configuration
    config = AirborneConfig.from_env()
    
    # Apply command line overrides
    if args.callsign:
        config.callsign = args.callsign
    if args.frequency:
        config.radio_frequency_mhz = args.frequency
    if args.power:
        config.radio_power_dbm = args.power
    if args.tx_pause is not None:
        config.tx_pause_sec = args.tx_pause
    
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
