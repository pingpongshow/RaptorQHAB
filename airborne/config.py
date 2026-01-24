"""
RaptorHab Airborne Configuration
All configurable parameters for the airborne payload

Note: This is a transmit-only payload. All configuration is done
through this file or environment variables.
"""

import os
from dataclasses import dataclass, field
from typing import Tuple


@dataclass
class Config:
    """Airborne payload configuration"""
    
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
    tx_period_sec: int = 3          # Duration of each TX burst
    tx_pause_sec: int = 10            # Pause between TX bursts (0 = continuous TX)
    telemetry_interval_packets: int = 5
    image_meta_interval_packets: int = 100
    
    # === Camera Configuration ===
    camera_resolution: Tuple[int, int] = (1280, 960)
    camera_burst_count: int = 5
    webp_quality: int = 75
    image_overlay_enabled: bool = True
    
    # Camera image adjustments (0-200 scale, 100 = neutral/normal)
    camera_brightness: int = 100     # 0=dark, 100=normal, 200=bright
    camera_contrast: int = 100       # 0=low, 100=normal, 200=high
    camera_saturation: int = 100     # 0=grayscale, 100=normal, 200=vivid
    camera_sharpness: int = 100      # 0=soft, 100=normal, 200=sharp
    camera_exposure_comp: int = 100  # 0=-2EV, 100=0EV, 200=+2EV
    camera_awb_mode: int = 0         # 0=auto, 1=daylight, 2=cloudy, 3=tungsten, 4=fluorescent, 5=indoor
    
    # Color gain adjustments (to fix red/pink tint)
    # Range: 50-200, where 100 = no adjustment
    # To fix red tint: reduce red_gain (e.g., 80) and/or increase blue_gain (e.g., 120)
    camera_red_gain: int = 100       # Red channel gain (lower = less red)
    camera_blue_gain: int = 100      # Blue channel gain (higher = more blue)
    
    # === GPS Configuration ===
    # L76K GPS on Pi hardware UART (GPIO 14=TX, GPIO 15=RX)
    # Use /dev/serial0 which symlinks to the correct UART device
    gps_device: str = "/dev/serial0"
    gps_device_alt: str = "/dev/ttyAMA0"  # Alternative if serial0 not available
    gps_baudrate: int = 9600
    gps_airborne_mode: bool = True  # Set uBlox airborne dynamic model (L76K ignores this)
    
    # === Fountain Code Configuration ===
    fountain_symbol_size: int = 200
    fountain_overhead_percent: int = 25
    
    # === Storage ===
    image_storage_path: str = "/RaptorQHAB/airborne/images"
    log_path: str = "/RaptorQHAB/airborne/logs"
    max_stored_images: int = 10000
    
    # === Operational ===
    auto_capture_interval_sec: int = 120  # Auto capture image every 120 seconds
    watchdog_enabled: bool = True
    watchdog_timeout_sec: int = 60
    reboot_on_fatal_error: bool = True
    
    # === Debug ===
    debug_mode: bool = False
    simulate_gps: bool = False
    simulate_camera: bool = False
    
    def __post_init__(self):
        """Create necessary directories"""
        os.makedirs(self.image_storage_path, exist_ok=True)
        os.makedirs(self.log_path, exist_ok=True)
    
    # Property aliases for cleaner access in main.py
    @property
    def frequency_mhz(self) -> float:
        return self.radio_frequency_mhz
    
    @property
    def tx_power_dbm(self) -> int:
        return self.radio_power_dbm
    
    @tx_power_dbm.setter
    def tx_power_dbm(self, value: int):
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
    def capture_interval_sec(self, value: int):
        self.auto_capture_interval_sec = value
    
    @classmethod
    def from_env(cls) -> 'Config':
        """
        Create config from environment variables
        
        Environment variables override defaults:
        - RAPTORHAB_CALLSIGN
        - RAPTORHAB_FREQUENCY
        - RAPTORHAB_TX_POWER
        - RAPTORHAB_GPS_DEVICE
        - RAPTORHAB_DEBUG
        - RAPTORHAB_TX_PAUSE (seconds of pause between TX bursts)
        - RAPTORHAB_CAPTURE_INTERVAL (seconds between auto captures)
        - RAPTORHAB_CAMERA_BRIGHTNESS (0-200)
        - RAPTORHAB_CAMERA_CONTRAST (0-200)
        - RAPTORHAB_CAMERA_SATURATION (0-200)
        - RAPTORHAB_CAMERA_SHARPNESS (0-200)
        - RAPTORHAB_CAMERA_EXPOSURE (0-200)
        - RAPTORHAB_CAMERA_AWB (0-5)
        - RAPTORHAB_WEBP_QUALITY (0-100)
        etc.
        """
        config = cls()
        
        # Read environment overrides
        if os.getenv('RAPTORHAB_CALLSIGN'):
            config.callsign = os.getenv('RAPTORHAB_CALLSIGN')
        
        if os.getenv('RAPTORHAB_FREQUENCY'):
            config.radio_frequency_mhz = float(os.getenv('RAPTORHAB_FREQUENCY'))
        
        if os.getenv('RAPTORHAB_TX_POWER'):
            config.radio_power_dbm = int(os.getenv('RAPTORHAB_TX_POWER'))
        
        if os.getenv('RAPTORHAB_GPS_DEVICE'):
            config.gps_device = os.getenv('RAPTORHAB_GPS_DEVICE')
        
        if os.getenv('RAPTORHAB_DEBUG'):
            config.debug_mode = os.getenv('RAPTORHAB_DEBUG').lower() in ('1', 'true', 'yes')
        
        if os.getenv('RAPTORHAB_SIMULATE_GPS'):
            config.simulate_gps = os.getenv('RAPTORHAB_SIMULATE_GPS').lower() in ('1', 'true', 'yes')
        
        if os.getenv('RAPTORHAB_SIMULATE_CAMERA'):
            config.simulate_camera = os.getenv('RAPTORHAB_SIMULATE_CAMERA').lower() in ('1', 'true', 'yes')
        
        if os.getenv('RAPTORHAB_IMAGE_PATH'):
            config.image_storage_path = os.getenv('RAPTORHAB_IMAGE_PATH')
        
        if os.getenv('RAPTORHAB_LOG_PATH'):
            config.log_path = os.getenv('RAPTORHAB_LOG_PATH')
        
        # TX timing
        if os.getenv('RAPTORHAB_TX_PERIOD'):
            config.tx_period_sec = int(os.getenv('RAPTORHAB_TX_PERIOD'))
        
        if os.getenv('RAPTORHAB_TX_PAUSE'):
            config.tx_pause_sec = int(os.getenv('RAPTORHAB_TX_PAUSE'))
        
        if os.getenv('RAPTORHAB_CAPTURE_INTERVAL'):
            config.auto_capture_interval_sec = int(os.getenv('RAPTORHAB_CAPTURE_INTERVAL'))
        
        # Camera settings
        if os.getenv('RAPTORHAB_CAMERA_BRIGHTNESS'):
            config.camera_brightness = max(0, min(200, int(os.getenv('RAPTORHAB_CAMERA_BRIGHTNESS'))))
        
        if os.getenv('RAPTORHAB_CAMERA_CONTRAST'):
            config.camera_contrast = max(0, min(200, int(os.getenv('RAPTORHAB_CAMERA_CONTRAST'))))
        
        if os.getenv('RAPTORHAB_CAMERA_SATURATION'):
            config.camera_saturation = max(0, min(200, int(os.getenv('RAPTORHAB_CAMERA_SATURATION'))))
        
        if os.getenv('RAPTORHAB_CAMERA_SHARPNESS'):
            config.camera_sharpness = max(0, min(200, int(os.getenv('RAPTORHAB_CAMERA_SHARPNESS'))))
        
        if os.getenv('RAPTORHAB_CAMERA_EXPOSURE'):
            config.camera_exposure_comp = max(0, min(200, int(os.getenv('RAPTORHAB_CAMERA_EXPOSURE'))))
        
        if os.getenv('RAPTORHAB_CAMERA_AWB'):
            config.camera_awb_mode = max(0, min(5, int(os.getenv('RAPTORHAB_CAMERA_AWB'))))
        
        if os.getenv('RAPTORHAB_CAMERA_RED_GAIN'):
            config.camera_red_gain = max(50, min(200, int(os.getenv('RAPTORHAB_CAMERA_RED_GAIN'))))
        
        if os.getenv('RAPTORHAB_CAMERA_BLUE_GAIN'):
            config.camera_blue_gain = max(50, min(200, int(os.getenv('RAPTORHAB_CAMERA_BLUE_GAIN'))))
        
        if os.getenv('RAPTORHAB_WEBP_QUALITY'):
            config.webp_quality = max(0, min(100, int(os.getenv('RAPTORHAB_WEBP_QUALITY'))))
        
        return config


# Default configuration instance
DEFAULT_CONFIG = Config()

# Alias for backward compatibility
AirborneConfig = Config
