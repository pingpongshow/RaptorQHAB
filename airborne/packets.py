"""
RaptorHab Packets Module
Packet assembly and transmission scheduling
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Generator, Callable
from queue import Queue, Empty
from enum import IntEnum, auto

from common.constants import (
    PacketType, PacketFlags, TELEMETRY_INTERVAL_PACKETS,
    IMAGE_META_INTERVAL_PACKETS, MAX_PAYLOAD_SIZE
)
from common.protocol import (
    build_packet, TelemetryPayload, ImageMetaPayload, ImageDataPayload,
    TextMessagePayload, CommandAckPayload
)
from common.crc import crc32
from .fountain import FountainEncoder

logger = logging.getLogger(__name__)


class PacketPriority(IntEnum):
    """Packet transmission priority"""
    LOW = 0
    NORMAL = 1
    HIGH = 2
    URGENT = 3


@dataclass
class QueuedPacket:
    """Packet waiting to be transmitted"""
    packet_type: PacketType
    payload: object
    priority: PacketPriority = PacketPriority.NORMAL
    flags: int = PacketFlags.NONE
    created_at: float = field(default_factory=time.time)
    
    def __lt__(self, other):
        # Higher priority first, then older packets
        if self.priority != other.priority:
            return self.priority > other.priority
        return self.created_at < other.created_at


@dataclass
class ImageTransmission:
    """State for an image being transmitted"""
    image_id: int
    image_data: bytes
    width: int
    height: int
    timestamp: int
    encoder: Optional[FountainEncoder] = None
    symbols_sent: int = 0
    total_symbols: int = 0
    checksum: int = 0
    
    @property
    def progress_percent(self) -> int:
        """Get transmission progress as percentage"""
        if self.total_symbols == 0:
            return 0
        return min(100, int((self.symbols_sent / self.total_symbols) * 100))
    
    @property
    def is_complete(self) -> bool:
        """Check if transmission is complete"""
        return self.symbols_sent >= self.total_symbols


class PacketScheduler:
    """
    Schedules packets for transmission
    
    Handles interleaving of telemetry and image data according to spec:
    - Every 10th packet: TELEMETRY
    - Every 100th packet: IMAGE_META
    - Remaining packets: IMAGE_DATA
    """
    
    def __init__(
        self,
        telemetry_callback: Optional[Callable[[], TelemetryPayload]] = None,
        telemetry_interval: int = TELEMETRY_INTERVAL_PACKETS,
        image_meta_interval: int = IMAGE_META_INTERVAL_PACKETS,
        symbol_size: int = 200,
        overhead_percent: float = 25
    ):
        """
        Initialize packet scheduler
        
        Args:
            telemetry_callback: Optional function to get current telemetry
            telemetry_interval: Packets between telemetry
            image_meta_interval: Packets between image metadata
            symbol_size: Fountain code symbol size
            overhead_percent: Fountain code overhead
        """
        self.telemetry_callback = telemetry_callback
        self.telemetry_interval = telemetry_interval
        self.image_meta_interval = image_meta_interval
        self.symbol_size = symbol_size
        self.overhead_percent = overhead_percent
        
        self._sequence: int = 0
        self._packet_counter: int = 0
        self._current_image: Optional[ImageTransmission] = None
        self._image_queue: Queue = Queue(maxsize=5)
        self._priority_queue: List[QueuedPacket] = []
    
    def queue_image(
        self,
        image_id: int,
        image_data: bytes,
        width: int,
        height: int,
        timestamp: int
    ) -> bool:
        """
        Queue an image for transmission
        
        Args:
            image_id: Unique image ID
            image_data: WebP encoded image data
            width: Image width
            height: Image height
            timestamp: Capture timestamp
            
        Returns:
            True if queued successfully
        """
        try:
            # Create fountain encoder
            encoder = FountainEncoder(
                image_data,
                self.symbol_size,
                prefer_raptorq=True
            )
            
            total_symbols = encoder.get_recommended_symbol_count(self.overhead_percent)
            
            transmission = ImageTransmission(
                image_id=image_id,
                image_data=image_data,
                width=width,
                height=height,
                timestamp=timestamp,
                encoder=encoder,
                total_symbols=total_symbols,
                checksum=crc32(image_data)
            )
            
            self._image_queue.put_nowait(transmission)
            
            logger.info(
                f"Queued image {image_id}: {len(image_data)} bytes, "
                f"{total_symbols} symbols"
            )
            return True
            
        except Exception as e:
            logger.error(f"Failed to queue image: {e}")
            return False
    
    def queue_packet(
        self,
        packet_type: PacketType,
        payload: object,
        priority: PacketPriority = PacketPriority.NORMAL,
        flags: int = PacketFlags.NONE
    ):
        """Queue a packet for transmission"""
        packet = QueuedPacket(
            packet_type=packet_type,
            payload=payload,
            priority=priority,
            flags=flags
        )
        self._priority_queue.append(packet)
        self._priority_queue.sort()
    
    def queue_text_message(self, message: str, priority: PacketPriority = PacketPriority.NORMAL):
        """Queue a text message packet"""
        self.queue_packet(
            PacketType.TEXT_MSG,
            TextMessagePayload(message=message),
            priority=priority
        )
    
    def queue_command_ack(
        self,
        command_type: PacketType,
        command_seq: int,
        status: int = 0
    ):
        """Queue a command acknowledgment"""
        self.queue_packet(
            PacketType.CMD_ACK,
            CommandAckPayload(
                command_type=command_type,
                command_seq=command_seq,
                status=status
            ),
            priority=PacketPriority.HIGH
        )
    
    def get_next_packet(self, telemetry_bytes: Optional[bytes] = None) -> Optional[bytes]:
        """
        Get the next packet to transmit
        
        Args:
            telemetry_bytes: Optional pre-serialized telemetry payload
        
        Returns:
            Packet bytes or None if nothing to send
        """
        self._telemetry_bytes = telemetry_bytes
        
        # Check for priority packets first
        if self._priority_queue:
            queued = self._priority_queue.pop(0)
            return self._build_and_advance(queued.packet_type, queued.payload, queued.flags)
        
        self._packet_counter += 1
        
        # Check if this slot is for telemetry
        if self._packet_counter % self.telemetry_interval == 0:
            return self._get_telemetry_packet()
        
        # Check if this slot is for image metadata
        if self._packet_counter % self.image_meta_interval == 0:
            packet = self._get_image_meta_packet()
            if packet:
                return packet
        
        # Otherwise, send image data
        packet = self._get_image_data_packet()
        if packet:
            return packet
        
        # Fallback to telemetry if no image data
        return self._get_telemetry_packet()
    
    def _get_telemetry_packet(self) -> bytes:
        """Get telemetry packet"""
        try:
            # Use pre-serialized telemetry if available
            if hasattr(self, '_telemetry_bytes') and self._telemetry_bytes:
                return self._build_and_advance_raw(PacketType.TELEMETRY, self._telemetry_bytes)
            
            # Otherwise use callback if available
            if self.telemetry_callback:
                payload = self.telemetry_callback()
                return self._build_and_advance(PacketType.TELEMETRY, payload)
            
            # Fallback to empty telemetry
            return self._build_and_advance(PacketType.TELEMETRY, TelemetryPayload())
        except Exception as e:
            logger.error(f"Telemetry callback failed: {e}")
            return self._build_and_advance(PacketType.TELEMETRY, TelemetryPayload())
    
    def _get_image_meta_packet(self) -> Optional[bytes]:
        """Get image metadata packet"""
        # Start new image if needed
        if self._current_image is None:
            try:
                self._current_image = self._image_queue.get_nowait()
                logger.info(f"Starting transmission of image {self._current_image.image_id}")
            except Empty:
                return None
        
        return self._get_image_meta_packet_for_current()
    
    def _get_image_meta_packet_for_current(self) -> Optional[bytes]:
        """Get image metadata packet for current image (must have _current_image set)"""
        if self._current_image is None:
            return None
            
        img = self._current_image
        
        payload = ImageMetaPayload(
            image_id=img.image_id,
            total_size=len(img.image_data),
            symbol_size=self.symbol_size,
            num_source_symbols=img.encoder.num_source_symbols,
            checksum=img.checksum,
            width=img.width,
            height=img.height,
            timestamp=img.timestamp
        )
        
        return self._build_and_advance(PacketType.IMAGE_META, payload)
    
    def _get_image_data_packet(self) -> Optional[bytes]:
        """Get image data packet"""
        # Start new image if needed
        if self._current_image is None:
            try:
                self._current_image = self._image_queue.get_nowait()
                logger.info(f"Starting transmission of image {self._current_image.image_id}")
                # IMPORTANT: Send IMAGE_META first when starting a new image!
                # This ensures the ground station knows about the image before data arrives
                return self._get_image_meta_packet_for_current()
            except Empty:
                return None
        
        img = self._current_image
        
        # Check if transmission is complete
        if img.is_complete:
            logger.info(
                f"Image {img.image_id} transmission complete: "
                f"{img.symbols_sent} symbols"
            )
            self._current_image = None
            return None
        
        # Generate next symbol
        symbol_id, symbol_data = img.encoder.generate_symbol()
        img.symbols_sent += 1
        
        payload = ImageDataPayload(
            image_id=img.image_id,
            symbol_id=symbol_id,
            symbol_data=symbol_data
        )
        
        return self._build_and_advance(PacketType.IMAGE_DATA, payload)
    
    def _build_and_advance(
        self,
        packet_type: PacketType,
        payload: object,
        flags: int = PacketFlags.NONE
    ) -> bytes:
        """Build packet and advance sequence number"""
        packet = build_packet(packet_type, self._sequence, payload, flags)
        self._sequence = (self._sequence + 1) % 65536
        return packet
    
    def _build_and_advance_raw(
        self,
        packet_type: PacketType,
        payload_bytes: bytes,
        flags: int = PacketFlags.NONE
    ) -> bytes:
        """Build packet from raw payload bytes and advance sequence number"""
        from common.crc import crc32_bytes
        from common.constants import SYNC_WORD
        import struct
        
        header = struct.pack('>BHB', packet_type, self._sequence, flags)
        packet_without_crc = SYNC_WORD + header + payload_bytes
        crc = crc32_bytes(packet_without_crc)
        packet = packet_without_crc + crc
        self._sequence = (self._sequence + 1) % 65536
        return packet
    
    def get_current_image_status(self) -> tuple:
        """
        Get current image transmission status
        
        Returns:
            Tuple of (image_id, progress_percent)
        """
        if self._current_image:
            return (
                self._current_image.image_id,
                self._current_image.progress_percent
            )
        return (0, 0)
    
    def get_image_progress(self) -> dict:
        """
        Get current image transmission progress as dict
        
        Returns:
            Dict with 'image_id' and 'progress' keys
        """
        image_id, progress = self.get_current_image_status()
        return {'image_id': image_id, 'progress': progress}
    
    def add_image(
        self,
        image_id: int,
        image_data: bytes,
        width: int,
        height: int,
        timestamp: int
    ) -> bool:
        """Alias for queue_image for API compatibility"""
        return self.queue_image(image_id, image_data, width, height, timestamp)
    
    def has_pending_data(self) -> bool:
        """Check if there's data waiting to be transmitted"""
        return (
            self._current_image is not None
            or not self._image_queue.empty()
            or len(self._priority_queue) > 0
        )
    
    def get_queue_status(self) -> dict:
        """Get status of transmission queues"""
        return {
            'priority_queue': len(self._priority_queue),
            'image_queue': self._image_queue.qsize(),
            'current_image': self._current_image.image_id if self._current_image else None,
            'image_progress': self._current_image.progress_percent if self._current_image else 0,
            'packets_sent': self._packet_counter,
            'sequence': self._sequence
        }
    
    def clear_queues(self):
        """Clear all transmission queues"""
        self._priority_queue.clear()
        while not self._image_queue.empty():
            try:
                self._image_queue.get_nowait()
            except Empty:
                break
        self._current_image = None
        logger.info("Transmission queues cleared")
