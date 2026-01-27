"""
RaptorHab Ground Station Configuration
All configurable parameters for the ground station
"""

import os
from dataclasses import dataclass, field
from typing import Tuple, Optional


@dataclass
class GroundConfig:
    """Ground station configuration"""
    
    # === Identification ===
    callsign: str = "RPGND1"
    station_id: int = 1
    
    # === Radio Configuration ===
    frequency_mhz: float = 915.0
    tx_power_dbm: int = 22
    bitrate_bps: int = 96000
    fdev_hz: int = 50000
    
    # === SX1262 Pin Configuration (BCM numbering) ===
    # Same as airborne - adjust for your hardware
    pin_cs: int = 21
    pin_clk: int = 11
    pin_mosi: int = 10
    pin_miso: int = 9
    pin_busy: int = 20
    pin_dio1: int = 16
    pin_txen: int = 6
    pin_rst: int = 18
    
    # === Timing ===
    rx_timeout_ms: int = 100  # Receive check interval
    command_retry_count: int = 3
    command_retry_delay_sec: float = 2.0
    ack_timeout_sec: float = 5.0
    
    # === Fountain Code Decoder ===
    fountain_symbol_size: int = 200
    max_pending_images: int = 10
    image_timeout_sec: float = 300.0  # Abandon incomplete images after 5 min
    
    # === Storage ===
    data_path: str = "/RaptorQHAB/ground/data"
    image_path: str = "/RaptorQHAB/ground/images"
    log_path: str = "/RaptorQHAB/ground/logs"
    telemetry_db_path: str = "/RaptorQHAB/ground/telemetry.db"
    max_stored_images: int = 20000
    
    # === Web Interface ===
    web_host: str = "0.0.0.0"
    web_port: int = 5000
    web_debug: bool = False
    enable_web: bool = True
    
    # === Map Configuration ===
    map_default_lat: float = 40.0
    map_default_lon: float = -74.0
    map_default_zoom: int = 8
    
    # === Ground Station GPS ===
    # L76K GPS on Pi hardware UART (GPIO 14=TX, GPIO 15=RX)
    # Enables distance/bearing calculations to airborne unit
    gps_enabled: bool = True
    gps_device: str = "/dev/serial0"
    gps_device_alt: str = "/dev/ttyAMA0"
    gps_baudrate: int = 9600
    
    # === Alerts ===
    alert_low_battery_mv: int = 3300
    alert_high_altitude_m: float = 30000
    alert_descent_rate_mps: float = 10.0
    enable_audio_alerts: bool = False
    
    # === Debug ===
    debug_mode: bool = False
    simulate_radio: bool = False
    log_raw_packets: bool = True
    
    def __post_init__(self):
        """Create necessary directories"""
        os.makedirs(self.data_path, exist_ok=True)
        os.makedirs(self.image_path, exist_ok=True)
        os.makedirs(self.log_path, exist_ok=True)
    
    @classmethod
    def from_env(cls) -> 'GroundConfig':
        """
        Create config from environment variables
        
        Environment variables override defaults:
        - RAPTORHAB_GND_CALLSIGN
        - RAPTORHAB_GND_FREQUENCY
        - RAPTORHAB_GND_DATA_PATH
        - RAPTORHAB_GND_WEB_PORT
        - RAPTORHAB_GND_DEBUG
        etc.
        """
        config = cls()
        
        if os.getenv('RAPTORHAB_GND_CALLSIGN'):
            config.callsign = os.getenv('RAPTORHAB_GND_CALLSIGN')
        
        if os.getenv('RAPTORHAB_GND_FREQUENCY'):
            config.frequency_mhz = float(os.getenv('RAPTORHAB_GND_FREQUENCY'))
        
        if os.getenv('RAPTORHAB_GND_DATA_PATH'):
            config.data_path = os.getenv('RAPTORHAB_GND_DATA_PATH')
        
        if os.getenv('RAPTORHAB_GND_IMAGE_PATH'):
            config.image_path = os.getenv('RAPTORHAB_GND_IMAGE_PATH')
        
        if os.getenv('RAPTORHAB_GND_LOG_PATH'):
            config.log_path = os.getenv('RAPTORHAB_GND_LOG_PATH')
        
        if os.getenv('RAPTORHAB_GND_WEB_PORT'):
            config.web_port = int(os.getenv('RAPTORHAB_GND_WEB_PORT'))
        
        if os.getenv('RAPTORHAB_GND_DEBUG'):
            config.debug_mode = os.getenv('RAPTORHAB_GND_DEBUG').lower() in ('1', 'true', 'yes')
        
        if os.getenv('RAPTORHAB_GND_SIMULATE'):
            config.simulate_radio = os.getenv('RAPTORHAB_GND_SIMULATE').lower() in ('1', 'true', 'yes')
        
        if os.getenv('RAPTORHAB_GND_GPS_ENABLED'):
            config.gps_enabled = os.getenv('RAPTORHAB_GND_GPS_ENABLED').lower() in ('1', 'true', 'yes')
        
        if os.getenv('RAPTORHAB_GND_GPS_DEVICE'):
            config.gps_device = os.getenv('RAPTORHAB_GND_GPS_DEVICE')
        
        return config


# Default configuration instance
DEFAULT_CONFIG = GroundConfig()
