"""
Web-compatible managers for RaptorHabGS.
These versions use standard Python threading and callbacks instead of PyQt6 signals.
"""

import serial
import serial.tools.list_ports
import threading
from threading import Thread, Event, Timer
from typing import Optional, Callable, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
import pynmea2
import math

from .protocol import (
    FrameExtractor, PacketParser, PacketType,
    TelemetryPayload, ImageMetaPayload, ImageDataPayload, TextMessagePayload
)
from .config import ModemConfig, get_config, get_data_directory
from .telemetry import TelemetryPoint, GPSPosition, BearingDistance, PendingImage, ImageMetadata


@dataclass
class SerialStats:
    """Serial port statistics."""
    bytes_received: int = 0
    frames_extracted: int = 0
    packets_valid: int = 0
    packets_invalid: int = 0
    last_packet_time: Optional[datetime] = None


class WebSerialManager:
    """
    Web-compatible serial manager using standard Python threading.
    Uses callbacks instead of Qt signals.
    """
    
    def __init__(self):
        self.serial: Optional[serial.Serial] = None
        self.is_connected = False
        self.is_configured = False
        
        self._read_thread: Optional[Thread] = None
        self._stop_event = Event()
        
        self._frame_extractor = FrameExtractor()
        self._text_buffer = ""
        
        self.stats = SerialStats()
        self.current_rssi = 0.0
        self.current_snr = 0.0
        
        # Callbacks (set by owner)
        self.on_packet_received: Optional[Callable] = None  # (packet_type, seq, flags, payload, rssi, snr)
        self.on_connected: Optional[Callable] = None
        self.on_disconnected: Optional[Callable] = None
        self.on_error: Optional[Callable] = None  # (message)
        self.on_config_response: Optional[Callable] = None  # (response)
    
    @staticmethod
    def list_ports() -> List[str]:
        """List available serial ports."""
        ports = serial.tools.list_ports.comports()
        return [port.device for port in ports]
    
    def connect(self, port: str, baud_rate: int = 921600) -> bool:
        """Connect to serial port."""
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
            self.serial.reset_output_buffer()
            
            self.is_connected = True
            self._frame_extractor.clear()
            self.stats = SerialStats()
            
            self._stop_event.clear()
            self._read_thread = Thread(target=self._read_loop, daemon=True)
            self._read_thread.start()
            
            if self.on_connected:
                self.on_connected()
            
            return True
            
        except Exception as e:
            if self.on_error:
                self.on_error(f"Connection failed: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from serial port."""
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
        self.is_configured = False
        self._frame_extractor.clear()
        
        if self.on_disconnected:
            self.on_disconnected()
    
    def configure_modem(self, config: ModemConfig) -> bool:
        """Send configuration to the modem."""
        if not self.is_connected or not self.serial:
            return False
        
        try:
            cmd = config.config_command
            print(f"[WebSerial] Sending config: {cmd.strip()}")
            self.serial.write(cmd.encode("utf-8"))
            self.serial.flush()
            self.is_configured = True
            return True
        except Exception as e:
            if self.on_error:
                self.on_error(f"Configuration failed: {e}")
            return False
    
    def _read_loop(self):
        """Background thread for reading serial data."""
        while not self._stop_event.is_set() and self.serial:
            try:
                if self.serial.in_waiting > 0:
                    data = self.serial.read(self.serial.in_waiting)
                    if data:
                        self.stats.bytes_received += len(data)
                        self._process_data(data)
                else:
                    self._stop_event.wait(0.02)
                    
            except serial.SerialException as e:
                if not self._stop_event.is_set():
                    if self.on_error:
                        self.on_error(f"Read error: {e}")
                break
            except Exception as e:
                if not self._stop_event.is_set():
                    if self.on_error:
                        self.on_error(f"Read error: {e}")
    
    def _process_data(self, data: bytes):
        """Process received data."""
        self._extract_text_lines(data)
        
        frames = self._frame_extractor.add_data(data)
        
        for rssi, snr, payload in frames:
            self.stats.frames_extracted += 1
            self.current_rssi = rssi
            self.current_snr = snr
            
            result = PacketParser.parse(payload)
            if result:
                packet_type, sequence, flags, packet_payload = result
                self.stats.packets_valid += 1
                self.stats.last_packet_time = datetime.now()
                
                if self.on_packet_received:
                    self.on_packet_received(
                        packet_type, sequence, flags, packet_payload, rssi, snr
                    )
            else:
                self.stats.packets_invalid += 1
    
    def _extract_text_lines(self, data: bytes):
        """Extract text lines from data (for modem status messages)."""
        try:
            text = data.decode("utf-8", errors="ignore")
        except:
            return
        
        self._text_buffer += text
        
        while "\n" in self._text_buffer:
            line, self._text_buffer = self._text_buffer.split("\n", 1)
            line = line.strip()
            
            if line:
                self._process_text_line(line)
    
    def _process_text_line(self, line: str):
        """Process a text line from the modem."""
        if line.startswith("CFG_OK:") or line.startswith("CFG_ACK:"):
            self.is_configured = True
            if self.on_config_response:
                self.on_config_response(line)
        elif line.startswith("CFG_ERR:"):
            self.is_configured = False
            if self.on_config_response:
                self.on_config_response(line)
        elif line.startswith("STATUS:") or line.startswith("INFO:") or line.startswith("["):
            if self.on_config_response:
                self.on_config_response(line)


class WebGPSManager:
    """
    Web-compatible GPS manager using standard Python threading.
    """
    
    def __init__(self):
        self.serial: Optional[serial.Serial] = None
        self.is_connected = False
        
        self._read_thread: Optional[Thread] = None
        self._stop_event = Event()
        
        self.current_position: Optional[GPSPosition] = None
        self.current_bearing: Optional[BearingDistance] = None
        
        # Callbacks
        self.on_position_updated: Optional[Callable] = None  # (position)
        self.on_bearing_updated: Optional[Callable] = None   # (bearing)
        self.on_error: Optional[Callable] = None             # (message)
    
    @staticmethod
    def list_ports() -> List[str]:
        """List available serial ports."""
        ports = serial.tools.list_ports.comports()
        return [port.device for port in ports]
    
    def connect(self, port: str, baud_rate: int = 9600) -> bool:
        """Connect to GPS receiver."""
        if self.is_connected:
            self.disconnect()
        
        try:
            self.serial = serial.Serial(
                port=port,
                baudrate=baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.5,
            )
            
            self.serial.reset_input_buffer()
            
            self.is_connected = True
            
            self._stop_event.clear()
            self._read_thread = Thread(target=self._read_loop, daemon=True)
            self._read_thread.start()
            
            print(f"[WebGPS] Connected to {port} at {baud_rate} baud")
            return True
            
        except Exception as e:
            if self.on_error:
                self.on_error(f"GPS connection failed: {e}")
            print(f"[WebGPS] Connection failed: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from GPS receiver."""
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
        print("[WebGPS] Disconnected")
    
    def _read_loop(self):
        """Background thread for reading GPS data."""
        buffer = ""
        
        while not self._stop_event.is_set() and self.serial:
            try:
                if self.serial.in_waiting > 0:
                    data = self.serial.read(self.serial.in_waiting)
                    try:
                        buffer += data.decode("ascii", errors="ignore")
                    except:
                        continue
                    
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if line.startswith("$"):
                            self._parse_nmea(line)
                else:
                    self._stop_event.wait(0.1)
                    
            except serial.SerialException as e:
                if not self._stop_event.is_set():
                    if self.on_error:
                        self.on_error(f"GPS read error: {e}")
                break
            except Exception as e:
                pass  # Ignore parse errors
    
    def _parse_nmea(self, sentence: str):
        """Parse NMEA sentence."""
        try:
            msg = pynmea2.parse(sentence)
            
            if isinstance(msg, pynmea2.GGA):
                if msg.latitude and msg.longitude:
                    position = GPSPosition(
                        latitude=msg.latitude,
                        longitude=msg.longitude,
                        altitude=msg.altitude or 0.0,
                        satellites=int(msg.num_sats) if msg.num_sats else 0,
                        fix_quality=int(msg.gps_qual) if msg.gps_qual else 0,
                        hdop=float(msg.horizontal_dil) if msg.horizontal_dil else 99.9,
                        timestamp=datetime.now()
                    )
                    self.current_position = position
                    
                    if self.on_position_updated:
                        self.on_position_updated(position)
                        
        except Exception as e:
            pass  # Ignore parse errors
    
    def update_bearing(self, target_lat: float, target_lon: float, target_alt: float = 0):
        """Calculate bearing and distance to target."""
        if not self.current_position or not self.current_position.is_valid:
            return
        
        pos = self.current_position
        
        # Haversine formula for distance
        R = 6371000  # Earth radius in meters
        
        lat1 = math.radians(pos.latitude)
        lat2 = math.radians(target_lat)
        dlat = math.radians(target_lat - pos.latitude)
        dlon = math.radians(target_lon - pos.longitude)
        
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        distance = R * c
        
        # Bearing calculation
        y = math.sin(dlon) * math.cos(lat2)
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        bearing = math.degrees(math.atan2(y, x))
        bearing = (bearing + 360) % 360
        
        # Elevation angle
        alt_diff = target_alt - pos.altitude
        elevation = math.degrees(math.atan2(alt_diff, distance)) if distance > 0 else 0
        
        self.current_bearing = BearingDistance(
            bearing=bearing,
            distance=distance,
            elevation=elevation
        )
        
        if self.on_bearing_updated:
            self.on_bearing_updated(self.current_bearing)


@dataclass
class WebStatistics:
    """Ground station statistics."""
    packets_received: int = 0
    packets_valid: int = 0
    packets_invalid: int = 0
    telemetry_received: int = 0
    image_meta_received: int = 0
    image_data_received: int = 0
    images_decoded: int = 0
    last_packet_time: Optional[datetime] = None


class WebGroundStationManager:
    """
    Web-compatible ground station manager.
    Uses callbacks instead of Qt signals.
    """
    
    def __init__(self):
        self.config = get_config()
        
        self.serial_manager = WebSerialManager()
        self.gps_manager = WebGPSManager()
        
        self.is_receiving = False
        self.is_configured = False
        
        self.telemetry_history: List[TelemetryPoint] = []
        self.latest_telemetry: Optional[TelemetryPoint] = None
        self.pending_images: Dict[int, PendingImage] = {}
        self.decoded_images: set = set()
        self.text_messages: List[tuple] = []
        
        self.statistics = WebStatistics()
        
        self.current_rssi: float = 0.0
        self.current_snr: float = 0.0
        
        # Inactivity tracking
        self._last_activity = datetime.now()
        self._inactivity_timer: Optional[Timer] = None
        
        # Callbacks
        self.on_telemetry_received: Optional[Callable] = None  # (telemetry_point)
        self.on_image_decoded: Optional[Callable] = None       # (path, image_id)
        self.on_image_progress: Optional[Callable] = None      # (image_id, progress)
        self.on_text_message: Optional[Callable] = None        # (message)
        self.on_status_changed: Optional[Callable] = None      # (is_receiving, message)
        self.on_error: Optional[Callable] = None               # (message)
        
        self._connect_serial_callbacks()
    
    def _connect_serial_callbacks(self):
        """Connect serial manager callbacks."""
        self.serial_manager.on_packet_received = self._on_packet_received
        self.serial_manager.on_connected = self._on_serial_connected
        self.serial_manager.on_disconnected = self._on_serial_disconnected
        self.serial_manager.on_error = self._on_error
        self.serial_manager.on_config_response = self._on_config_response
        
        self.gps_manager.on_position_updated = self._on_gps_updated
    
    def get_available_ports(self) -> List[str]:
        """Get list of available serial ports."""
        return WebSerialManager.list_ports()
    
    def start_receiving(self, port: str = None, baud_rate: int = None) -> bool:
        """Start receiving data from the radio modem."""
        if self.is_receiving:
            return True
        
        port = port or self.config.serial_port
        baud_rate = baud_rate or self.config.serial_baud
        
        if not port:
            if self.on_error:
                self.on_error("No serial port configured")
            return False
        
        if not self.serial_manager.connect(port, baud_rate):
            return False
        
        # Configure modem
        self.serial_manager.configure_modem(self.config.modem)
        
        # Start inactivity checking
        self._last_activity = datetime.now()
        self._start_inactivity_timer()
        
        self.is_receiving = True
        if self.on_status_changed:
            self.on_status_changed(True, f"Receiving on {port}")
        
        return True
    
    def stop_receiving(self):
        """Stop receiving data."""
        if not self.is_receiving:
            return
        
        self._stop_inactivity_timer()
        self.serial_manager.disconnect()
        
        self.is_receiving = False
        self.is_configured = False
        if self.on_status_changed:
            self.on_status_changed(False, "Stopped")
    
    def clear_history(self):
        """Clear telemetry history."""
        self.telemetry_history.clear()
        self.latest_telemetry = None
    
    def _start_inactivity_timer(self):
        """Start the inactivity check timer."""
        self._stop_inactivity_timer()
        self._inactivity_timer = Timer(5.0, self._check_inactivity)
        self._inactivity_timer.daemon = True
        self._inactivity_timer.start()
    
    def _stop_inactivity_timer(self):
        """Stop the inactivity check timer."""
        if self._inactivity_timer:
            self._inactivity_timer.cancel()
            self._inactivity_timer = None
    
    def _check_inactivity(self):
        """Check for signal inactivity."""
        if not self.is_receiving:
            return
        
        inactive_seconds = (datetime.now() - self._last_activity).total_seconds()
        
        if inactive_seconds > 30:
            if self.on_status_changed:
                self.on_status_changed(True, f"No signal ({int(inactive_seconds)}s)")
        
        # Restart timer
        self._start_inactivity_timer()
    
    def _on_serial_connected(self):
        """Handle serial connection."""
        pass
    
    def _on_serial_disconnected(self):
        """Handle serial disconnection."""
        self.is_receiving = False
        self.is_configured = False
        if self.on_status_changed:
            self.on_status_changed(False, "Disconnected")
    
    def _on_config_response(self, response: str):
        """Handle modem configuration response."""
        if "CFG_OK" in response or "CFG_ACK" in response:
            self.is_configured = True
            if self.on_status_changed:
                self.on_status_changed(True, "Modem configured")
        elif "CFG_ERR" in response:
            self.is_configured = False
            if self.on_error:
                self.on_error(f"Modem config error: {response}")
    
    def _on_error(self, message: str):
        """Handle error."""
        if self.on_error:
            self.on_error(message)
    
    def _on_gps_updated(self, position):
        """Handle GPS position update."""
        if self.latest_telemetry:
            self.gps_manager.update_bearing(
                self.latest_telemetry.latitude,
                self.latest_telemetry.longitude,
                self.latest_telemetry.altitude
            )
    
    def _on_packet_received(self, packet_type: int, sequence: int,
                            flags: int, payload: bytes, rssi: float, snr: float):
        """Handle received packet."""
        self._last_activity = datetime.now()
        self.current_rssi = rssi
        self.current_snr = snr
        
        self.statistics.packets_received += 1
        
        try:
            ptype = PacketType(packet_type)
            ptype_name = ptype.name
        except ValueError:
            ptype = PacketType.UNKNOWN
            ptype_name = f"UNKNOWN(0x{packet_type:02X})"
        
        # Log non-telemetry packets (telemetry is too frequent)
        if ptype != PacketType.TELEMETRY:
            print(f"[WebGS] Packet: type={ptype_name}, seq={sequence}, payload_len={len(payload)}, rssi={rssi:.1f}")
        
        if ptype == PacketType.TELEMETRY:
            self._handle_telemetry(sequence, payload, rssi, snr)
        elif ptype == PacketType.IMAGE_META:
            self._handle_image_meta(payload)
        elif ptype == PacketType.IMAGE_DATA:
            self._handle_image_data(payload)
        elif ptype == PacketType.TEXT_MESSAGE:
            self._handle_text_message(payload)
        
        self.statistics.last_packet_time = datetime.now()
    
    def _handle_telemetry(self, sequence: int, payload: bytes, rssi: float, snr: float):
        """Process telemetry packet."""
        telem_payload = TelemetryPayload.deserialize(payload)
        if not telem_payload:
            self.statistics.packets_invalid += 1
            return
        
        self.statistics.telemetry_received += 1
        
        # Calculate vertical speed from history
        vertical_speed = 0.0
        if self.latest_telemetry and len(self.telemetry_history) >= 2:
            prev = self.telemetry_history[-1]
            time_diff = 1.0
            vertical_speed = (telem_payload.altitude - prev.altitude) / time_diff
        
        point = TelemetryPoint(
            sequence=sequence,
            latitude=telem_payload.latitude,
            longitude=telem_payload.longitude,
            altitude=telem_payload.altitude,
            speed=telem_payload.speed,
            heading=telem_payload.heading,
            vertical_speed=vertical_speed,
            satellites=telem_payload.satellites,
            fix_type=telem_payload.fix_type,
            hdop=99.9,
            battery_mv=telem_payload.battery_mv,
            cpu_temp=telem_payload.cpu_temp,
            radio_temp=telem_payload.radio_temp,
            rssi=telem_payload.rssi,
            image_id=telem_payload.image_id,
            image_progress=telem_payload.image_progress,
            rx_rssi=rssi,
            rx_snr=snr,
            timestamp=datetime.now()
        )
        
        self.latest_telemetry = point
        self.telemetry_history.append(point)
        
        if len(self.telemetry_history) > 10000:
            self.telemetry_history = self.telemetry_history[-5000:]
        
        if self.gps_manager.current_position:
            self.gps_manager.update_bearing(
                point.latitude, point.longitude, point.altitude
            )
        
        if self.on_telemetry_received:
            self.on_telemetry_received(point)
    
    def _handle_image_meta(self, payload: bytes):
        """Process image metadata packet."""
        meta_payload = ImageMetaPayload.deserialize(payload)
        if not meta_payload:
            print(f"[WebGS] Failed to deserialize IMAGE_META, payload len={len(payload)}")
            return
        
        self.statistics.image_meta_received += 1
        
        image_id = meta_payload.image_id
        print(f"[WebGS] IMAGE_META: id={image_id}, size={meta_payload.total_size}, "
              f"symbols={meta_payload.num_source_symbols}, symbol_size={meta_payload.symbol_size}")
        
        if image_id in self.decoded_images:
            return
        
        if image_id not in self.pending_images:
            self.pending_images[image_id] = PendingImage(image_id=image_id)
        
        pending = self.pending_images[image_id]
        pending.metadata = ImageMetadata(
            image_id=meta_payload.image_id,
            total_size=meta_payload.total_size,
            symbol_size=meta_payload.symbol_size,
            num_source_symbols=meta_payload.num_source_symbols,
            width=meta_payload.width,
            height=meta_payload.height,
            checksum=meta_payload.checksum
        )
        
        self._try_decode_image(image_id)
    
    def _handle_image_data(self, payload: bytes):
        """Process image data packet."""
        data_payload = ImageDataPayload.deserialize(payload)
        if not data_payload:
            print(f"[WebGS] Failed to deserialize IMAGE_DATA, payload len={len(payload)}")
            return
        
        self.statistics.image_data_received += 1
        
        image_id = data_payload.image_id
        
        if image_id in self.decoded_images:
            return
        
        if image_id not in self.pending_images:
            self.pending_images[image_id] = PendingImage(image_id=image_id)
        
        pending = self.pending_images[image_id]
        pending.symbols[data_payload.symbol_id] = data_payload.symbol_data
        pending.last_received = datetime.now()
        
        # Log progress periodically
        symbol_count = len(pending.symbols)
        if symbol_count % 20 == 0 or symbol_count <= 5:
            needed = pending.metadata.num_source_symbols if pending.metadata else "?"
            print(f"[WebGS] IMAGE_DATA: id={image_id}, symbol={data_payload.symbol_id}, "
                  f"count={symbol_count}/{needed}, data_len={len(data_payload.symbol_data)}")
        
        if pending.metadata:
            progress = pending.progress
            if self.on_image_progress:
                self.on_image_progress(image_id, progress)
        
        self._try_decode_image(image_id)
    
    def _handle_text_message(self, payload: bytes):
        """Process text message packet."""
        msg_payload = TextMessagePayload.deserialize(payload)
        if not msg_payload:
            return
        
        self.text_messages.append((datetime.now(), msg_payload.message))
        
        if len(self.text_messages) > 100:
            self.text_messages = self.text_messages[-100:]
        
        if self.on_text_message:
            self.on_text_message(msg_payload.message)
    
    def _try_decode_image(self, image_id: int):
        """Attempt to decode an image using RaptorQ."""
        pending = self.pending_images.get(image_id)
        if not pending or not pending.metadata:
            return
        
        meta = pending.metadata
        min_symbols = meta.num_source_symbols
        
        if len(pending.symbols) < min_symbols:
            return
        
        print(f"[WebGS] Attempting decode: id={image_id}, symbols={len(pending.symbols)}/{min_symbols}, "
              f"size={meta.total_size}, symbol_size={meta.symbol_size}")
        
        try:
            import raptorq
            
            # Create decoder with transfer length and symbol size
            decoder = raptorq.Decoder.with_defaults(
                meta.total_size,
                meta.symbol_size
            )
            
            # Add symbols - the symbol_data is the raw raptorq packet bytes
            for symbol_id, symbol_data in pending.symbols.items():
                # The decode() method takes raw packet bytes directly
                result = decoder.decode(bytes(symbol_data))
                
                if result is not None:
                    # Successfully decoded!
                    print(f"[WebGS] Successfully decoded image {image_id}! Size={len(result)} bytes")
                    self._save_decoded_image(image_id, result, meta)
                    return
            
            print(f"[WebGS] Decode incomplete for image {image_id}, need more symbols")
            
        except ImportError:
            print("[WebGS] raptorq library not installed")
        except Exception as e:
            print(f"[WebGS] Decode error for image {image_id}: {e}")
            if self.on_error:
                self.on_error(f"Decode error: {e}")
    
    def _save_decoded_image(self, image_id: int, data: bytes, meta: ImageMetadata):
        """Save decoded image to disk."""
        self.decoded_images.add(image_id)
        self.statistics.images_decoded += 1
        
        if image_id in self.pending_images:
            del self.pending_images[image_id]
        
        data_dir = get_data_directory()
        images_dir = data_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = int(datetime.now().timestamp())
        filename = f"image_{image_id}_{timestamp}.webp"
        filepath = images_dir / filename
        
        try:
            with open(filepath, 'wb') as f:
                f.write(data)
            
            if self.on_image_decoded:
                self.on_image_decoded(str(filepath), image_id)
            
        except Exception as e:
            if self.on_error:
                self.on_error(f"Failed to save image: {e}")
