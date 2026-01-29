"""
Serial port manager for Heltec LoRa radio modem communication.
"""

import serial
import serial.tools.list_ports
from threading import Thread, Event
from typing import Optional, Callable, List
from dataclasses import dataclass
from datetime import datetime

from PyQt6.QtCore import QObject, pyqtSignal

from .protocol import FrameExtractor, PacketParser, PacketType
from .config import ModemConfig


@dataclass
class SerialStats:
    """Serial port statistics."""
    bytes_received: int = 0
    frames_extracted: int = 0
    packets_valid: int = 0
    packets_invalid: int = 0
    last_packet_time: Optional[datetime] = None


class SerialManager(QObject):
    """
    Manages serial communication with the Heltec radio modem.
    
    Signals:
        packet_received: Emitted when a valid packet is received
                        Args: (packet_type, sequence, flags, payload, rssi, snr)
        connected: Emitted when serial port is connected
        disconnected: Emitted when serial port is disconnected
        error: Emitted on error with error message
        config_response: Emitted when modem sends config response
    """
    
    packet_received = pyqtSignal(int, int, int, bytes, float, float)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error = pyqtSignal(str)
    config_response = pyqtSignal(str)
    stats_updated = pyqtSignal(object)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
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
    
    @staticmethod
    def list_ports() -> List[str]:
        """List available serial ports."""
        ports = serial.tools.list_ports.comports()
        return [port.device for port in ports]
    
    def connect(self, port: str, baud_rate: int = 921600) -> bool:
        """
        Connect to serial port.
        
        Args:
            port: Serial port device path (e.g., "COM3" or "/dev/ttyUSB0")
            baud_rate: Baud rate (default 921600 to match Heltec modem)
        
        Returns:
            True if connected successfully
        """
        if self.is_connected:
            self.disconnect()
        
        try:
            self.serial = serial.Serial(
                port=port,
                baudrate=baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,  # 100ms read timeout
            )
            
            # Clear any pending data
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()
            
            self.is_connected = True
            self._frame_extractor.clear()
            self.stats = SerialStats()
            
            # Start read thread
            self._stop_event.clear()
            self._read_thread = Thread(target=self._read_loop, daemon=True)
            self._read_thread.start()
            
            self.connected.emit()
            return True
            
        except Exception as e:
            self.error.emit(f"Connection failed: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from serial port."""
        if not self.is_connected:
            return
        
        # Stop read thread
        self._stop_event.set()
        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=1.0)
        self._read_thread = None
        
        # Close serial port
        if self.serial:
            try:
                self.serial.close()
            except:
                pass
            self.serial = None
        
        self.is_connected = False
        self.is_configured = False
        self._frame_extractor.clear()
        
        self.disconnected.emit()
    
    def configure_modem(self, config: ModemConfig, timeout: float = 5.0) -> bool:
        """
        Send configuration to the modem.
        
        Args:
            config: Modem configuration
            timeout: Timeout in seconds
        
        Returns:
            True if configuration was acknowledged
        """
        if not self.is_connected or not self.serial:
            return False
        
        try:
            # Send configuration command
            cmd = config.config_command
            self.serial.write(cmd.encode("utf-8"))
            self.serial.flush()
            
            # Wait for acknowledgment (handled in read loop)
            # For now, assume success after sending
            self.is_configured = True
            return True
            
        except Exception as e:
            self.error.emit(f"Configuration failed: {e}")
            return False
    
    def write(self, data: bytes) -> bool:
        """Write data to serial port."""
        if not self.is_connected or not self.serial:
            return False
        
        try:
            self.serial.write(data)
            self.serial.flush()
            return True
        except Exception as e:
            self.error.emit(f"Write failed: {e}")
            return False
    
    def _read_loop(self):
        """Background thread for reading serial data."""
        while not self._stop_event.is_set() and self.serial:
            try:
                # Read available data
                if self.serial.in_waiting > 0:
                    data = self.serial.read(self.serial.in_waiting)
                    if data:
                        self.stats.bytes_received += len(data)
                        self._process_data(data)
                else:
                    # Small sleep to prevent busy loop
                    self._stop_event.wait(0.02)
                    
            except serial.SerialException as e:
                if not self._stop_event.is_set():
                    self.error.emit(f"Read error: {e}")
                break
            except Exception as e:
                if not self._stop_event.is_set():
                    self.error.emit(f"Read error: {e}")
    
    def _process_data(self, data: bytes):
        """Process received data."""
        # Check for text lines (modem status messages)
        self._extract_text_lines(data)
        
        # Extract frames
        frames = self._frame_extractor.add_data(data)
        
        for rssi, snr, payload in frames:
            self.stats.frames_extracted += 1
            self.current_rssi = rssi
            self.current_snr = snr
            
            # Parse packet
            result = PacketParser.parse(payload)
            if result:
                packet_type, sequence, flags, packet_payload = result
                self.stats.packets_valid += 1
                self.stats.last_packet_time = datetime.now()
                
                # Emit signal
                self.packet_received.emit(
                    packet_type, sequence, flags, packet_payload, rssi, snr
                )
            else:
                self.stats.packets_invalid += 1
        
        # Emit stats update occasionally
        if self.stats.frames_extracted % 10 == 0:
            self.stats_updated.emit(self.stats)
    
    def _extract_text_lines(self, data: bytes):
        """Extract text lines from data (for modem status messages)."""
        try:
            text = data.decode("utf-8", errors="ignore")
        except:
            return
        
        self._text_buffer += text
        
        # Process complete lines
        while "\n" in self._text_buffer:
            line, self._text_buffer = self._text_buffer.split("\n", 1)
            line = line.strip()
            
            if line:
                self._process_text_line(line)
    
    def _process_text_line(self, line: str):
        """Process a text line from the modem."""
        # Configuration acknowledgment
        if line.startswith("CFG_OK:") or line.startswith("CFG_ACK:"):
            self.is_configured = True
            self.config_response.emit(line)
        elif line.startswith("CFG_ERR:"):
            self.is_configured = False
            self.config_response.emit(line)
        elif line.startswith("STATUS:") or line.startswith("INFO:"):
            # Status message
            self.config_response.emit(line)
