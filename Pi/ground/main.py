#!/usr/bin/env python3
"""
RaptorHab Ground Station - Main Controller

Entry point for the ground station. Coordinates all subsystems:
radio receiver, telemetry processing, image decoding, web interface.

Note: Command transmission to airborne unit is not supported.
All airborne configuration must be done via config file on the airborne unit.
"""

import argparse
import logging
import math
import os
import signal
import sys
import time
import threading
from pathlib import Path
from typing import Optional, Tuple

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from ground.config import GroundConfig
from ground.receiver import PacketReceiver, SimulatedReceiver
from ground.telemetry import TelemetryProcessor, TelemetryPoint
from ground.decoder import FountainDecoder, ImageMetadata
from ground.storage import ImageStorage
from ground.web import WebServer

# Import GPS from common module (shared code)
try:
    from common.gps import GPS, GPSData
    GPS_AVAILABLE = True
except ImportError:
    GPS_AVAILABLE = False

logger = logging.getLogger(__name__)


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate distance between two points using Haversine formula
    
    Args:
        lat1, lon1: First point (degrees)
        lat2, lon2: Second point (degrees)
        
    Returns:
        Distance in meters
    """
    R = 6371000  # Earth radius in meters
    
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = math.sin(delta_phi / 2) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate initial bearing from point 1 to point 2
    
    Args:
        lat1, lon1: From point (degrees)
        lat2, lon2: To point (degrees)
        
    Returns:
        Bearing in degrees (0-360, where 0=North, 90=East)
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)
    
    x = math.sin(delta_lambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - \
        math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360


def calculate_elevation_angle(ground_lat: float, ground_lon: float, ground_alt: float,
                               target_lat: float, target_lon: float, target_alt: float) -> float:
    """
    Calculate elevation angle from ground to target
    
    Args:
        ground_lat, ground_lon, ground_alt: Ground position (degrees, meters)
        target_lat, target_lon, target_alt: Target position (degrees, meters)
        
    Returns:
        Elevation angle in degrees (0=horizon, 90=overhead)
    """
    horizontal_distance = haversine_distance(ground_lat, ground_lon, target_lat, target_lon)
    altitude_diff = target_alt - ground_alt
    
    if horizontal_distance < 1:  # Avoid division by zero
        return 90.0 if altitude_diff > 0 else -90.0
    
    elevation = math.degrees(math.atan2(altitude_diff, horizontal_distance))
    return elevation


def setup_logging(log_path: str, level: int = logging.INFO, name: str = "raptorhab"):
    """Setup logging configuration"""
    os.makedirs(log_path, exist_ok=True)
    
    # Create formatters
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    
    # File handler
    log_file = os.path.join(log_path, f"{name}.log")
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(console_formatter)
    
    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    # Reduce noise from libraries
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('engineio').setLevel(logging.WARNING)
    logging.getLogger('socketio').setLevel(logging.WARNING)


class GroundStation:
    """
    Main ground station controller
    
    Coordinates all subsystems and provides unified control interface.
    This is a receive-only ground station - no command transmission.
    """
    
    def __init__(self, config: GroundConfig, simulate: bool = False):
        """
        Initialize ground station
        
        Args:
            config: Ground station configuration
            simulate: Enable simulation mode (no real hardware)
        """
        self.config = config
        self.simulate = simulate
        
        # Components
        self._telemetry: Optional[TelemetryProcessor] = None
        self._decoder: Optional[FountainDecoder] = None
        self._storage: Optional[ImageStorage] = None
        self._receiver: Optional[PacketReceiver] = None
        self._sim_receiver: Optional[SimulatedReceiver] = None
        self._web: Optional[WebServer] = None
        self._gps: Optional[GPS] = None
        
        # Current ground station GPS data
        self._ground_gps: Optional[GPSData] = None
        self._ground_gps_lock = threading.Lock()
        
        # State
        self._running = False
        self._shutdown_event = threading.Event()
        
        # Statistics
        self._start_time: float = 0
    
    def start(self):
        """Start the ground station"""
        logger.info("=" * 60)
        logger.info("RaptorHab Ground Station Starting")
        logger.info(f"Callsign: {self.config.callsign}")
        logger.info(f"Frequency: {self.config.frequency_mhz} MHz")
        logger.info(f"Simulation mode: {self.simulate}")
        logger.info("Note: Receive-only mode (no command transmission)")
        logger.info("=" * 60)
        
        self._start_time = time.time()
        self._running = True
        
        try:
            self._initialize_components()
            self._start_components()
            self._run_main_loop()
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        except Exception as e:
            logger.critical(f"Fatal error: {e}", exc_info=True)
        finally:
            self._cleanup()
    
    def _initialize_components(self):
        """Initialize all components"""
        logger.info("Initializing components...")
        
        # Create directories
        os.makedirs(self.config.data_path, exist_ok=True)
        os.makedirs(self.config.image_path, exist_ok=True)
        os.makedirs(self.config.log_path, exist_ok=True)
        
        # Initialize telemetry processor
        logger.info("Initializing telemetry processor...")
        self._telemetry = TelemetryProcessor(
            db_path=self.config.telemetry_db_path,
            buffer_size=1000,
            on_telemetry=self._on_telemetry,
            on_alert=self._on_alert
        )
        self._telemetry.alert_low_battery_mv = self.config.alert_low_battery_mv
        self._telemetry.alert_high_altitude_m = self.config.alert_high_altitude_m
        self._telemetry.alert_descent_rate_mps = self.config.alert_descent_rate_mps
        
        # Initialize image storage
        logger.info("Initializing image storage...")
        self._storage = ImageStorage(
            image_path=self.config.image_path,
            db_path=os.path.join(self.config.data_path, "images.db"),
            max_images=self.config.max_stored_images,
            generate_thumbnails=True
        )
        
        # Set telemetry session_id to match image storage session
        self._telemetry.set_session_id(self._storage.session_id)
        logger.info(f"Session ID: {self._storage.session_id}")
        
        # Initialize fountain decoder
        logger.info("Initializing fountain decoder...")
        self._decoder = FountainDecoder(
            symbol_size=self.config.fountain_symbol_size,
            max_pending=self.config.max_pending_images,
            timeout_sec=self.config.image_timeout_sec,
            on_image_complete=self._on_image_complete
        )
        
        # Initialize receiver (receive-only, no command transmitter)
        if self.simulate:
            logger.info("Initializing simulated receiver...")
            self._sim_receiver = SimulatedReceiver(
                telemetry_processor=self._telemetry,
                image_decoder=self._decoder,
                telemetry_interval_sec=1.0
            )
        else:
            logger.info("Initializing packet receiver...")
            self._receiver = PacketReceiver(
                config=self.config,
                telemetry_processor=self._telemetry,
                image_decoder=self._decoder,
                on_packet=self._on_packet,
                on_text_message=self._on_text_message
            )
        
        # Initialize web interface
        if self.config.enable_web:
            logger.info("Initializing web interface...")
            self._web = WebServer(
                config=self.config,
                receiver=self._receiver,
                telemetry=self._telemetry,
                decoder=self._decoder,
                storage=self._storage,
                ground_station=self  # Pass self for GPS access
            )
        
        # Initialize ground station GPS
        if self.config.gps_enabled and GPS_AVAILABLE:
            logger.info("Initializing ground station GPS...")
            self._gps = GPS(
                device=self.config.gps_device,
                baudrate=self.config.gps_baudrate,
                airborne_mode=False,  # Ground station
                simulate=self.simulate,
                callback=self._on_ground_gps_update
            )
            if self._gps.init():
                logger.info(f"Ground GPS initialized on {self.config.gps_device}")
            else:
                logger.warning("Ground GPS initialization failed")
                self._gps = None
        elif self.config.gps_enabled and not GPS_AVAILABLE:
            logger.warning("GPS enabled but GPS module not available")
        else:
            logger.info("Ground station GPS disabled")
        
        logger.info("All components initialized")
    
    def _start_components(self):
        """Start all components"""
        logger.info("Starting components...")
        
        # Start ground GPS
        if self._gps:
            self._gps.start()
            logger.info("Ground GPS started")
        
        # Start receiver
        if self.simulate:
            self._sim_receiver.start()
        else:
            if not self._receiver.start():
                raise RuntimeError("Failed to start receiver")
        
        # Start web server
        if self._web:
            self._web.start()
        
        logger.info("All components started")
    
    def _run_main_loop(self):
        """Main control loop"""
        logger.info("Entering main loop")
        
        # Status update interval
        status_interval = 10.0
        last_status = time.time()
        
        while self._running:
            try:
                # Wait with timeout
                self._shutdown_event.wait(timeout=1.0)
                
                if self._shutdown_event.is_set():
                    break
                
                # Periodic status update
                now = time.time()
                if now - last_status >= status_interval:
                    self._log_status()
                    last_status = now
                
                # Check decoder for timed-out images (get_status triggers internal cleanup)
                if self._decoder:
                    self._decoder.get_status()
                
            except Exception as e:
                logger.error(f"Main loop error: {e}")
        
        logger.info("Exiting main loop")
    
    def _cleanup(self):
        """Cleanup resources"""
        logger.info("Cleaning up...")
        
        if self._web:
            self._web.stop()
        
        if self._receiver:
            self._receiver.stop()
        
        if self._sim_receiver:
            self._sim_receiver.stop()
        
        if self._gps:
            self._gps.stop()
        
        if self._telemetry:
            self._telemetry.close()
        
        if self._storage:
            self._storage.close()
        
        logger.info("Cleanup complete")
    
    def _on_ground_gps_update(self, gps_data: GPSData):
        """Callback for ground station GPS updates"""
        with self._ground_gps_lock:
            self._ground_gps = gps_data
        if gps_data.position_valid:
            logger.debug(f"Ground GPS update: {gps_data.latitude:.6f}, {gps_data.longitude:.6f}")
    
    def get_ground_position(self) -> Optional[GPSData]:
        """Get current ground station GPS position"""
        with self._ground_gps_lock:
            return self._ground_gps
    
    def get_tracking_info(self) -> Optional[dict]:
        """
        Get tracking info (distance, bearing, elevation) to airborne unit
        
        Returns:
            Dict with distance_m, bearing_deg, elevation_deg or None if unavailable
        """
        # Get ground position
        with self._ground_gps_lock:
            ground = self._ground_gps
        
        if ground is None or not ground.position_valid:
            return None
        
        # Get latest airborne position from telemetry
        if self._telemetry is None:
            return None
        
        airborne = self._telemetry.get_latest()
        if airborne is None or airborne.latitude == 0:
            return None
        
        # Calculate tracking info
        distance = haversine_distance(
            ground.latitude, ground.longitude,
            airborne.latitude, airborne.longitude
        )
        
        bearing = calculate_bearing(
            ground.latitude, ground.longitude,
            airborne.latitude, airborne.longitude
        )
        
        elevation = calculate_elevation_angle(
            ground.latitude, ground.longitude, ground.altitude,
            airborne.latitude, airborne.longitude, airborne.altitude
        )
        
        return {
            'distance_m': distance,
            'distance_km': distance / 1000,
            'bearing_deg': bearing,
            'elevation_deg': elevation,
            'ground_lat': ground.latitude,
            'ground_lon': ground.longitude,
            'ground_alt': ground.altitude,
            'ground_sats': ground.satellites,
        }
    
    def _on_telemetry(self, point: TelemetryPoint):
        """Callback for new telemetry"""
        # Push to web clients
        if self._web:
            self._web.emit_telemetry(point.to_dict())
    
    def _on_alert(self, alert_type: str, message: str, data):
        """Callback for alerts"""
        logger.warning(f"ALERT [{alert_type}]: {message}")
        
        if self._web:
            self._web.emit_alert(alert_type, message, data)
    
    def _on_image_complete(self, image_id: int, image_data: bytes, metadata: ImageMetadata):
        """Callback for completed image"""
        logger.info(f"Image {image_id} complete: {len(image_data)} bytes")
        
        # Store image
        if self._storage:
            stored = self._storage.store_image(image_id, image_data, metadata)
            
            if stored and self._web:
                self._web.emit_image_complete(image_id, {
                    'session_id': self._storage.session_id,
                    'width': metadata.width,
                    'height': metadata.height,
                    'size': len(image_data),
                })
    
    def _on_packet(self, packet_type, sequence, payload, rssi):
        """
        Callback for all received packets.
        
        This is an extension hook for custom packet processing.
        The main packet handling is done by dedicated handlers in receiver.py.
        Override this method in a subclass to add custom behavior.
        """
        # Extension point - no default action needed
    
    def _on_text_message(self, message: str):
        """Callback for text messages"""
        logger.info(f"Text message: {message}")
    
    def _log_status(self):
        """Log current status"""
        uptime = time.time() - self._start_time
        
        stats = []
        
        if self._receiver:
            rx_stats = self._receiver.get_stats()
            stats.append(f"RX:{rx_stats['packets_valid']}/{rx_stats['packets_received']}")
            stats.append(f"RSSI:{rx_stats['last_rssi']}")
        
        if self._telemetry:
            latest = self._telemetry.get_latest()
            if latest:
                stats.append(f"Alt:{latest.altitude:.0f}m")
                stats.append(f"Sats:{latest.satellites}")
        
        if self._decoder:
            dec_stats = self._decoder.get_status()
            stats.append(f"Imgs:{dec_stats['completed_images']}")
        
        logger.info(f"Status [{uptime:.0f}s] " + " | ".join(stats))
    
    def _get_status(self) -> dict:
        """Get current status as dictionary"""
        return {
            'time': time.time(),
            'uptime': time.time() - self._start_time,
            'receiver': self._receiver.get_stats() if self._receiver else {},
            'telemetry': self._telemetry.get_flight_stats() if self._telemetry else {},
            'decoder': self._decoder.get_status() if self._decoder else {},
            'storage': self._storage.get_storage_stats() if self._storage else {},
        }
    
    def request_shutdown(self):
        """Request graceful shutdown"""
        logger.info("Shutdown requested")
        self._running = False
        self._shutdown_event.set()


def signal_handler(signum, frame, station: GroundStation):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}")
    station.request_shutdown()


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="RaptorHab Ground Station (Receive-Only)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Enable simulation mode (no real radio hardware)"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to configuration file"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level"
    )
    parser.add_argument(
        "--callsign",
        type=str,
        default=None,
        help="Override station callsign"
    )
    parser.add_argument(
        "--frequency",
        type=float,
        default=None,
        help="Override frequency (MHz)"
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=None,
        help="Override web interface port"
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Disable web interface"
    )
    parser.add_argument(
        "--data-path",
        type=str,
        default=None,
        help="Override data storage path"
    )
    
    args = parser.parse_args()
    
    # Load configuration
    config = GroundConfig.from_env()
    
    # Apply command line overrides
    if args.callsign:
        config.callsign = args.callsign
    if args.frequency:
        config.frequency_mhz = args.frequency
    if args.web_port:
        config.web_port = args.web_port
    if args.no_web:
        config.enable_web = False
    if args.data_path:
        config.data_path = args.data_path
        config.image_path = os.path.join(args.data_path, "images")
        config.log_path = os.path.join(args.data_path, "logs")
        config.telemetry_db_path = os.path.join(args.data_path, "telemetry.db")
    if args.simulate:
        config.simulate_radio = True
    
    # Setup logging
    log_level = getattr(logging, args.log_level.upper())
    setup_logging(config.log_path, log_level, "raptorhab-ground")
    
    # Create ground station
    station = GroundStation(config, simulate=args.simulate)
    
    # Setup signal handlers
    signal.signal(
        signal.SIGTERM,
        lambda s, f: signal_handler(s, f, station)
    )
    signal.signal(
        signal.SIGINT,
        lambda s, f: signal_handler(s, f, station)
    )
    
    # Start
    station.start()


if __name__ == "__main__":
    main()
