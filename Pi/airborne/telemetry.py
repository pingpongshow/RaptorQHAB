"""
RaptorHab Telemetry Module
Sensor data collection and telemetry packet assembly
"""

import time
import logging
from dataclasses import dataclass
from typing import Optional

from common.constants import FixType
from common.protocol import TelemetryPayload
from common.gps import GPSData

logger = logging.getLogger(__name__)


@dataclass
class TelemetryData:
    """Complete telemetry data from all sensors"""
    # GPS data
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    speed: float = 0.0
    heading: float = 0.0
    satellites: int = 0
    fix_type: FixType = FixType.NONE
    gps_time: int = 0
    
    # System status
    battery_mv: int = 0
    cpu_temp: float = 0.0
    radio_temp: float = 0.0
    
    # Transmission status
    image_id: int = 0
    image_progress: int = 0
    rssi: int = 0
    
    # Metadata
    timestamp: float = 0.0
    
    def to_payload(self) -> TelemetryPayload:
        """Convert to telemetry packet payload"""
        return TelemetryPayload(
            latitude=self.latitude,
            longitude=self.longitude,
            altitude=self.altitude,
            speed=self.speed,
            heading=self.heading,
            satellites=self.satellites,
            fix_type=self.fix_type,
            gps_time=self.gps_time,
            battery_mv=self.battery_mv,
            cpu_temp=self.cpu_temp,
            radio_temp=self.radio_temp,
            image_id=self.image_id,
            image_progress=self.image_progress,
            rssi=self.rssi
        )


class TelemetryCollector:
    """Collects telemetry data from all sensors"""
    
    def __init__(self):
        """Initialize telemetry collector"""
        self._last_gps_data: Optional[GPSData] = None
        self._battery_mv: int = 0
        self._cpu_temp: float = 0.0
        self._radio_temp: float = 0.0
        self._image_id: int = 0
        self._image_progress: int = 0
        self._rssi: int = 0
    
    def update_gps(self, gps_data: GPSData):
        """Update GPS data"""
        self._last_gps_data = gps_data
    
    def update_radio_temp(self, temp: float):
        """Update radio temperature"""
        self._radio_temp = temp
    
    def update_image_status(self, image_id: int, progress: int):
        """
        Update current image transmission status
        
        Args:
            image_id: Current image ID
            progress: Transmission progress (0-100%)
        """
        self._image_id = image_id
        self._image_progress = progress
    
    def update_rssi(self, rssi: int):
        """Update last received RSSI"""
        self._rssi = rssi
    
    def update_system(self, battery_mv: int, cpu_temp: float, radio_temp: float):
        """
        Update system sensor data
        
        Args:
            battery_mv: Battery voltage in millivolts
            cpu_temp: CPU temperature in Celsius
            radio_temp: Radio temperature in Celsius
        """
        self._battery_mv = battery_mv
        self._cpu_temp = cpu_temp
        self._radio_temp = radio_temp
    
    def get_payload_bytes(self) -> bytes:
        """
        Get telemetry as serialized payload bytes
        
        Returns:
            Serialized telemetry payload
        """
        return self.collect_payload().serialize()
    
    def collect(self) -> TelemetryData:
        """
        Collect current telemetry from all sources
        
        Returns:
            TelemetryData with current values
        """
        telemetry = TelemetryData(timestamp=time.time())
        
        # GPS data
        if self._last_gps_data:
            telemetry.latitude = self._last_gps_data.latitude
            telemetry.longitude = self._last_gps_data.longitude
            telemetry.altitude = self._last_gps_data.altitude
            telemetry.speed = self._last_gps_data.speed
            telemetry.heading = self._last_gps_data.heading
            telemetry.satellites = self._last_gps_data.satellites
            telemetry.fix_type = self._last_gps_data.fix_type
            telemetry.gps_time = self._last_gps_data.time_utc
        
        # System sensors
        telemetry.battery_mv = self._battery_mv
        telemetry.cpu_temp = self._cpu_temp
        telemetry.radio_temp = self._radio_temp
        
        # Transmission status
        telemetry.image_id = self._image_id
        telemetry.image_progress = self._image_progress
        telemetry.rssi = self._rssi
        
        return telemetry
    
    def collect_payload(self) -> TelemetryPayload:
        """
        Collect telemetry and return as packet payload
        
        Returns:
            TelemetryPayload ready for transmission
        """
        return self.collect().to_payload()


class TelemetryLogger:
    """Logs telemetry data to file for post-flight analysis"""
    
    def __init__(self, log_path: str, callsign: str = "RAPTOR"):
        """
        Initialize telemetry logger
        
        Args:
            log_path: Directory for telemetry logs
            callsign: Station callsign for log filename
        """
        import os
        from datetime import datetime
        
        os.makedirs(log_path, exist_ok=True)
        self.callsign = callsign
        
        # Create log file with timestamp and callsign
        filename = datetime.now().strftime(f"telemetry_{callsign}_%Y%m%d_%H%M%S.csv")
        self.filepath = os.path.join(log_path, filename)
        
        # Write header
        self._write_header()
        
        logger.info(f"Telemetry logging to {self.filepath}")
    
    def _write_header(self):
        """Write CSV header"""
        headers = [
            "timestamp", "gps_time",
            "latitude", "longitude", "altitude",
            "speed", "heading", "satellites", "fix_type",
            "battery_mv", "cpu_temp", "radio_temp",
            "image_id", "image_progress", "rssi"
        ]
        
        with open(self.filepath, 'w') as f:
            f.write(','.join(headers) + '\n')
    
    def log(
        self,
        telemetry: TelemetryData = None,
        timestamp: float = None,
        latitude: float = 0.0,
        longitude: float = 0.0,
        altitude: float = 0.0,
        speed: float = 0.0,
        heading: float = 0.0,
        satellites: int = 0,
        fix_type = None,
        gps_time: int = 0,
        battery_mv: int = 0,
        cpu_temp: float = 0.0,
        radio_temp: float = 0.0,
        image_id: int = 0,
        image_progress: int = 0,
        rssi: int = 0
    ):
        """
        Log telemetry data point
        
        Args:
            telemetry: TelemetryData object (if provided, other args ignored)
            Or individual fields can be passed as keyword args
        """
        import time as time_module
        
        if telemetry is not None:
            # Use TelemetryData object directly
            values = [
                f"{telemetry.timestamp:.3f}",
                str(telemetry.gps_time),
                f"{telemetry.latitude:.7f}",
                f"{telemetry.longitude:.7f}",
                f"{telemetry.altitude:.1f}",
                f"{telemetry.speed:.2f}",
                f"{telemetry.heading:.1f}",
                str(telemetry.satellites),
                str(telemetry.fix_type.value if hasattr(telemetry.fix_type, 'value') else telemetry.fix_type),
                str(telemetry.battery_mv),
                f"{telemetry.cpu_temp:.1f}",
                f"{telemetry.radio_temp:.1f}",
                str(telemetry.image_id),
                str(telemetry.image_progress),
                str(telemetry.rssi)
            ]
        else:
            # Use individual fields
            ts = timestamp if timestamp is not None else time_module.time()
            fix_val = fix_type.value if hasattr(fix_type, 'value') else (fix_type if fix_type is not None else 0)
            values = [
                f"{ts:.3f}",
                str(gps_time),
                f"{latitude:.7f}",
                f"{longitude:.7f}",
                f"{altitude:.1f}",
                f"{speed:.2f}",
                f"{heading:.1f}",
                str(satellites),
                str(fix_val),
                str(battery_mv),
                f"{cpu_temp:.1f}",
                f"{radio_temp:.1f}",
                str(image_id),
                str(image_progress),
                str(rssi)
            ]
        
        try:
            with open(self.filepath, 'a') as f:
                f.write(','.join(values) + '\n')
        except IOError as e:
            logger.error(f"Failed to log telemetry: {e}")
    
    def close(self):
        """Close the telemetry logger (flush any pending data)"""
        logger.info(f"Telemetry logger closed: {self.filepath}")
