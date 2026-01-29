"""
SondeHub Amateur API integration.
Uploads telemetry data to the SondeHub tracking network.
"""

import json
import requests
from datetime import datetime, timezone
from typing import Optional, Callable
from threading import Thread

# Try to import PyQt6, but work without it for web version
try:
    from PyQt6.QtCore import QObject, pyqtSignal
    HAS_PYQT = True
except ImportError:
    HAS_PYQT = False
    QObject = object  # Fallback base class

from .telemetry import TelemetryPoint
from .config import SondeHubConfig


class SondeHubManager(QObject if HAS_PYQT else object):
    """
    Manages uploads to SondeHub Amateur.
    
    Works with or without PyQt6. When PyQt6 is available, uses Qt signals.
    Otherwise uses callback functions.
    """
    
    TELEMETRY_URL = "https://api.v2.sondehub.org/amateur/telemetry"
    LISTENERS_URL = "https://api.v2.sondehub.org/amateur/listeners"
    
    # Qt signals (only created when PyQt6 is available)
    if HAS_PYQT:
        upload_success = pyqtSignal()
        upload_error = pyqtSignal(str)
        status_updated = pyqtSignal(str)
    
    def __init__(self, parent=None):
        if HAS_PYQT:
            super().__init__(parent)
        
        self.config = SondeHubConfig()
        
        # Statistics
        self.upload_count = 0
        self.error_count = 0
        self.last_upload_time: Optional[datetime] = None
        self.last_status = ""
        
        # Rate limiting
        self._last_telemetry_upload = datetime.min
        self._min_upload_interval = 2.0  # seconds
        
        # Ground station position
        self.ground_station_lat: Optional[float] = None
        self.ground_station_lon: Optional[float] = None
        self.ground_station_alt: Optional[float] = None
        
        # Callbacks for non-Qt mode
        self.on_upload_success: Optional[Callable] = None
        self.on_upload_error: Optional[Callable] = None
        self.on_status_updated: Optional[Callable] = None
    
    def set_config(self, config: SondeHubConfig):
        """Update configuration."""
        self.config = config
    
    def set_ground_station_position(self, lat: float, lon: float, alt: float):
        """Set ground station position for listener uploads."""
        self.ground_station_lat = lat
        self.ground_station_lon = lon
        self.ground_station_alt = alt
    
    def upload_telemetry(self, telemetry: TelemetryPoint, rssi: float = 0, snr: float = 0):
        """
        Upload telemetry to SondeHub.
        
        Runs in background thread to avoid blocking.
        """
        if not self.config.enabled or not self.config.is_valid:
            return
        
        if not self.config.upload_telemetry:
            return
        
        # Rate limiting
        now = datetime.now()
        elapsed = (now - self._last_telemetry_upload).total_seconds()
        if elapsed < self._min_upload_interval:
            return
        self._last_telemetry_upload = now
        
        # Skip invalid positions
        if telemetry.satellites == 0:
            return
        
        # Run upload in background
        thread = Thread(
            target=self._do_upload_telemetry,
            args=(telemetry, rssi, snr),
            daemon=True
        )
        thread.start()
    
    def _do_upload_telemetry(self, telemetry: TelemetryPoint, rssi: float, snr: float):
        """Perform the actual upload (runs in background thread)."""
        try:
            # Build payload
            payload = {
                "software_name": "RaptorHabGS",
                "software_version": "1.0.0",
                "uploader_callsign": self.config.uploader_callsign,
                "uploader_antenna": self.config.uploader_antenna,
                "time_received": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "payload_callsign": self.config.payload_callsign,
                "datetime": telemetry.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lat": telemetry.latitude,
                "lon": telemetry.longitude,
                "alt": telemetry.altitude,
                "sats": telemetry.satellites,
            }
            
            # Optional fields
            if telemetry.speed > 0:
                payload["speed"] = telemetry.speed
            if telemetry.heading > 0:
                payload["heading"] = telemetry.heading
            if telemetry.battery_mv > 0:
                payload["batt"] = telemetry.battery_voltage
            if telemetry.cpu_temp > -40:
                payload["temp"] = telemetry.cpu_temp
            if rssi != 0:
                payload["rssi"] = rssi
            if snr != 0:
                payload["snr"] = snr
            
            # Uploader position
            if self.ground_station_lat is not None:
                payload["uploader_position"] = [
                    self.ground_station_lat,
                    self.ground_station_lon,
                    self.ground_station_alt or 0
                ]
            
            # Send request
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "RaptorHabGS-1.0",
                "Date": self._format_rfc2822_date()
            }
            
            response = requests.put(
                self.TELEMETRY_URL,
                json=[payload],  # API expects array
                headers=headers,
                timeout=20
            )
            
            if response.status_code == 200:
                self.upload_count += 1
                self.last_upload_time = datetime.now()
                self.last_status = f"OK ({self.upload_count} uploads)"
                self._emit_success()
                self._emit_status(self.last_status)
            else:
                self.error_count += 1
                error_msg = f"HTTP {response.status_code}: {response.text[:100]}"
                self.last_status = error_msg
                self._emit_error(error_msg)
                self._emit_status(self.last_status)
                
        except Exception as e:
            self.error_count += 1
            error_msg = str(e)[:100]
            self.last_status = f"Error: {error_msg}"
            self._emit_error(error_msg)
            self._emit_status(self.last_status)
    
    def _emit_success(self):
        """Emit upload success via signal or callback."""
        if HAS_PYQT:
            self.upload_success.emit()
        elif self.on_upload_success:
            self.on_upload_success()
    
    def _emit_error(self, message: str):
        """Emit error via signal or callback."""
        if HAS_PYQT:
            self.upload_error.emit(message)
        elif self.on_upload_error:
            self.on_upload_error(message)
    
    def _emit_status(self, message: str):
        """Emit status update via signal or callback."""
        if HAS_PYQT:
            self.status_updated.emit(message)
        elif self.on_status_updated:
            self.on_status_updated(message)
    
    def upload_station_position(self):
        """Upload listener/station position to SondeHub."""
        if not self.config.enabled or not self.config.is_valid:
            return
        
        if not self.config.upload_position:
            return
        
        if self.ground_station_lat is None:
            return
        
        thread = Thread(target=self._do_upload_position, daemon=True)
        thread.start()
    
    def _do_upload_position(self):
        """Upload station position (runs in background)."""
        try:
            payload = {
                "software_name": "RaptorHabGS",
                "software_version": "1.0.0",
                "uploader_callsign": self.config.uploader_callsign,
                "uploader_antenna": self.config.uploader_antenna,
                "uploader_position": [
                    self.ground_station_lat,
                    self.ground_station_lon,
                    self.ground_station_alt or 0
                ]
            }
            
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "RaptorHabGS-1.0",
                "Date": self._format_rfc2822_date()
            }
            
            requests.put(
                self.LISTENERS_URL,
                json=payload,
                headers=headers,
                timeout=20
            )
            
        except Exception:
            pass  # Fire and forget for position updates
    
    @staticmethod
    def _format_rfc2822_date() -> str:
        """Format current time as RFC 2822 date string."""
        now = datetime.now(timezone.utc)
        return now.strftime("%a, %d %b %Y %H:%M:%S +0000")
