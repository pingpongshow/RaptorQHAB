"""
Ground Station Manager - Main controller for the ground station.
Coordinates serial communication, telemetry processing, and image decoding.
"""

import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, pyqtSignal, QTimer

from .serial_manager import SerialManager
from .gps_manager import GPSManager
from .protocol import (
    PacketType, TelemetryPayload, ImageMetaPayload, 
    ImageDataPayload, TextMessagePayload
)
from .telemetry import TelemetryPoint, PendingImage, ImageMetadata
from .config import get_config, get_data_directory, ModemConfig


@dataclass
class Statistics:
    """Ground station statistics."""
    packets_received: int = 0
    packets_valid: int = 0
    packets_invalid: int = 0
    telemetry_received: int = 0
    image_meta_received: int = 0
    image_data_received: int = 0
    images_decoded: int = 0
    last_packet_time: Optional[datetime] = None


class GroundStationManager(QObject):
    """
    Main ground station controller.
    
    Signals:
        telemetry_received: Emitted when new telemetry is received
        image_decoded: Emitted when an image is fully decoded (path, image_id)
        image_progress: Emitted when image reception progress updates (image_id, progress)
        text_message: Emitted when a text message is received
        status_changed: Emitted when connection status changes
        error: Emitted on error
    """
    
    telemetry_received = pyqtSignal(object)  # TelemetryPoint
    image_decoded = pyqtSignal(str, int)  # path, image_id
    image_progress = pyqtSignal(int, float)  # image_id, progress percentage
    text_message = pyqtSignal(str)
    status_changed = pyqtSignal(bool, str)  # is_receiving, status_message
    error = pyqtSignal(str)
    stats_updated = pyqtSignal(object)  # Statistics
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        self.config = get_config()
        
        # Managers
        self.serial_manager = SerialManager()
        self.gps_manager = GPSManager()
        
        # State
        self.is_receiving = False
        self.is_configured = False
        
        # Data
        self.telemetry_history: List[TelemetryPoint] = []
        self.latest_telemetry: Optional[TelemetryPoint] = None
        self.pending_images: Dict[int, PendingImage] = {}
        self.decoded_images: set = set()
        self.text_messages: List[tuple] = []  # (timestamp, message)
        
        self.statistics = Statistics()
        
        # RSSI/SNR from radio
        self.current_rssi: float = 0.0
        self.current_snr: float = 0.0
        
        # Connect signals
        self._connect_signals()
        
        # Inactivity timer
        self._inactivity_timer = QTimer()
        self._inactivity_timer.timeout.connect(self._check_inactivity)
        self._last_activity = datetime.now()
    
    def _connect_signals(self):
        """Connect internal signals."""
        self.serial_manager.packet_received.connect(self._on_packet_received)
        self.serial_manager.connected.connect(self._on_serial_connected)
        self.serial_manager.disconnected.connect(self._on_serial_disconnected)
        self.serial_manager.error.connect(self._on_error)
        self.serial_manager.config_response.connect(self._on_config_response)
        
        self.gps_manager.position_updated.connect(self._on_gps_updated)
    
    def get_available_ports(self) -> List[str]:
        """Get list of available serial ports."""
        return SerialManager.list_ports()
    
    def start_receiving(self, port: str = None, baud_rate: int = None) -> bool:
        """
        Start receiving data from the radio modem.
        
        Args:
            port: Serial port (uses config if not specified)
            baud_rate: Baud rate (uses config if not specified)
        
        Returns:
            True if started successfully
        """
        if self.is_receiving:
            return True
        
        port = port or self.config.serial_port
        baud_rate = baud_rate or self.config.serial_baud
        
        if not port:
            self.error.emit("No serial port configured")
            return False
        
        # Connect to serial port
        if not self.serial_manager.connect(port, baud_rate):
            return False
        
        # Configure modem
        self.serial_manager.configure_modem(self.config.modem)
        
        # Start inactivity timer
        self._last_activity = datetime.now()
        self._inactivity_timer.start(5000)  # Check every 5 seconds
        
        self.is_receiving = True
        self.status_changed.emit(True, f"Receiving on {port}")
        
        return True
    
    def stop_receiving(self):
        """Stop receiving data."""
        if not self.is_receiving:
            return
        
        self._inactivity_timer.stop()
        self.serial_manager.disconnect()
        
        self.is_receiving = False
        self.is_configured = False
        self.status_changed.emit(False, "Stopped")
    
    def clear_history(self):
        """Clear telemetry history."""
        self.telemetry_history.clear()
        self.latest_telemetry = None
    
    def _on_serial_connected(self):
        """Handle serial connection."""
        pass
    
    def _on_serial_disconnected(self):
        """Handle serial disconnection."""
        self.is_receiving = False
        self.is_configured = False
        self.status_changed.emit(False, "Disconnected")
    
    def _on_config_response(self, response: str):
        """Handle modem configuration response."""
        if "CFG_OK" in response or "CFG_ACK" in response:
            self.is_configured = True
            self.status_changed.emit(True, "Modem configured")
        elif "CFG_ERR" in response:
            self.is_configured = False
            self.error.emit(f"Modem config error: {response}")
    
    def _on_error(self, message: str):
        """Handle error."""
        self.error.emit(message)
    
    def _on_gps_updated(self, position):
        """Handle GPS position update."""
        # Update bearing if we have telemetry
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
            print(f"[GroundStation] Packet: type={ptype_name}, seq={sequence}, payload_len={len(payload)}, rssi={rssi:.1f}")
        
        if ptype == PacketType.TELEMETRY:
            self._handle_telemetry(sequence, payload, rssi, snr)
        elif ptype == PacketType.IMAGE_META:
            self._handle_image_meta(payload)
        elif ptype == PacketType.IMAGE_DATA:
            self._handle_image_data(payload)
        elif ptype == PacketType.TEXT_MESSAGE:
            self._handle_text_message(payload)
        
        self.statistics.last_packet_time = datetime.now()
        self.stats_updated.emit(self.statistics)
    
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
            time_diff = 1.0  # Assume 1 second between packets
            vertical_speed = (telem_payload.altitude - prev.altitude) / time_diff
        
        # Create telemetry point
        # Note: sequence comes from packet header, hdop is not in the protocol
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
            hdop=99.9,  # Not available in protocol, use default
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
        
        # Limit history size
        if len(self.telemetry_history) > 10000:
            self.telemetry_history = self.telemetry_history[-5000:]
        
        # Update GPS bearing
        if self.gps_manager.current_position:
            self.gps_manager.update_bearing(
                point.latitude, point.longitude, point.altitude
            )
        
        self.telemetry_received.emit(point)
    
    def _handle_image_meta(self, payload: bytes):
        """Process image metadata packet."""
        meta_payload = ImageMetaPayload.deserialize(payload)
        if not meta_payload:
            print(f"[GroundStation] Failed to deserialize IMAGE_META, payload len={len(payload)}")
            return
        
        self.statistics.image_meta_received += 1
        
        image_id = meta_payload.image_id
        print(f"[GroundStation] IMAGE_META: id={image_id}, size={meta_payload.total_size}, "
              f"symbols={meta_payload.num_source_symbols}, symbol_size={meta_payload.symbol_size}")
        
        # Skip if already decoded
        if image_id in self.decoded_images:
            return
        
        # Create or update pending image
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
        
        # Try to decode
        self._try_decode_image(image_id)
    
    def _handle_image_data(self, payload: bytes):
        """Process image data packet."""
        data_payload = ImageDataPayload.deserialize(payload)
        if not data_payload:
            print(f"[GroundStation] Failed to deserialize IMAGE_DATA, payload len={len(payload)}")
            return
        
        self.statistics.image_data_received += 1
        
        image_id = data_payload.image_id
        
        # Skip if already decoded
        if image_id in self.decoded_images:
            return
        
        # Create pending image if needed
        if image_id not in self.pending_images:
            self.pending_images[image_id] = PendingImage(image_id=image_id)
        
        pending = self.pending_images[image_id]
        pending.symbols[data_payload.symbol_id] = data_payload.symbol_data
        pending.last_received = datetime.now()
        
        # Log progress periodically
        symbol_count = len(pending.symbols)
        if symbol_count % 20 == 0 or symbol_count <= 5:
            needed = pending.metadata.num_source_symbols if pending.metadata else "?"
            print(f"[GroundStation] IMAGE_DATA: id={image_id}, symbol={data_payload.symbol_id}, "
                  f"count={symbol_count}/{needed}, data_len={len(data_payload.symbol_data)}")
        
        # Emit progress
        if pending.metadata:
            progress = pending.progress
            self.image_progress.emit(image_id, progress)
        
        # Try to decode
        self._try_decode_image(image_id)
    
    def _handle_text_message(self, payload: bytes):
        """Process text message packet."""
        msg_payload = TextMessagePayload.deserialize(payload)
        if not msg_payload:
            return
        
        self.text_messages.append((datetime.now(), msg_payload.message))
        
        # Limit messages
        if len(self.text_messages) > 100:
            self.text_messages = self.text_messages[-100:]
        
        self.text_message.emit(msg_payload.message)
    
    def _try_decode_image(self, image_id: int):
        """Attempt to decode an image using RaptorQ."""
        pending = self.pending_images.get(image_id)
        if not pending or not pending.metadata:
            return
        
        meta = pending.metadata
        min_symbols = meta.num_source_symbols
        
        # Need at least K symbols for RaptorQ
        if len(pending.symbols) < min_symbols:
            return
        
        print(f"[GroundStation] Attempting decode: id={image_id}, symbols={len(pending.symbols)}/{min_symbols}, "
              f"size={meta.total_size}, symbol_size={meta.symbol_size}")
        
        # Try RaptorQ decoding
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
                    print(f"[GroundStation] Successfully decoded image {image_id}! Size={len(result)} bytes")
                    self._save_decoded_image(image_id, result, meta)
                    return
            
            print(f"[GroundStation] Decode incomplete for image {image_id}, need more symbols")
            
        except ImportError:
            # RaptorQ library not available
            print("[GroundStation] raptorq library not installed")
        except Exception as e:
            print(f"[GroundStation] Decode error for image {image_id}: {e}")
            self.error.emit(f"Decode error: {e}")
    
    def _try_python_decode(self, image_id: int):
        """Try decoding with Python raptorq_decoder.py script."""
        # This would call the external Python decoder
        # For now, just log that native decoder isn't available
        pass
    
    def _save_decoded_image(self, image_id: int, data: bytes, meta: ImageMetadata):
        """Save decoded image to disk."""
        self.decoded_images.add(image_id)
        self.statistics.images_decoded += 1
        
        # Remove from pending
        if image_id in self.pending_images:
            del self.pending_images[image_id]
        
        # Save to images folder
        data_dir = get_data_directory()
        images_dir = data_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = int(datetime.now().timestamp())
        filename = f"image_{image_id}_{timestamp}.webp"
        filepath = images_dir / filename
        
        try:
            # Data should be the raw image (WebP)
            with open(filepath, 'wb') as f:
                f.write(data)
            
            self.image_decoded.emit(str(filepath), image_id)
            
        except Exception as e:
            self.error.emit(f"Failed to save image: {e}")
    
    def _check_inactivity(self):
        """Check for signal inactivity."""
        if not self.is_receiving:
            return
        
        inactive_seconds = (datetime.now() - self._last_activity).total_seconds()
        
        if inactive_seconds > 30:
            # No packets for 30 seconds
            self.status_changed.emit(True, f"No signal ({int(inactive_seconds)}s)")
