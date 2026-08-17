"""
RaptorHab Ground Station - Receiver Module
Handles radio reception and packet processing (receive-only)
"""

import logging
import time
import threading
from dataclasses import dataclass
from typing import Optional, Callable, Dict, Any
from enum import IntEnum, auto

from common.constants import PacketType, SYNC_WORD
from common.protocol import (
    parse_packet, TelemetryPayload, ImageMetaPayload,
    ImageDataPayload, TextMessagePayload
)
from common.radio import SX1262Radio

from ground.config import GroundConfig
from ground.decoder import FountainDecoder, ImageMetadata
from ground.telemetry import TelemetryProcessor, TelemetryPoint

logger = logging.getLogger(__name__)


class ReceiverState(IntEnum):
    """Receiver state machine states"""
    STOPPED = auto()
    RECEIVING = auto()
    ERROR = auto()


@dataclass
class ReceiverStats:
    """Receiver statistics"""
    packets_received: int = 0
    packets_valid: int = 0
    packets_invalid: int = 0
    telemetry_packets: int = 0
    image_meta_packets: int = 0
    image_data_packets: int = 0
    text_packets: int = 0
    unknown_packets: int = 0
    crc_errors: int = 0
    last_rssi: int = 0
    last_packet_time: float = 0


class PacketReceiver:
    """
    Main packet receiver class (receive-only)
    
    Coordinates radio reception, packet parsing, and dispatching
    to appropriate handlers (telemetry, image decoder, etc.)
    """
    
    def __init__(
        self,
        config: GroundConfig,
        telemetry_processor: TelemetryProcessor,
        image_decoder: FountainDecoder,
        on_packet: Optional[Callable[[PacketType, int, Any, int], None]] = None,
        on_text_message: Optional[Callable[[str], None]] = None,
    ):
        """
        Initialize packet receiver
        
        Args:
            config: Ground station configuration
            telemetry_processor: Telemetry processor instance
            image_decoder: Fountain code decoder instance
            on_packet: Callback for all packets (type, seq, payload, rssi)
            on_text_message: Callback for text messages
        """
        self.config = config
        self.telemetry = telemetry_processor
        self.decoder = image_decoder
        self.on_packet = on_packet
        self.on_text_message = on_text_message
        
        # Radio
        self._radio: Optional[SX1262Radio] = None
        
        # State
        self._state = ReceiverState.STOPPED
        self._state_lock = threading.Lock()
        
        # Receive thread
        self._receive_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Statistics
        self.stats = ReceiverStats()
        
        # Raw packet logging
        self._log_raw = config.log_raw_packets
        self._raw_log_path = f"{config.log_path}/raw_packets.log"
    
    def start(self) -> bool:
        """
        Start the receiver
        
        Returns:
            True if started successfully
        """
        if self._state != ReceiverState.STOPPED:
            logger.warning("Receiver already running")
            return False
        
        try:
            # Initialize radio
            logger.info("Initializing radio...")
            self._radio = SX1262Radio(
                frequency_mhz=self.config.frequency_mhz,
                tx_power_dbm=self.config.tx_power_dbm,
                bitrate_bps=self.config.bitrate_bps,
                fdev_hz=self.config.fdev_hz,
                pin_cs=self.config.pin_cs,
                pin_busy=self.config.pin_busy,
                pin_dio1=self.config.pin_dio1,
                pin_reset=self.config.pin_rst,
                pin_txen=self.config.pin_txen,
                simulation=self.config.simulate_radio,
            )
            
            if not self._radio.init():
                raise RuntimeError("Radio initialization failed")
            
            logger.info(f"Radio initialized at {self.config.frequency_mhz} MHz")
            
            # Start receive thread
            self._stop_event.clear()
            self._receive_thread = threading.Thread(
                target=self._receive_loop,
                name="PacketReceiver",
                daemon=True
            )
            self._receive_thread.start()
            
            with self._state_lock:
                self._state = ReceiverState.RECEIVING
            
            logger.info("Packet receiver started")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start receiver: {e}")
            with self._state_lock:
                self._state = ReceiverState.ERROR
            return False
    
    def stop(self):
        """Stop the receiver"""
        logger.info("Stopping packet receiver...")
        
        self._stop_event.set()
        
        if self._receive_thread and self._receive_thread.is_alive():
            self._receive_thread.join(timeout=5.0)
        
        if self._radio:
            self._radio.close()
            self._radio = None
        
        with self._state_lock:
            self._state = ReceiverState.STOPPED
        
        logger.info("Packet receiver stopped")
    
    def _receive_loop(self):
        """Main receive loop"""
        logger.info("Receive loop started")
        
        # Put radio in continuous receive mode
        self._radio.receive_continuous()
        
        while not self._stop_event.is_set():
            try:
                # Check for packet
                packet_data, rssi = self._radio.check_for_packet()
                
                if packet_data:
                    self._process_packet(packet_data, rssi)
                
                # Small sleep to prevent busy-waiting
                time.sleep(0.001)
                
            except Exception as e:
                logger.error(f"Receive loop error: {e}")
                time.sleep(0.1)
        
        logger.info("Receive loop stopped")
    
    def _process_packet(self, packet_data: bytes, rssi: int):
        """Process a received packet"""
        self.stats.packets_received += 1
        self.stats.last_rssi = rssi
        self.stats.last_packet_time = time.time()
        
        # Log raw packet if enabled
        if self._log_raw:
            self._log_raw_packet(packet_data, rssi)
        
        # Parse packet - returns tuple (packet_type, sequence, flags, payload)
        try:
            result = parse_packet(packet_data)
            if result is None:
                self.stats.packets_invalid += 1
                logger.debug("Failed to parse packet")
                return
            
            packet_type, sequence, flags, payload = result
        except Exception as e:
            self.stats.packets_invalid += 1
            logger.warning(f"Packet parse error: {e}")
            return
        
        self.stats.packets_valid += 1
        
        logger.debug(
            f"RX packet: type={packet_type.name} seq={sequence} "
            f"len={len(payload)} rssi={rssi}"
        )
        
        # Dispatch to handler
        try:
            if packet_type == PacketType.TELEMETRY:
                self._handle_telemetry(payload, sequence, rssi)
            elif packet_type == PacketType.IMAGE_META:
                self._handle_image_meta(payload)
            elif packet_type == PacketType.IMAGE_DATA:
                self._handle_image_data(payload)
            elif packet_type == PacketType.TEXT_MESSAGE:
                self._handle_text_message(payload)
            else:
                self.stats.unknown_packets += 1
                logger.debug(f"Unknown packet type: {packet_type}")
        except Exception as e:
            logger.error(f"Packet handler error: {e}")
        
        # General callback
        if self.on_packet:
            try:
                self.on_packet(packet_type, sequence, payload, rssi)
            except Exception as e:
                logger.error(f"Packet callback error: {e}")
    
    def _handle_telemetry(self, payload: bytes, sequence: int, rssi: int):
        """Handle telemetry packet"""
        self.stats.telemetry_packets += 1
        
        try:
            telem = TelemetryPayload.deserialize(payload)
            self.telemetry.process_packet(telem, rssi, sequence)
        except Exception as e:
            logger.error(f"Telemetry decode error: {e}")
    
    def _handle_image_meta(self, payload: bytes):
        """Handle image metadata packet"""
        self.stats.image_meta_packets += 1
        
        try:
            meta = ImageMetaPayload.deserialize(payload)
            logger.info(
                f"Image {meta.image_id}: {meta.width}x{meta.height}, "
                f"{meta.total_size} bytes, {meta.num_source_symbols} symbols"
            )
            
            # Initialize decoder for this image
            metadata = ImageMetadata(
                image_id=meta.image_id,
                total_size=meta.total_size,
                symbol_size=meta.symbol_size,
                num_source_symbols=meta.num_source_symbols,
                checksum=meta.checksum,
                width=meta.width,
                height=meta.height,
                timestamp=meta.timestamp
            )
            self.decoder.add_metadata(metadata)
            
        except Exception as e:
            logger.error(f"Image meta decode error: {e}")
    
    def _handle_image_data(self, payload: bytes):
        """Handle image data packet"""
        self.stats.image_data_packets += 1
        
        try:
            img_data = ImageDataPayload.deserialize(payload)
            self.decoder.add_symbol(
                img_data.image_id,
                img_data.symbol_id,
                img_data.symbol_data
            )
        except Exception as e:
            logger.error(f"Image data decode error: {e}")
    
    def _handle_text_message(self, payload: bytes):
        """Handle text message packet"""
        self.stats.text_packets += 1
        
        try:
            msg = TextMessagePayload.deserialize(payload)
            logger.info(f"Text message: {msg.message}")
            
            if self.on_text_message:
                self.on_text_message(msg.message)
                
        except Exception as e:
            logger.error(f"Text message decode error: {e}")
    
    def _log_raw_packet(self, packet_data: bytes, rssi: int):
        """Log raw packet to file"""
        try:
            with open(self._raw_log_path, 'a') as f:
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                hex_data = packet_data.hex()
                f.write(f"{timestamp},{rssi},{len(packet_data)},{hex_data}\n")
        except Exception as e:
            logger.error(f"Raw packet log error: {e}")
    
    def get_state(self) -> ReceiverState:
        """Get current receiver state"""
        with self._state_lock:
            return self._state
    
    def get_stats(self) -> Dict:
        """Get receiver statistics"""
        return {
            'state': self._state.name,
            'packets_received': self.stats.packets_received,
            'packets_valid': self.stats.packets_valid,
            'packets_invalid': self.stats.packets_invalid,
            'crc_errors': self.stats.crc_errors,
            'telemetry_packets': self.stats.telemetry_packets,
            'image_meta_packets': self.stats.image_meta_packets,
            'image_data_packets': self.stats.image_data_packets,
            'text_packets': self.stats.text_packets,
            'last_rssi': self.stats.last_rssi,
            'last_packet_time': self.stats.last_packet_time,
        }
    
    def get_signal_quality(self) -> Dict:
        """Get signal quality metrics"""
        if self.stats.packets_received == 0:
            return {
                'rssi': 0,
                'packet_rate': 0,
                'success_rate': 0,
            }
        
        # Calculate packet rate (packets per minute)
        elapsed = time.time() - self.stats.last_packet_time if self.stats.last_packet_time > 0 else 0
        
        return {
            'rssi': self.stats.last_rssi,
            'success_rate': (self.stats.packets_valid / self.stats.packets_received) * 100,
            'last_packet_age': elapsed,
        }


class SimulatedReceiver:
    """
    Simulated receiver for testing without hardware
    
    Generates fake telemetry and image data
    """
    
    def __init__(
        self,
        telemetry_processor: TelemetryProcessor,
        image_decoder: FountainDecoder,
        telemetry_interval_sec: float = 1.0
    ):
        """
        Initialize simulated receiver
        
        Args:
            telemetry_processor: Telemetry processor
            image_decoder: Image decoder
            telemetry_interval_sec: Interval between telemetry points
        """
        self.telemetry = telemetry_processor
        self.decoder = image_decoder
        self.telemetry_interval = telemetry_interval_sec
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        # Simulation state
        self._lat = 40.0
        self._lon = -74.0
        self._alt = 0.0
        self._ascending = True
        self._sequence = 0
    
    def start(self):
        """Start simulation"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._simulation_loop, daemon=True)
        self._thread.start()
        logger.info("Simulated receiver started")
    
    def stop(self):
        """Stop simulation"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("Simulated receiver stopped")
    
    def _simulation_loop(self):
        """Generate simulated data"""
        import random
        
        while self._running:
            # Generate telemetry
            self._generate_telemetry()
            
            # Update position
            self._lat += random.uniform(-0.001, 0.001)
            self._lon += random.uniform(-0.001, 0.001)
            
            if self._ascending:
                self._alt += random.uniform(3, 8)  # Ascent rate ~5 m/s
                if self._alt > 30000:
                    self._ascending = False
            else:
                self._alt -= random.uniform(4, 10)  # Descent rate ~7 m/s
                if self._alt < 0:
                    self._alt = 0
                    self._ascending = True
            
            time.sleep(self.telemetry_interval)
    
    def _generate_telemetry(self):
        """Generate a simulated telemetry point"""
        import random
        
        payload = TelemetryPayload(
            latitude=self._lat,
            longitude=self._lon,
            altitude=self._alt,
            speed=random.uniform(0, 30),
            heading=random.uniform(0, 360),
            satellites=random.randint(6, 12),
            fix_type=2,  # 3D fix
            gps_time=int(time.time()),
            battery_mv=random.randint(3500, 4200),
            cpu_temp=random.uniform(20, 50),
            radio_temp=random.uniform(15, 40),
            image_id=0,
            image_progress=0,
            rssi=random.randint(-120, -60),
        )
        
        rssi = random.randint(-100, -60)
        self._sequence = (self._sequence + 1) % 65536
        
        self.telemetry.process_packet(payload, rssi, self._sequence)
