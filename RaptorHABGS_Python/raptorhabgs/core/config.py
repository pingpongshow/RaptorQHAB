"""
Configuration and settings management.
"""

import json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
import platform


def get_data_directory() -> Path:
    """Get the application data directory based on platform."""
    system = platform.system()
    
    if system == "Windows":
        base = Path.home() / "Documents" / "RaptorHabGS"
    elif system == "Darwin":  # macOS
        base = Path.home() / "Documents" / "RaptorHabGS"
    else:  # Linux
        base = Path.home() / ".local" / "share" / "RaptorHabGS"
    
    return base


def get_config_path() -> Path:
    """Get the configuration file path."""
    return get_data_directory() / "config.json"


@dataclass
class ModemConfig:
    """RF modem configuration for Heltec SX1262."""
    frequency_mhz: float = 915.0
    bitrate_kbps: float = 96.0     # FSK bit rate in kbps
    deviation_khz: float = 50.0    # FSK frequency deviation
    bandwidth_khz: float = 467.0   # RX bandwidth in kHz
    preamble_bits: int = 32        # Preamble length in bits
    
    @property
    def config_command(self) -> str:
        """Generate modem configuration command string matching firmware format."""
        # Format: CFG:<freq>,<bitrate>,<deviation>,<bandwidth>,<preamble>\n
        return f"CFG:{self.frequency_mhz:.1f},{self.bitrate_kbps:.1f},{self.deviation_khz:.1f},{self.bandwidth_khz:.1f},{self.preamble_bits}\n"


@dataclass
class SondeHubConfig:
    """SondeHub upload configuration."""
    enabled: bool = False
    uploader_callsign: str = ""
    uploader_antenna: str = "Ground Station"
    payload_callsign: str = ""
    upload_telemetry: bool = True
    upload_position: bool = True
    upload_images: bool = False
    
    @property
    def is_valid(self) -> bool:
        return bool(self.uploader_callsign and self.payload_callsign)


@dataclass
class GPSConfig:
    """GPS receiver configuration."""
    port: str = ""
    baud_rate: int = 9600
    enabled: bool = True


@dataclass
class PredictionConfig:
    """Landing prediction configuration."""
    enabled: bool = True
    burst_altitude: float = 30000.0  # meters
    ascent_rate: float = 5.0  # m/s
    descent_rate: float = 5.0  # m/s (at sea level)
    use_auto_wind: bool = True
    wind_speed: float = 10.0  # m/s
    wind_direction: float = 270.0  # degrees (from)


@dataclass 
class AppConfig:
    """Main application configuration."""
    # Serial port for radio modem
    serial_port: str = ""
    serial_baud: int = 921600  # Must match Heltec modem firmware
    
    # Modem settings
    modem: ModemConfig = field(default_factory=ModemConfig)
    
    # SondeHub settings
    sondehub: SondeHubConfig = field(default_factory=SondeHubConfig)
    
    # GPS settings
    gps: GPSConfig = field(default_factory=GPSConfig)
    
    # Prediction settings
    prediction: PredictionConfig = field(default_factory=PredictionConfig)
    
    # Mission recording
    auto_record: bool = True
    missions_folder: str = ""
    
    # UI preferences
    map_style: str = "openstreetmap"
    show_track: bool = True
    show_prediction: bool = True
    
    def save(self):
        """Save configuration to file."""
        config_path = get_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Convert to dict, handling nested dataclasses
        data = {
            "serial_port": self.serial_port,
            "serial_baud": self.serial_baud,
            "modem": asdict(self.modem),
            "sondehub": asdict(self.sondehub),
            "gps": asdict(self.gps),
            "prediction": asdict(self.prediction),
            "auto_record": self.auto_record,
            "missions_folder": self.missions_folder,
            "map_style": self.map_style,
            "show_track": self.show_track,
            "show_prediction": self.show_prediction,
        }
        
        with open(config_path, "w") as f:
            json.dump(data, f, indent=2)
    
    @classmethod
    def load(cls) -> "AppConfig":
        """Load configuration from file."""
        config_path = get_config_path()
        
        if not config_path.exists():
            return cls()
        
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            
            config = cls()
            config.serial_port = data.get("serial_port", "")
            config.serial_baud = data.get("serial_baud", 921600)
            config.auto_record = data.get("auto_record", True)
            config.missions_folder = data.get("missions_folder", "")
            config.map_style = data.get("map_style", "openstreetmap")
            config.show_track = data.get("show_track", True)
            config.show_prediction = data.get("show_prediction", True)
            
            if "modem" in data:
                modem_data = data["modem"]
                # Handle both old and new field names for backwards compatibility
                config.modem = ModemConfig(
                    frequency_mhz=modem_data.get("frequency_mhz", 915.0),
                    bitrate_kbps=modem_data.get("bitrate_kbps", 96.0),
                    deviation_khz=modem_data.get("deviation_khz", 50.0),
                    bandwidth_khz=modem_data.get("bandwidth_khz", 467.0),
                    preamble_bits=modem_data.get("preamble_bits", 
                                                  modem_data.get("preamble_length", 32)),
                )
            if "sondehub" in data:
                config.sondehub = SondeHubConfig(**data["sondehub"])
            if "gps" in data:
                config.gps = GPSConfig(**data["gps"])
            if "prediction" in data:
                config.prediction = PredictionConfig(**data["prediction"])
            
            return config
            
        except Exception as e:
            print(f"Error loading config: {e}")
            return cls()


# Global config instance
_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = AppConfig.load()
    return _config


def save_config():
    """Save the global configuration."""
    global _config
    if _config is not None:
        _config.save()
