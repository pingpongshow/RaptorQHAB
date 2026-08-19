"""
Telemetry data structures and serialization.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
from uuid import uuid4
import json


@dataclass
class TelemetryPoint:
    """Single telemetry data point from the balloon payload."""
    # Position
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0  # meters
    
    # Movement
    speed: float = 0.0  # m/s
    heading: float = 0.0  # degrees
    vertical_speed: float = 0.0  # m/s (calculated)
    
    # GPS info
    satellites: int = 0
    fix_type: int = 0
    hdop: float = 99.9
    
    # System status
    battery_mv: int = 0
    cpu_temp: float = 0.0
    radio_temp: float = 0.0
    rssi: int = 0
    
    # Image transfer status
    image_id: int = 0
    image_progress: float = 0.0
    
    # Metadata
    sequence: int = 0
    timestamp: datetime = field(default_factory=datetime.now)
    id: str = field(default_factory=lambda: str(uuid4()))
    
    # Signal quality from ground station
    rx_rssi: float = 0.0
    rx_snr: float = 0.0
    
    @property
    def is_valid(self) -> bool:
        """Check if this telemetry point has valid GPS data."""
        return (self.latitude != 0.0 or self.longitude != 0.0) and self.satellites > 0
    
    @property
    def battery_voltage(self) -> float:
        """Battery voltage in volts."""
        return self.battery_mv / 1000.0
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "sequence": self.sequence,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude": self.altitude,
            "speed": self.speed,
            "heading": self.heading,
            "vertical_speed": self.vertical_speed,
            "satellites": self.satellites,
            "fix_type": self.fix_type,
            "hdop": self.hdop,
            "battery_mv": self.battery_mv,
            "cpu_temp": self.cpu_temp,
            "radio_temp": self.radio_temp,
            "rssi": self.rssi,
            "image_id": self.image_id,
            "image_progress": self.image_progress,
            "rx_rssi": self.rx_rssi,
            "rx_snr": self.rx_snr,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "TelemetryPoint":
        """Create from dictionary."""
        point = cls()
        point.id = data.get("id", str(uuid4()))
        
        ts = data.get("timestamp")
        if isinstance(ts, str):
            point.timestamp = datetime.fromisoformat(ts)
        elif isinstance(ts, datetime):
            point.timestamp = ts
        
        point.sequence = data.get("sequence", 0)
        point.latitude = data.get("latitude", 0.0)
        point.longitude = data.get("longitude", 0.0)
        point.altitude = data.get("altitude", 0.0)
        point.speed = data.get("speed", 0.0)
        point.heading = data.get("heading", 0.0)
        point.vertical_speed = data.get("vertical_speed", 0.0)
        point.satellites = data.get("satellites", 0)
        point.fix_type = data.get("fix_type", 0)
        point.hdop = data.get("hdop", 99.9)
        point.battery_mv = data.get("battery_mv", 0)
        point.cpu_temp = data.get("cpu_temp", 0.0)
        point.radio_temp = data.get("radio_temp", 0.0)
        point.rssi = data.get("rssi", 0)
        point.image_id = data.get("image_id", 0)
        point.image_progress = data.get("image_progress", 0.0)
        point.rx_rssi = data.get("rx_rssi", 0.0)
        point.rx_snr = data.get("rx_snr", 0.0)
        
        return point


@dataclass
class GPSPosition:
    """Ground station GPS position."""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    satellites: int = 0
    fix_quality: int = 0
    hdop: float = 99.9
    timestamp: datetime = field(default_factory=datetime.now)
    
    @property
    def is_valid(self) -> bool:
        """Check if position is valid."""
        return (self.latitude != 0.0 or self.longitude != 0.0) and self.fix_quality > 0


@dataclass
class BearingDistance:
    """Bearing and distance calculation result."""
    bearing: float = 0.0  # degrees
    distance: float = 0.0  # meters
    elevation: float = 0.0  # degrees
    
    @property
    def distance_km(self) -> float:
        return self.distance / 1000.0
    
    @property
    def distance_miles(self) -> float:
        return self.distance / 1609.344
    
    @property
    def cardinal_direction(self) -> str:
        """Get cardinal direction from bearing."""
        directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                     "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        index = round(self.bearing / 22.5) % 16
        return directions[index]


@dataclass
class ImageMetadata:
    """Metadata for an image being received."""
    image_id: int
    total_size: int
    symbol_size: int
    num_source_symbols: int
    width: int
    height: int
    checksum: int
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class PendingImage:
    """Image in the process of being received."""
    image_id: int
    metadata: Optional[ImageMetadata] = None
    symbols: dict = field(default_factory=dict)  # symbol_id -> data
    last_received: datetime = field(default_factory=datetime.now)
    
    @property
    def progress(self) -> float:
        """Get reception progress as percentage."""
        if self.metadata is None or self.metadata.num_source_symbols == 0:
            return 0.0
        return min(100.0, len(self.symbols) / self.metadata.num_source_symbols * 100)
    
    @property
    def symbols_needed(self) -> int:
        """Get number of symbols still needed."""
        if self.metadata is None:
            return 0
        return max(0, self.metadata.num_source_symbols - len(self.symbols))


@dataclass
class LandingPrediction:
    """Landing site prediction."""
    latitude: float
    longitude: float
    time_to_landing: float  # seconds
    distance_to_landing: float  # meters
    bearing_to_landing: float  # degrees
    confidence: str = "low"  # low, medium, high
    descent_rate: float = 0.0
    phase: str = "unknown"  # prelaunch, ascending, descending, floating, landed
    timestamp: datetime = field(default_factory=datetime.now)
    used_wind_profile: bool = False


@dataclass
class Mission:
    """Recorded mission data."""
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    launch_time: Optional[datetime] = None
    landing_time: Optional[datetime] = None
    max_altitude: float = 0.0
    total_distance: float = 0.0
    burst_altitude: Optional[float] = None
    telemetry_count: int = 0
    image_count: int = 0
    notes: str = ""
    
    @property
    def folder_name(self) -> str:
        """Generate folder name for this mission."""
        date_str = self.created_at.strftime("%Y-%m-%d_%H-%M")
        safe_name = self.name.replace(" ", "_").replace("/", "-")
        return f"{date_str}_{safe_name}"
    
    @property
    def duration(self) -> Optional[float]:
        """Get mission duration in seconds."""
        if self.launch_time and self.landing_time:
            return (self.landing_time - self.launch_time).total_seconds()
        return None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "launch_time": self.launch_time.isoformat() if self.launch_time else None,
            "landing_time": self.landing_time.isoformat() if self.landing_time else None,
            "max_altitude": self.max_altitude,
            "total_distance": self.total_distance,
            "burst_altitude": self.burst_altitude,
            "telemetry_count": self.telemetry_count,
            "image_count": self.image_count,
            "notes": self.notes,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Mission":
        """Create from dictionary."""
        mission = cls()
        mission.id = data.get("id", str(uuid4()))
        mission.name = data.get("name", "")
        
        if data.get("created_at"):
            mission.created_at = datetime.fromisoformat(data["created_at"])
        if data.get("launch_time"):
            mission.launch_time = datetime.fromisoformat(data["launch_time"])
        if data.get("landing_time"):
            mission.landing_time = datetime.fromisoformat(data["landing_time"])
        
        mission.max_altitude = data.get("max_altitude", 0.0)
        mission.total_distance = data.get("total_distance", 0.0)
        mission.burst_altitude = data.get("burst_altitude")
        mission.telemetry_count = data.get("telemetry_count", 0)
        mission.image_count = data.get("image_count", 0)
        mission.notes = data.get("notes", "")
        
        return mission
