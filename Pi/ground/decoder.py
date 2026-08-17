"""
RaptorHab Ground Station - Fountain Code Decoder
Reconstructs images from received fountain-coded symbols using RaptorQ
"""

import logging
import time
import hashlib
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Set, Tuple, Callable
from enum import IntEnum, auto

logger = logging.getLogger(__name__)

# Check raptorq availability at module load
try:
    import raptorq
    RAPTORQ_AVAILABLE = True
    logger.info("raptorq library loaded successfully")
except ImportError as e:
    RAPTORQ_AVAILABLE = False
    logger.error(f"raptorq library not available: {e}")
    logger.error("Install with: pip install raptorq")


class ImageStatus(IntEnum):
    """Status of image reconstruction"""
    RECEIVING = auto()
    COMPLETE = auto()
    FAILED = auto()
    TIMEOUT = auto()


@dataclass
class ImageMetadata:
    """Metadata for an image being reconstructed"""
    image_id: int
    total_size: int
    symbol_size: int
    num_source_symbols: int
    checksum: int
    width: int
    height: int
    timestamp: int
    first_received: float = field(default_factory=time.time)
    last_received: float = field(default_factory=time.time)


@dataclass
class ImageReconstruction:
    """State for reconstructing a single image"""
    metadata: ImageMetadata
    status: ImageStatus = ImageStatus.RECEIVING
    received_symbols: Dict[int, bytes] = field(default_factory=dict)
    decoded_data: Optional[bytes] = None
    error_message: str = ""
    
    @property
    def symbols_received(self) -> int:
        return len(self.received_symbols)
    
    @property
    def progress_percent(self) -> float:
        if self.metadata.num_source_symbols == 0:
            return 0.0
        # With fountain codes, we need ~5-10% overhead typically
        return min(100.0, (self.symbols_received / self.metadata.num_source_symbols) * 100)
    
    @property
    def is_decodable(self) -> bool:
        """Check if we have enough symbols to attempt decoding"""
        # For LT codes, need at least num_source_symbols
        # For RaptorQ, can decode with slight overhead
        return self.symbols_received >= self.metadata.num_source_symbols


class LTDecoder:
    """
    Luby Transform (LT) code decoder
    
    Implements belief propagation decoding for fountain codes.
    """
    
    def __init__(self, num_source_symbols: int, symbol_size: int):
        """
        Initialize LT decoder
        
        Args:
            num_source_symbols: Number of source symbols (K)
            symbol_size: Size of each symbol in bytes
        """
        self.num_source_symbols = num_source_symbols
        self.symbol_size = symbol_size
        
        # Decoded source symbols
        self.decoded: Dict[int, bytes] = {}
        
        # Received encoded symbols: symbol_id -> (data, neighbors)
        self.encoded: Dict[int, Tuple[bytes, Set[int]]] = {}
        
        # Ripple: encoded symbols with degree 1
        self.ripple: Set[int] = set()
    
    def add_symbol(self, symbol_id: int, symbol_data: bytes) -> bool:
        """
        Add a received symbol
        
        Args:
            symbol_id: Symbol identifier
            symbol_data: Symbol data
            
        Returns:
            True if decoding is complete
        """
        if symbol_id in self.encoded:
            return self.is_complete()
        
        # Determine which source symbols this encoded symbol covers
        neighbors = self._get_neighbors(symbol_id)
        
        # Remove already-decoded neighbors by XORing
        data = bytearray(symbol_data)
        remaining = set()
        
        for src_id in neighbors:
            if src_id in self.decoded:
                # XOR out the decoded symbol
                decoded_sym = self.decoded[src_id]
                for i in range(min(len(data), len(decoded_sym))):
                    data[i] ^= decoded_sym[i]
            else:
                remaining.add(src_id)
        
        if len(remaining) == 0:
            # Already fully decoded (redundant symbol)
            return self.is_complete()
        elif len(remaining) == 1:
            # Degree 1 - can decode immediately
            src_id = remaining.pop()
            self._decode_symbol(src_id, bytes(data))
            return self.is_complete()
        else:
            # Store for later processing
            self.encoded[symbol_id] = (bytes(data), remaining)
            return self.is_complete()
    
    def _decode_symbol(self, src_id: int, data: bytes):
        """Decode a source symbol and propagate"""
        if src_id in self.decoded:
            return
        
        self.decoded[src_id] = data
        
        # Queue of sources to decode (to avoid recursive issues)
        to_decode = []
        
        # Propagate to all encoded symbols that reference this source
        to_remove = []
        
        for enc_id, (enc_data, neighbors) in list(self.encoded.items()):
            if src_id in neighbors:
                # XOR out the newly decoded symbol
                new_data = bytearray(enc_data)
                for i in range(min(len(new_data), len(data))):
                    new_data[i] ^= data[i]
                
                # Create new set to avoid modifying while iterating
                new_neighbors = neighbors.copy()
                new_neighbors.discard(src_id)
                
                if len(new_neighbors) == 0:
                    to_remove.append(enc_id)
                elif len(new_neighbors) == 1:
                    # Can decode another symbol - queue it
                    next_src = next(iter(new_neighbors))
                    to_remove.append(enc_id)
                    to_decode.append((next_src, bytes(new_data)))
                else:
                    self.encoded[enc_id] = (bytes(new_data), new_neighbors)
        
        # Remove processed encoded symbols
        for enc_id in to_remove:
            if enc_id in self.encoded:
                del self.encoded[enc_id]
        
        # Now decode queued symbols (after we've finished updating encoded dict)
        for next_src, next_data in to_decode:
            self._decode_symbol(next_src, next_data)
    
    def _get_neighbors(self, symbol_id: int) -> Set[int]:
        """
        Determine which source symbols an encoded symbol covers
        
        Uses PRNG seeded with symbol_id for reproducible selection.
        MUST match the encoder's algorithm exactly!
        """
        import random
        
        # Encoder uses seed + symbol_id. With seed=0, this is just symbol_id
        rng = random.Random(symbol_id)
        
        # Sample degree using same distribution as encoder
        degree = self._sample_degree_matching_encoder(rng)
        degree = min(degree, self.num_source_symbols)
        
        # Select source symbols using same algorithm as encoder: rng.sample()
        indices = rng.sample(range(self.num_source_symbols), degree)
        return set(indices)
    
    def _sample_degree_matching_encoder(self, rng: 'random.Random') -> int:
        """
        Sample degree from Robust Soliton Distribution
        MUST match encoder's RobustSolitonDistribution.sample() exactly!
        """
        import math
        
        K = self.num_source_symbols
        if K <= 1:
            return 1
        
        # Parameters must match encoder
        c = 0.1
        delta = 0.5
        
        # R as float (encoder does NOT use int(R))
        R = c * math.log(K / delta) * math.sqrt(K)
        
        # Build distribution same as encoder
        rho = [0.0] * (K + 1)
        rho[1] = 1.0 / K
        for d in range(2, K + 1):
            rho[d] = 1.0 / (d * (d - 1))
        
        # Tau calculation - match encoder exactly
        tau = [0.0] * (K + 1)
        threshold = min(int(K / R) if R > 0 else K, K)
        
        for d in range(1, threshold):
            tau[d] = R / (d * K)
        
        if 0 < threshold <= K:
            tau[threshold] = R * math.log(R / delta) / K
        
        # Combine and normalize (same as encoder)
        mu = [rho[d] + tau[d] for d in range(K + 1)]
        total = sum(mu)
        probabilities = [p / total for p in mu]
        
        # Build cumulative distribution
        cumulative = []
        cum = 0.0
        for p in probabilities:
            cum += p
            cumulative.append(cum)
        
        # Sample using binary search (same as encoder)
        r = rng.random()
        low, high = 1, len(cumulative) - 1
        
        while low < high:
            mid = (low + high) // 2
            if cumulative[mid] < r:
                low = mid + 1
            else:
                high = mid
        
        return min(low, K)
    
    def is_complete(self) -> bool:
        """Check if all source symbols are decoded"""
        return len(self.decoded) >= self.num_source_symbols
    
    def get_decoded_data(self) -> Optional[bytes]:
        """Get the fully decoded data"""
        if not self.is_complete():
            return None
        
        # Concatenate source symbols in order
        result = bytearray()
        for i in range(self.num_source_symbols):
            if i in self.decoded:
                result.extend(self.decoded[i])
            else:
                # Missing symbol - shouldn't happen if is_complete() is True
                logger.error(f"Missing source symbol {i}")
                return None
        
        return bytes(result)
    
    @property
    def progress(self) -> float:
        """Get decoding progress as percentage"""
        if self.num_source_symbols == 0:
            return 0.0
        return (len(self.decoded) / self.num_source_symbols) * 100


class RaptorQDecoder:
    """
    RaptorQ decoder wrapper using the raptorq library.
    
    The raptorq Python library API:
    - Encoder.with_defaults(data, symbol_size) -> creates encoder
    - encoder.get_encoded_packets(repair_count) -> returns list of bytes (serialized packets)
    - Decoder.with_defaults(transfer_length, symbol_size) -> creates decoder
    - decoder.decode(packet_bytes) -> returns None until complete, then returns decoded data
    
    The packets are already serialized bytes from the encoder, passed directly to decoder.
    """
    
    def __init__(self, num_source_symbols: int, symbol_size: int, total_size: int):
        """
        Initialize RaptorQ decoder
        
        Args:
            num_source_symbols: Number of source symbols (for progress tracking)
            symbol_size: Size of each symbol (must match encoder)
            total_size: Total size of original data (must match encoder)
        """
        if not RAPTORQ_AVAILABLE:
            raise ImportError("raptorq library required but not available. Install with: pip install raptorq")
        
        self.num_source_symbols = num_source_symbols
        self.symbol_size = symbol_size
        self.total_size = total_size
        self._symbols: Dict[int, bytes] = {}
        self._decoded_data = None
        
        # Create decoder with same parameters as encoder
        # Decoder.with_defaults(transfer_length, max_transmission_unit)
        self._decoder = raptorq.Decoder.with_defaults(total_size, symbol_size)
        logger.info(f"RaptorQ decoder initialized: {total_size} bytes, {symbol_size} byte symbols")
    
    def add_symbol(self, symbol_id: int, symbol_data: bytes) -> bool:
        """
        Add a received symbol
        
        Args:
            symbol_id: Symbol identifier (for tracking/logging)
            symbol_data: Serialized packet bytes from encoder's get_encoded_packets()
        
        Returns:
            True if decoding is complete
        """
        if self._decoded_data is not None:
            return True  # Already complete
        
        # Check for duplicates
        if symbol_id in self._symbols:
            return False
        
        # CRITICAL: Verify symbol_id matches packet header ID
        if len(symbol_data) >= 4:
            header_id = int.from_bytes(symbol_data[:4], 'big')
            if header_id != symbol_id:
                logger.warning(f"MISMATCH! symbol_id={symbol_id} but packet header ID={header_id}")
        
        self._symbols[symbol_id] = symbol_data
        
        # Log first few packets for debugging
        if len(self._symbols) <= 3:
            header_id = int.from_bytes(symbol_data[:4], 'big') if len(symbol_data) >= 4 else -1
            logger.info(f"RaptorQ packet symbol_id={symbol_id}, header_id={header_id}, "
                       f"{len(symbol_data)} bytes")
        elif len(self._symbols) % 20 == 0:
            logger.info(f"RaptorQ progress: {len(self._symbols)} unique packets received")
        
        # Log when we hit the threshold
        if len(self._symbols) == self.num_source_symbols:
            # Extract actual packet IDs from headers
            mismatches = []
            for sid, sdata in sorted(self._symbols.items()):
                if len(sdata) >= 4:
                    pkt_id = int.from_bytes(sdata[:4], 'big')
                    if pkt_id != sid:
                        mismatches.append((sid, pkt_id))
            logger.info(f"RaptorQ: reached K={self.num_source_symbols}, "
                       f"symbol_ids: {sorted(self._symbols.keys())[:5]}...{sorted(self._symbols.keys())[-5:]}")
            if mismatches:
                logger.error(f"RaptorQ: FOUND {len(mismatches)} ID MISMATCHES: {mismatches[:10]}")
            else:
                logger.info(f"RaptorQ: all symbol_ids match packet header IDs ✓")
        
        try:
            # Pass raw packet bytes directly to decoder
            result = self._decoder.decode(symbol_data)
            
            if result is not None:
                self._decoded_data = bytes(result)
                logger.info(f"RaptorQ decode complete after {len(self._symbols)} symbols, "
                           f"got {len(self._decoded_data)} bytes")
                return True
            
            # Log if we're past K and still not decoded
            if len(self._symbols) > self.num_source_symbols and len(self._symbols) <= self.num_source_symbols + 5:
                logger.warning(f"RaptorQ: {len(self._symbols)} packets (K+{len(self._symbols)-self.num_source_symbols}), still not decoded!")
            
            return False
            
        except Exception as e:
            # Log ALL errors to debug
            logger.warning(f"RaptorQ packet {symbol_id} error (len={len(symbol_data)}): {e}")
            return False
    
    def is_complete(self) -> bool:
        """Check if decoding is complete"""
        return self._decoded_data is not None
    
    def get_decoded_data(self) -> Optional[bytes]:
        """Get decoded data, trimmed to original size"""
        if self._decoded_data is not None:
            return self._decoded_data[:self.total_size]
        return None
    
    @property
    def symbols_received(self) -> int:
        return len(self._symbols)
    
    @property
    def progress(self) -> float:
        """Estimate progress based on symbols received vs source symbols"""
        if self._decoded_data is not None:
            return 100.0
        if self.num_source_symbols == 0:
            return 0.0
        # RaptorQ typically needs ~K * 1.02 symbols to decode
        return min(99.0, (len(self._symbols) / self.num_source_symbols) * 100)


class FountainDecoder:
    """
    Manages decoding of multiple images using fountain codes
    """
    
    def __init__(
        self,
        symbol_size: int = 200,
        max_pending: int = 10,
        timeout_sec: float = 300.0,
        on_image_complete: Optional[Callable[[int, bytes, ImageMetadata], None]] = None
    ):
        """
        Initialize fountain decoder
        
        Args:
            symbol_size: Expected symbol size
            max_pending: Maximum pending image reconstructions
            timeout_sec: Timeout for incomplete images
            on_image_complete: Callback when image is fully decoded
        """
        self.symbol_size = symbol_size
        self.max_pending = max_pending
        self.timeout_sec = timeout_sec
        self.on_image_complete = on_image_complete
        
        # Active image reconstructions
        self._images: Dict[int, ImageReconstruction] = {}
        self._decoders: Dict[int, RaptorQDecoder] = {}
        
        # Completed images (keep recent ones)
        self._completed: Dict[int, ImageReconstruction] = {}
        self._max_completed = 100
        
        # Statistics
        self.stats = {
            'images_completed': 0,
            'images_failed': 0,
            'symbols_received': 0,
            'symbols_duplicate': 0,
        }
    
    def add_metadata(self, metadata: ImageMetadata) -> bool:
        """
        Add image metadata (from IMAGE_META packet)
        
        Args:
            metadata: Image metadata
            
        Returns:
            True if accepted
        """
        image_id = metadata.image_id
        
        # Check if already completed
        if image_id in self._completed:
            logger.debug(f"Image {image_id} already completed")
            return False
        
        # Check if already receiving (symbols arrived before metadata)
        if image_id in self._images:
            # Update metadata
            old_meta = self._images[image_id].metadata
            self._images[image_id].metadata = metadata
            
            # If decoder doesn't exist yet, create it now and process buffered symbols
            if image_id not in self._decoders and metadata.num_source_symbols > 0:
                logger.info(
                    f"Late metadata for image {image_id}: creating decoder, "
                    f"{len(self._images[image_id].received_symbols)} symbols buffered"
                )
                self._decoders[image_id] = RaptorQDecoder(
                    metadata.num_source_symbols,
                    metadata.symbol_size,
                    metadata.total_size
                )
                
                # Process any buffered symbols
                for sym_id, sym_data in self._images[image_id].received_symbols.items():
                    complete = self._decoders[image_id].add_symbol(sym_id, sym_data)
                    if complete:
                        result = self._complete_image(image_id)
                        if result:
                            logger.info(f"Image {image_id} completed from buffered symbols!")
                        break
            return True
        
        # Cleanup old incomplete images
        self._cleanup_timeout()
        
        # Check capacity
        if len(self._images) >= self.max_pending:
            # Remove oldest
            oldest_id = min(self._images.keys(), key=lambda x: self._images[x].metadata.first_received)
            self._remove_image(oldest_id, ImageStatus.TIMEOUT)
        
        # Create new reconstruction
        self._images[image_id] = ImageReconstruction(metadata=metadata)
        self._decoders[image_id] = RaptorQDecoder(
            metadata.num_source_symbols,
            metadata.symbol_size,
            metadata.total_size
        )
        
        logger.info(
            f"Started receiving image {image_id}: "
            f"{metadata.total_size} bytes, {metadata.num_source_symbols} symbols"
        )
        return True
    
    def add_symbol(self, image_id: int, symbol_id: int, symbol_data: bytes) -> Optional[bytes]:
        """
        Add a received symbol
        
        Args:
            image_id: Image identifier
            symbol_id: Symbol identifier
            symbol_data: Symbol data
            
        Returns:
            Decoded image data if complete, None otherwise
        """
        self.stats['symbols_received'] += 1
        
        # Check if already completed
        if image_id in self._completed:
            self.stats['symbols_duplicate'] += 1
            return None
        
        # Check if we have metadata
        if image_id not in self._images:
            logger.warning(f"Received symbol for unknown image {image_id}")
            # Create placeholder - will get metadata later
            placeholder_meta = ImageMetadata(
                image_id=image_id,
                total_size=0,
                symbol_size=len(symbol_data),
                num_source_symbols=0,
                checksum=0,
                width=0,
                height=0,
                timestamp=0
            )
            self._images[image_id] = ImageReconstruction(metadata=placeholder_meta)
            # Can't create decoder without knowing num_source_symbols
            return None
        
        # Update timestamp
        self._images[image_id].metadata.last_received = time.time()
        
        # Check if decoder exists
        if image_id not in self._decoders:
            # Store symbol for later
            self._images[image_id].received_symbols[symbol_id] = symbol_data
            return None
        
        # Add to decoder
        decoder = self._decoders[image_id]
        complete = decoder.add_symbol(symbol_id, symbol_data)
        self._images[image_id].received_symbols[symbol_id] = symbol_data
        
        if complete:
            return self._complete_image(image_id)
        
        return None
    
    def _complete_image(self, image_id: int) -> Optional[bytes]:
        """Complete image reconstruction"""
        if image_id not in self._images or image_id not in self._decoders:
            return None
        
        decoder = self._decoders[image_id]
        image_rec = self._images[image_id]
        
        decoded_data = decoder.get_decoded_data()
        if decoded_data is None:
            logger.error(f"Failed to get decoded data for image {image_id}")
            self._remove_image(image_id, ImageStatus.FAILED)
            return None
        
        # Verify checksum if we have metadata
        if image_rec.metadata.checksum != 0:
            from common.crc import crc32
            actual_crc = crc32(decoded_data)
            if actual_crc != image_rec.metadata.checksum:
                logger.error(
                    f"Image {image_id} checksum mismatch: "
                    f"expected {image_rec.metadata.checksum:08x}, got {actual_crc:08x}"
                )
                self._remove_image(image_id, ImageStatus.FAILED)
                self.stats['images_failed'] += 1
                return None
        
        # Success!
        image_rec.status = ImageStatus.COMPLETE
        image_rec.decoded_data = decoded_data
        
        # Move to completed
        self._completed[image_id] = image_rec
        del self._images[image_id]
        del self._decoders[image_id]
        
        self.stats['images_completed'] += 1
        
        logger.info(
            f"Image {image_id} complete: {len(decoded_data)} bytes, "
            f"{image_rec.symbols_received} symbols"
        )
        
        # Callback
        if self.on_image_complete:
            try:
                self.on_image_complete(image_id, decoded_data, image_rec.metadata)
            except Exception as e:
                logger.error(f"Image complete callback error: {e}")
        
        # Trim completed list
        while len(self._completed) > self._max_completed:
            oldest = min(self._completed.keys())
            del self._completed[oldest]
        
        return decoded_data
    
    def _remove_image(self, image_id: int, status: ImageStatus):
        """Remove an image reconstruction"""
        if image_id in self._images:
            self._images[image_id].status = status
            del self._images[image_id]
        if image_id in self._decoders:
            del self._decoders[image_id]
    
    def _cleanup_timeout(self):
        """Remove timed-out image reconstructions"""
        now = time.time()
        to_remove = []
        
        for image_id, image_rec in self._images.items():
            if now - image_rec.metadata.last_received > self.timeout_sec:
                to_remove.append(image_id)
                logger.warning(f"Image {image_id} timed out")
        
        for image_id in to_remove:
            self._remove_image(image_id, ImageStatus.TIMEOUT)
            self.stats['images_failed'] += 1
    
    def get_status(self) -> Dict:
        """Get decoder status"""
        self._cleanup_timeout()
        
        pending = []
        for image_id, image_rec in self._images.items():
            decoder = self._decoders.get(image_id)
            pending.append({
                'image_id': image_id,
                'progress': decoder.progress if decoder else 0,
                'symbols_received': image_rec.symbols_received,
                'symbols_needed': image_rec.metadata.num_source_symbols,
                'elapsed_sec': time.time() - image_rec.metadata.first_received,
            })
        
        return {
            'pending_images': len(self._images),
            'completed_images': self.stats['images_completed'],
            'failed_images': self.stats['images_failed'],
            'symbols_received': self.stats['symbols_received'],
            'pending': pending,
        }
    
    def get_pending_progress(self) -> List[Dict]:
        """Get progress of all pending images"""
        result = []
        for image_id, image_rec in self._images.items():
            decoder = self._decoders.get(image_id)
            result.append({
                'image_id': image_id,
                'width': image_rec.metadata.width,
                'height': image_rec.metadata.height,
                'progress': decoder.progress if decoder else 0,
                'symbols': image_rec.symbols_received,
                'total_symbols': image_rec.metadata.num_source_symbols,
            })
        return result
    
    def get_completed_image(self, image_id: int) -> Optional[bytes]:
        """Get a completed image's data"""
        if image_id in self._completed:
            return self._completed[image_id].decoded_data
        return None
