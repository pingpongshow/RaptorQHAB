"""
GPS Manager for ground station position.
Parses NMEA data from a serial GPS receiver.
"""

import serial
import serial.tools.list_ports
import math
from threading import Thread, Event
from typing import Optional, List
from datetime import datetime

from PyQt6.QtCore import QObject, pyqtSignal

from .telemetry import GPSPosition, BearingDistance


class GPSManager(QObject):
    """
    Manages GPS serial connection and NMEA parsing.
    
    Signals:
        position_updated: Emitted when GPS position is updated
        bearing_updated: Emitted when bearing to target is calculated
        connected: Emitted on successful connection
        disconnected: Emitted on disconnection
        error: Emitted on error
    """
    
    position_updated = pyqtSignal(object)  # GPSPosition
    bearing_updated = pyqtSignal(object)   # BearingDistance
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error = pyqtSignal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        self.serial: Optional[serial.Serial] = None
        self.is_connected = False
        
        self._read_thread: Optional[Thread] = None
        self._stop_event = Event()
        
        self._byte_buffer = bytearray()
        
        self.current_position: Optional[GPSPosition] = None
        self.current_bearing: Optional[BearingDistance] = None
        self._pending_position = GPSPosition()
        
        self.status_message = "Disconnected"
    
    @staticmethod
    def list_ports() -> List[str]:
        """List available serial ports."""
        ports = serial.tools.list_ports.comports()
        return [port.device for port in ports]
    
    def connect(self, port: str, baud_rate: int = 9600) -> bool:
        """Connect to GPS serial port."""
        if self.is_connected:
            self.disconnect()
        
        try:
            self.serial = serial.Serial(
                port=port,
                baudrate=baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
            )
            
            self.serial.reset_input_buffer()
            self.is_connected = True
            self._byte_buffer.clear()
            self.status_message = "Connected"
            
            # Start read thread
            self._stop_event.clear()
            self._read_thread = Thread(target=self._read_loop, daemon=True)
            self._read_thread.start()
            
            self.connected.emit()
            return True
            
        except Exception as e:
            self.error.emit(f"GPS connection failed: {e}")
            self.status_message = f"Error: {e}"
            return False
    
    def disconnect(self):
        """Disconnect from GPS."""
        if not self.is_connected:
            return
        
        self._stop_event.set()
        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=1.0)
        self._read_thread = None
        
        if self.serial:
            try:
                self.serial.close()
            except:
                pass
            self.serial = None
        
        self.is_connected = False
        self.status_message = "Disconnected"
        self.current_position = None
        
        self.disconnected.emit()
    
    def update_bearing(self, target_lat: float, target_lon: float, target_alt: float = 0):
        """Calculate bearing and distance to target."""
        if not self.current_position or not self.current_position.is_valid:
            return
        
        pos = self.current_position
        bearing = self._calculate_bearing(
            pos.latitude, pos.longitude,
            target_lat, target_lon
        )
        
        distance = self._haversine_distance(
            pos.latitude, pos.longitude,
            target_lat, target_lon
        )
        
        # Calculate elevation angle
        if distance > 0:
            alt_diff = target_alt - pos.altitude
            elevation = math.degrees(math.atan2(alt_diff, distance))
        else:
            elevation = 90.0 if target_alt > pos.altitude else 0.0
        
        self.current_bearing = BearingDistance(
            bearing=bearing,
            distance=distance,
            elevation=elevation
        )
        
        self.bearing_updated.emit(self.current_bearing)
    
    def _read_loop(self):
        """Background thread for reading GPS data."""
        while not self._stop_event.is_set() and self.serial:
            try:
                if self.serial.in_waiting > 0:
                    data = self.serial.read(self.serial.in_waiting)
                    if data:
                        self._process_bytes(data)
                else:
                    self._stop_event.wait(0.05)
                    
            except serial.SerialException as e:
                if not self._stop_event.is_set():
                    self.error.emit(f"GPS read error: {e}")
                break
            except Exception:
                pass
    
    def _process_bytes(self, data: bytes):
        """Process received bytes and extract NMEA sentences."""
        self._byte_buffer.extend(data)
        
        while True:
            # Find $ which starts NMEA sentence
            try:
                dollar_idx = self._byte_buffer.index(ord('$'))
            except ValueError:
                self._byte_buffer.clear()
                break
            
            # Remove garbage before $
            if dollar_idx > 0:
                del self._byte_buffer[:dollar_idx]
            
            # Find newline
            try:
                newline_idx = self._byte_buffer.index(ord('\n'))
            except ValueError:
                break
            
            # Extract sentence
            sentence_bytes = bytes(self._byte_buffer[:newline_idx])
            del self._byte_buffer[:newline_idx + 1]
            
            # Clean up and parse
            try:
                sentence = sentence_bytes.decode('ascii').strip()
                if sentence.startswith('$') and self._verify_checksum(sentence):
                    self._parse_nmea(sentence)
            except:
                pass
    
    def _verify_checksum(self, sentence: str) -> bool:
        """Verify NMEA sentence checksum."""
        if '*' not in sentence:
            return False
        
        try:
            data, checksum_str = sentence[1:].split('*')
            expected = int(checksum_str, 16)
            
            calculated = 0
            for char in data:
                calculated ^= ord(char)
            
            return calculated == expected
        except:
            return False
    
    def _parse_nmea(self, sentence: str):
        """Parse NMEA sentence."""
        parts = sentence.split(',')
        if len(parts) < 2:
            return
        
        msg_type = parts[0]
        
        if msg_type in ('$GPGGA', '$GNGGA'):
            self._parse_gga(parts)
        elif msg_type in ('$GPRMC', '$GNRMC'):
            self._parse_rmc(parts)
        elif msg_type in ('$GPGSA', '$GNGSA'):
            self._parse_gsa(parts)
    
    def _parse_gga(self, fields: List[str]):
        """Parse GGA sentence (fix data)."""
        if len(fields) < 15:
            return
        
        try:
            # Latitude
            if fields[2] and fields[3]:
                lat = self._parse_coordinate(fields[2], fields[3])
                self._pending_position.latitude = lat
            
            # Longitude
            if fields[4] and fields[5]:
                lon = self._parse_coordinate(fields[4], fields[5])
                self._pending_position.longitude = lon
            
            # Fix quality
            if fields[6]:
                self._pending_position.fix_quality = int(fields[6])
            
            # Satellites
            if fields[7]:
                self._pending_position.satellites = int(fields[7])
            
            # HDOP
            if fields[8]:
                self._pending_position.hdop = float(fields[8])
            
            # Altitude
            if fields[9]:
                self._pending_position.altitude = float(fields[9])
            
            self._pending_position.timestamp = datetime.now()
            
            # Update position if valid
            if self._pending_position.is_valid:
                self.current_position = GPSPosition(
                    latitude=self._pending_position.latitude,
                    longitude=self._pending_position.longitude,
                    altitude=self._pending_position.altitude,
                    satellites=self._pending_position.satellites,
                    fix_quality=self._pending_position.fix_quality,
                    hdop=self._pending_position.hdop,
                    timestamp=self._pending_position.timestamp
                )
                self.status_message = f"{self.current_position.satellites} satellites"
                self.position_updated.emit(self.current_position)
                
        except Exception as e:
            pass
    
    def _parse_rmc(self, fields: List[str]):
        """Parse RMC sentence (recommended minimum)."""
        if len(fields) < 12:
            return
        
        try:
            # Check validity
            if fields[2] != 'A':
                return
            
            # Latitude
            if fields[3] and fields[4]:
                lat = self._parse_coordinate(fields[3], fields[4])
                self._pending_position.latitude = lat
            
            # Longitude
            if fields[5] and fields[6]:
                lon = self._parse_coordinate(fields[5], fields[6])
                self._pending_position.longitude = lon
                
        except:
            pass
    
    def _parse_gsa(self, fields: List[str]):
        """Parse GSA sentence (DOP and active satellites)."""
        if len(fields) < 18:
            return
        
        try:
            # Fix type: 1=no fix, 2=2D, 3=3D
            if fields[2]:
                fix_type = int(fields[2])
                if fix_type >= 2:
                    self._pending_position.fix_quality = fix_type - 1
            
            # HDOP
            if fields[16]:
                self._pending_position.hdop = float(fields[16])
                
        except:
            pass
    
    def _parse_coordinate(self, value: str, direction: str) -> float:
        """Parse NMEA coordinate (DDMM.MMMM or DDDMM.MMMM)."""
        if not value:
            return 0.0
        
        # Find decimal point
        dot_idx = value.index('.')
        
        # Degrees are before the last 2 digits before decimal
        degrees = int(value[:dot_idx - 2])
        minutes = float(value[dot_idx - 2:])
        
        coord = degrees + minutes / 60.0
        
        if direction in ('S', 'W'):
            coord = -coord
        
        return coord
    
    @staticmethod
    def _calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate bearing from point 1 to point 2."""
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lon = math.radians(lon2 - lon1)
        
        x = math.sin(delta_lon) * math.cos(lat2_rad)
        y = (math.cos(lat1_rad) * math.sin(lat2_rad) -
             math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(delta_lon))
        
        bearing = math.degrees(math.atan2(x, y))
        return (bearing + 360) % 360
    
    @staticmethod
    def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points using Haversine formula."""
        R = 6371000  # Earth radius in meters
        
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        
        a = (math.sin(delta_phi / 2) ** 2 +
             math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        return R * c
