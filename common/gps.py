"""
RaptorHab GPS Module
L76K GPS interface with NMEA and PMTK support

Supports L76K/MediaTek GPS modules with PMTK protocol for configuration.
Includes balloon mode support for high altitude operation (up to ~80km).
"""

import os
import time
import logging
import serial
import threading
from dataclasses import dataclass
from typing import Optional, List, Callable

from common.constants import FixType

logger = logging.getLogger(__name__)


@dataclass
class GPSData:
    """GPS position and velocity data"""
    latitude: float = 0.0           # degrees
    longitude: float = 0.0          # degrees
    altitude: float = 0.0           # meters MSL
    altitude_ellipsoid: float = 0.0 # meters above ellipsoid
    speed: float = 0.0              # m/s ground speed
    heading: float = 0.0            # degrees true
    vertical_speed: float = 0.0     # m/s (positive = up)
    satellites: int = 0
    fix_type: FixType = FixType.NONE
    hdop: float = 99.9
    vdop: float = 99.9
    pdop: float = 99.9
    time_utc: int = 0               # Unix timestamp
    time_valid: bool = False
    position_valid: bool = False
    last_update: float = 0.0        # time.time() of last update
    
    def is_valid(self) -> bool:
        """Check if we have a valid 3D fix"""
        return self.fix_type == FixType.FIX_3D and self.position_valid
    
    def age(self) -> float:
        """Get age of position in seconds"""
        return time.time() - self.last_update if self.last_update > 0 else float('inf')


class GPS:
    """L76K GPS interface with NMEA parsing and PMTK configuration"""
    
    def __init__(
        self,
        device: str = "/dev/serial0",
        baudrate: int = 9600,
        airborne_mode: bool = False,  # Default False - only enable on airborne unit
        simulate: bool = False,
        # Aliases for compatibility
        port: str = None,
        simulation: bool = None,
        callback: Callable[['GPSData'], None] = None,
    ):
        """
        Initialize GPS interface
        
        Args:
            device: Serial device path (alias: port)
            baudrate: Serial baudrate (L76K default: 9600)
            airborne_mode: Enable balloon mode for high altitude (>18km)
                          Set True ONLY on airborne unit
            simulate: Enable simulation mode (alias: simulation)
            port: Alias for device
            simulation: Alias for simulate
            callback: Optional callback for GPS updates
        """
        # Handle aliases
        if port is not None:
            device = port
        if simulation is not None:
            simulate = simulation
            
        self.device = device
        self.baudrate = baudrate
        self.airborne_mode = airborne_mode
        self.simulate = simulate
        
        self._serial: Optional[serial.Serial] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._data = GPSData()
        self._data_lock = threading.Lock()
        self._callbacks: List[Callable[[GPSData], None]] = []
        
        # Register initial callback if provided
        if callback is not None:
            self._callbacks.append(callback)
        
        # NMEA parsing state
        self._nmea_buffer = ""
        
        # Simulation state
        self._sim_lat = 40.7128
        self._sim_lon = -74.0060
        self._sim_alt = 0.0
        self._sim_start_time = time.time()
    
    def init(self) -> bool:
        """
        Initialize GPS connection
        
        Returns:
            True on success
        """
        logger.info(f"GPS init: device={self.device}, baudrate={self.baudrate}, "
                   f"airborne_mode={self.airborne_mode}, simulate={self.simulate}")
        
        if self.simulate:
            logger.info("GPS in simulation mode")
            return True
        
        # Try primary device, then alternates
        # For L76K on Pi GPIO 14/15, use /dev/serial0 or /dev/ttyAMA0
        devices = [self.device]
        if self.device != "/dev/serial0":
            devices.append("/dev/serial0")
        if self.device != "/dev/ttyAMA0":
            devices.append("/dev/ttyAMA0")
        
        for dev in devices:
            if os.path.exists(dev):
                try:
                    self._serial = serial.Serial(
                        port=dev,
                        baudrate=self.baudrate,
                        timeout=1.0
                    )
                    logger.info(f"GPS connected on {dev}")
                    break
                except serial.SerialException as e:
                    logger.warning(f"Failed to open {dev}: {e}")
        
        if self._serial is None:
            logger.error("No GPS device found")
            return False
        
        # Configure L76K GPS
        time.sleep(0.5)
        self._configure_gps()
        
        return True
    
    def _configure_gps(self):
        """Configure L76K GPS module"""
        
        # Enable GPS + GLONASS + BeiDou for better satellite coverage
        # PMTK353: Search mode (GPS, GLONASS, Galileo, Galileo_full, BeiDou)
        # $PMTK353,1,1,0,0,1*2B = GPS + GLONASS + BeiDou
        self._send_pmtk("PMTK353,1,1,0,0,1")
        time.sleep(0.1)
        
        # Set balloon mode if requested (airborne unit only)
        if self.airborne_mode:
            self._set_balloon_mode()
        
        logger.info("L76K GPS configured")
    
    def _set_balloon_mode(self):
        """
        Set L76K GPS to balloon/flight mode for high altitude operation.
        
        PMTK886 - Set Navigation Mode:
          Mode 0: Normal mode (default) - altitude limit ~10km
          Mode 1: Fitness mode  
          Mode 2: Aviation mode - altitude limit ~10km
          Mode 3: Balloon mode - altitude limit ~80km
        
        Command: $PMTK886,3*2B
        Expected Response: $PMTK001,886,3*36
        
        This MUST be set for the GPS to work above 18km altitude!
        """
        logger.info("Setting L76K GPS to BALLOON MODE (PMTK886,3) for high altitude operation")
        
        # Send balloon mode command
        self._send_pmtk("PMTK886,3")
        
        time.sleep(0.1)
        
        logger.info("L76K balloon mode enabled - GPS will work up to ~80km altitude")
    
    def _send_pmtk(self, command: str):
        """
        Send PMTK command to L76K GPS.
        
        Args:
            command: PMTK command without $ prefix and checksum
                     e.g., "PMTK886,3" for balloon mode
        
        The checksum is calculated as XOR of all characters between $ and *
        """
        if self._serial is None:
            return
        
        # Calculate checksum (XOR of all characters in command)
        checksum = 0
        for char in command:
            checksum ^= ord(char)
        
        # Build complete sentence: $command*checksum\r\n
        sentence = f"${command}*{checksum:02X}\r\n"
        
        try:
            self._serial.write(sentence.encode('ascii'))
            logger.debug(f"Sent PMTK: {sentence.strip()}")
        except serial.SerialException as e:
            logger.error(f"Failed to send PMTK command: {e}")
    
    def start(self):
        """Start GPS reading thread"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        logger.info("GPS reader started")
    
    def stop(self):
        """Stop GPS reading thread"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        
        if self._serial:
            self._serial.close()
            self._serial = None
        
        logger.info("GPS reader stopped")
    
    def _read_loop(self):
        """Main GPS reading loop"""
        bytes_received = 0
        last_log_time = time.time()
        
        while self._running:
            if self.simulate:
                self._update_simulation()
                time.sleep(1.0)
                continue
            
            if self._serial is None:
                time.sleep(1.0)
                continue
            
            try:
                # Read available data
                if self._serial.in_waiting > 0:
                    data = self._serial.read(self._serial.in_waiting)
                    bytes_received += len(data)
                    self._process_data(data)
                else:
                    time.sleep(0.01)
                
                # Log stats every 10 seconds
                now = time.time()
                if now - last_log_time >= 10.0:
                    with self._data_lock:
                        logger.debug(f"GPS stats: {bytes_received} bytes, "
                                   f"fix={self._data.position_valid}, "
                                   f"sats={self._data.satellites}, "
                                   f"alt={self._data.altitude:.1f}m")
                    bytes_received = 0
                    last_log_time = now
            
            except serial.SerialException as e:
                logger.error(f"GPS serial error: {e}")
                time.sleep(1.0)
            except Exception as e:
                logger.error(f"GPS read error: {e}")
                time.sleep(0.1)
    
    def _process_data(self, data: bytes):
        """Process received GPS data (NMEA sentences)"""
        try:
            text = data.decode('ascii', errors='ignore')
            for char in text:
                self._parse_nmea_byte(char)
        except Exception as e:
            logger.error(f"GPS data processing error: {e}")
    
    def _parse_nmea_byte(self, char: str):
        """Parse one character of NMEA data"""
        if char == '$':
            self._nmea_buffer = char
        elif char == '\n':
            if self._nmea_buffer.startswith('$'):
                self._handle_nmea_sentence(self._nmea_buffer.strip())
            self._nmea_buffer = ""
        else:
            self._nmea_buffer += char
            if len(self._nmea_buffer) > 100:
                self._nmea_buffer = ""
    
    def _handle_nmea_sentence(self, sentence: str):
        """Handle complete NMEA sentence"""
        if not sentence or '*' not in sentence:
            return
        
        # Verify checksum
        try:
            msg, checksum = sentence[1:].split('*')
        except ValueError:
            return
            
        calculated = 0
        for char in msg:
            calculated ^= ord(char)
        
        try:
            if calculated != int(checksum, 16):
                return
        except ValueError:
            return
        
        parts = msg.split(',')
        msg_type = parts[0]
        
        # Handle different NMEA sentence types
        # L76K outputs: GNRMC, GNGGA, GNVTG, GNGSA, GPGSV, GLGSV, BDGSV, GNGLL
        if msg_type in ('GNGGA', 'GPGGA', 'GLGGA', 'BDGGA'):
            self._parse_nmea_gga(parts)
        elif msg_type in ('GNRMC', 'GPRMC', 'GLRMC', 'BDRMC'):
            self._parse_nmea_rmc(parts)
        elif msg_type in ('GNGSA', 'GPGSA', 'GLGSA', 'BDGSA'):
            self._parse_nmea_gsa(parts)
        # Handle PMTK ACK responses
        elif msg_type == 'PMTK001':
            self._handle_pmtk_ack(parts)
    
    def _parse_nmea_gga(self, parts: List[str]):
        """Parse NMEA GGA sentence (position fix data)"""
        try:
            if len(parts) < 15:
                return
            
            # Parse latitude
            if parts[2] and parts[3]:
                lat = float(parts[2][:2]) + float(parts[2][2:]) / 60
                if parts[3] == 'S':
                    lat = -lat
            else:
                return
            
            # Parse longitude
            if parts[4] and parts[5]:
                lon = float(parts[4][:3]) + float(parts[4][3:]) / 60
                if parts[5] == 'W':
                    lon = -lon
            else:
                return
            
            # Parse altitude
            alt = float(parts[9]) if parts[9] else 0.0
            
            # Parse satellites
            sats = int(parts[7]) if parts[7] else 0
            
            # Parse fix quality (0=invalid, 1=GPS fix, 2=DGPS fix)
            fix = int(parts[6]) if parts[6] else 0
            
            # Parse HDOP
            hdop = float(parts[8]) if parts[8] else 99.9
            
            with self._data_lock:
                self._data.latitude = lat
                self._data.longitude = lon
                self._data.altitude = alt
                self._data.satellites = sats
                self._data.hdop = hdop
                self._data.fix_type = FixType.FIX_3D if fix >= 1 else FixType.NONE
                self._data.position_valid = fix >= 1
                self._data.last_update = time.time()
            
            self._notify_callbacks()
            
        except (ValueError, IndexError) as e:
            logger.debug(f"GGA parse error: {e}")
    
    def _parse_nmea_rmc(self, parts: List[str]):
        """Parse NMEA RMC sentence (recommended minimum data)"""
        try:
            if len(parts) < 12:
                return
            
            # Parse time
            if parts[1] and len(parts[1]) >= 6:
                try:
                    hour = int(parts[1][0:2])
                    minute = int(parts[1][2:4])
                    second = int(float(parts[1][4:]))
                    
                    # Parse date if available
                    if parts[9] and len(parts[9]) >= 6:
                        day = int(parts[9][0:2])
                        month = int(parts[9][2:4])
                        year = 2000 + int(parts[9][4:6])
                        
                        import calendar
                        from datetime import datetime
                        dt = datetime(year, month, day, hour, minute, second)
                        
                        with self._data_lock:
                            self._data.time_utc = calendar.timegm(dt.timetuple())
                            self._data.time_valid = True
                except (ValueError, IndexError):
                    pass
            
            # Parse speed (knots to m/s)
            if parts[7]:
                speed = float(parts[7]) * 0.514444
                with self._data_lock:
                    self._data.speed = speed
            
            # Parse heading
            if parts[8]:
                heading = float(parts[8])
                with self._data_lock:
                    self._data.heading = heading
            
        except (ValueError, IndexError):
            pass
    
    def _parse_nmea_gsa(self, parts: List[str]):
        """Parse NMEA GSA sentence (DOP and active satellites)"""
        try:
            if len(parts) < 18:
                return
            
            # Parse PDOP, HDOP, VDOP
            pdop = float(parts[15]) if parts[15] else 99.9
            hdop = float(parts[16]) if parts[16] else 99.9
            vdop = float(parts[17].split('*')[0]) if parts[17] else 99.9
            
            with self._data_lock:
                self._data.pdop = pdop
                self._data.hdop = hdop
                self._data.vdop = vdop
            
        except (ValueError, IndexError):
            pass
    
    def _handle_pmtk_ack(self, parts: List[str]):
        """Handle PMTK ACK response"""
        try:
            if len(parts) >= 3:
                cmd = parts[1]
                result = parts[2].split('*')[0]
                
                result_text = {
                    '0': 'Invalid',
                    '1': 'Unsupported',
                    '2': 'Failed',
                    '3': 'Success'
                }.get(result, 'Unknown')
                
                logger.debug(f"PMTK ACK: cmd={cmd}, result={result_text}")
                
                # Log balloon mode confirmation
                if cmd == '886' and result == '3':
                    logger.info("L76K balloon mode CONFIRMED by GPS")
                    
        except (ValueError, IndexError):
            pass
    
    def _update_simulation(self):
        """Update simulated GPS data"""
        elapsed = time.time() - self._sim_start_time
        
        with self._data_lock:
            # Simulate balloon ascent
            self._data.latitude = self._sim_lat + (elapsed * 0.0001)
            self._data.longitude = self._sim_lon + (elapsed * 0.00005)
            self._data.altitude = min(elapsed * 5, 35000)  # 5 m/s ascent, max 35km
            self._data.speed = 10 + (elapsed * 0.01)
            self._data.heading = (elapsed * 2) % 360
            self._data.vertical_speed = 5.0 if self._data.altitude < 35000 else 0.0
            self._data.satellites = 12
            self._data.fix_type = FixType.FIX_3D
            self._data.position_valid = True
            self._data.time_utc = int(time.time())
            self._data.time_valid = True
            self._data.hdop = 1.0
            self._data.last_update = time.time()
        
        self._notify_callbacks()
    
    def add_callback(self, callback: Callable[[GPSData], None]):
        """Add callback for GPS data updates"""
        self._callbacks.append(callback)
    
    def remove_callback(self, callback: Callable[[GPSData], None]):
        """Remove callback"""
        if callback in self._callbacks:
            self._callbacks.remove(callback)
    
    def _notify_callbacks(self):
        """Notify all registered callbacks"""
        data = self.get_data()
        for callback in self._callbacks:
            try:
                callback(data)
            except Exception as e:
                logger.error(f"GPS callback error: {e}")
    
    def get_data(self) -> GPSData:
        """Get current GPS data (thread-safe copy)"""
        with self._data_lock:
            return GPSData(
                latitude=self._data.latitude,
                longitude=self._data.longitude,
                altitude=self._data.altitude,
                altitude_ellipsoid=self._data.altitude_ellipsoid,
                speed=self._data.speed,
                heading=self._data.heading,
                vertical_speed=self._data.vertical_speed,
                satellites=self._data.satellites,
                fix_type=self._data.fix_type,
                hdop=self._data.hdop,
                vdop=self._data.vdop,
                pdop=self._data.pdop,
                time_utc=self._data.time_utc,
                time_valid=self._data.time_valid,
                position_valid=self._data.position_valid,
                last_update=self._data.last_update
            )
    
    def wait_for_fix(self, timeout_sec: float = 120) -> bool:
        """
        Wait for a valid GPS fix
        
        Args:
            timeout_sec: Maximum time to wait
            
        Returns:
            True if fix acquired
        """
        start = time.time()
        
        while time.time() - start < timeout_sec:
            data = self.get_data()
            if data.is_valid():
                logger.info(f"GPS fix acquired: {data.latitude:.6f}, {data.longitude:.6f}, "
                          f"alt={data.altitude:.1f}m, sats={data.satellites}")
                return True
            time.sleep(0.5)
        
        logger.warning("GPS fix timeout")
        return False


# Alias for backward compatibility
GPSReader = GPS
