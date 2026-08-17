"""
RaptorHab Fountain Code Encoder
Luby Transform (LT) codes implementation for rateless image transmission
"""

import math
import random
import logging
from dataclasses import dataclass
from typing import List, Optional, Generator, Tuple

logger = logging.getLogger(__name__)

# Try to import raptorq for optimal fountain codes
try:
    import raptorq
    RAPTORQ_AVAILABLE = True
except ImportError:
    RAPTORQ_AVAILABLE = False
    logger.info("raptorq not available, using LT codes")


@dataclass
class EncodingSession:
    """State for an active fountain encoding session"""
    data: bytes
    symbol_size: int
    num_source_symbols: int
    symbols_generated: int = 0
    random_seed: int = 0
    
    @property
    def overhead_percent(self) -> float:
        """Calculate current overhead percentage"""
        if self.num_source_symbols == 0:
            return 0.0
        return ((self.symbols_generated / self.num_source_symbols) - 1) * 100


class RobustSolitonDistribution:
    """
    Robust Soliton Distribution for LT codes
    Determines how many source symbols to combine for each encoding symbol
    """
    
    def __init__(self, k: int, c: float = 0.1, delta: float = 0.5):
        """
        Initialize distribution
        
        Args:
            k: Number of source symbols
            c: Constant for robust component (typically 0.03 - 0.5)
            delta: Failure probability bound (typically 0.05 - 0.5)
        """
        self.k = k
        self.c = c
        self.delta = delta
        
        # Calculate R (expected ripple size)
        self.R = c * math.log(k / delta) * math.sqrt(k)
        
        # Pre-compute probability distribution
        self._compute_distribution()
    
    def _compute_distribution(self):
        """Compute the robust soliton probability distribution"""
        k = self.k
        R = self.R
        
        # Ideal soliton distribution
        rho = [0.0] * (k + 1)
        rho[1] = 1.0 / k
        for d in range(2, k + 1):
            rho[d] = 1.0 / (d * (d - 1))
        
        # Robust component (tau)
        tau = [0.0] * (k + 1)
        threshold = min(int(k / R) if R > 0 else k, k)  # Clamp to valid range
        
        for d in range(1, threshold):
            tau[d] = R / (d * k)
        
        if 0 < threshold <= k:
            tau[threshold] = R * math.log(R / self.delta) / k
        
        # Combine into robust soliton distribution (mu)
        mu = [rho[d] + tau[d] for d in range(k + 1)]
        
        # Normalize
        total = sum(mu)
        self.probabilities = [p / total for p in mu]
        
        # Build cumulative distribution for sampling
        self.cumulative = []
        cum = 0.0
        for p in self.probabilities:
            cum += p
            self.cumulative.append(cum)
    
    def sample(self, rng: random.Random) -> int:
        """
        Sample a degree from the distribution
        
        Args:
            rng: Random number generator
            
        Returns:
            Degree (number of source symbols to combine)
        """
        r = rng.random()
        
        # Binary search in cumulative distribution
        low, high = 1, len(self.cumulative) - 1
        
        while low < high:
            mid = (low + high) // 2
            if self.cumulative[mid] < r:
                low = mid + 1
            else:
                high = mid
        
        return min(low, self.k)


class LTEncoder:
    """Luby Transform (LT) codes encoder - optimized for continuous transmission"""
    
    def __init__(self, data: bytes, symbol_size: int = 200, seed: int = None):
        """
        Initialize LT encoder
        
        Args:
            data: Data to encode
            symbol_size: Size of each symbol in bytes
            seed: Random seed for reproducibility
        """
        self.data = data
        self.symbol_size = symbol_size
        self.seed = seed or random.randint(0, 2**32 - 1)
        
        # Pad data to multiple of symbol_size
        padding = (symbol_size - (len(data) % symbol_size)) % symbol_size
        self.padded_data = data + bytes(padding)
        
        # Split into source symbols
        self.num_source_symbols = len(self.padded_data) // symbol_size
        self.source_symbols = [
            self.padded_data[i * symbol_size:(i + 1) * symbol_size]
            for i in range(self.num_source_symbols)
        ]
        
        # Initialize distribution
        self.distribution = RobustSolitonDistribution(
            self.num_source_symbols,
            c=0.1,
            delta=0.5
        )
        
        # RNG for encoding
        self.rng = random.Random(self.seed)
        self.symbols_generated = 0
        
        # Pre-generate symbols for faster transmission
        self._symbol_cache = []
        self._cache_size = 0
        
        logger.debug(
            f"LT encoder: {len(data)} bytes -> {self.num_source_symbols} symbols "
            f"(symbol_size={symbol_size}, seed={self.seed})"
        )
    
    def _xor_symbols(self, indices: list) -> bytes:
        """Fast XOR of multiple source symbols using int operations"""
        if len(indices) == 1:
            return self.source_symbols[indices[0]]
        
        # Process in 8-byte chunks for speed
        result = bytearray(self.symbol_size)
        
        for idx in indices:
            source = self.source_symbols[idx]
            # XOR in larger chunks when possible
            for i in range(0, self.symbol_size - 7, 8):
                # Read 8 bytes as int, XOR, write back
                val = int.from_bytes(result[i:i+8], 'little')
                src_val = int.from_bytes(source[i:i+8], 'little')
                val ^= src_val
                result[i:i+8] = val.to_bytes(8, 'little')
            
            # Handle remaining bytes
            remainder = self.symbol_size % 8
            if remainder:
                start = self.symbol_size - remainder
                for i in range(start, self.symbol_size):
                    result[i] ^= source[i]
        
        return bytes(result)
    
    def _generate_one_symbol(self, symbol_id: int) -> bytes:
        """Generate a single symbol by ID"""
        # Use symbol_id as seed for this symbol's RNG
        symbol_rng = random.Random(self.seed + symbol_id)
        
        # Sample degree from robust soliton distribution
        degree = self.distribution.sample(symbol_rng)
        degree = min(degree, self.num_source_symbols)
        
        # Select source symbols to combine
        indices = symbol_rng.sample(range(self.num_source_symbols), degree)
        
        # XOR selected source symbols (optimized)
        return self._xor_symbols(indices)
    
    def _ensure_cache(self, count: int = 50):
        """Pre-generate symbols into cache"""
        while len(self._symbol_cache) < count:
            symbol_id = self._cache_size
            symbol_data = self._generate_one_symbol(symbol_id)
            self._symbol_cache.append((symbol_id, symbol_data))
            self._cache_size += 1
    
    def generate_symbol(self) -> Tuple[int, bytes]:
        """
        Generate the next encoding symbol (from cache for speed)
        
        Returns:
            Tuple of (symbol_id, symbol_data)
        """
        # Ensure we have symbols in cache
        if not self._symbol_cache:
            self._ensure_cache(50)  # Pre-generate 50 symbols
        
        symbol_id, symbol_data = self._symbol_cache.pop(0)
        self.symbols_generated += 1
        
        # Refill cache in background if running low
        if len(self._symbol_cache) < 10:
            self._ensure_cache(50)
        
        return symbol_id, symbol_data
    
    def generate_symbols(self, count: int) -> Generator[Tuple[int, bytes], None, None]:
        """
        Generate multiple encoding symbols
        
        Args:
            count: Number of symbols to generate
            
        Yields:
            Tuples of (symbol_id, symbol_data)
        """
        for _ in range(count):
            yield self.generate_symbol()
    
    def get_recommended_symbol_count(self, overhead_percent: float = 25) -> int:
        """
        Get recommended number of symbols for successful decoding
        
        Args:
            overhead_percent: Desired overhead percentage
            
        Returns:
            Number of symbols to generate
        """
        return int(self.num_source_symbols * (1 + overhead_percent / 100))


class RaptorQEncoder:
    """RaptorQ encoder wrapper (if available) - pre-generates all packets"""
    
    def __init__(self, data: bytes, symbol_size: int = 200):
        """
        Initialize RaptorQ encoder
        
        Args:
            data: Data to encode
            symbol_size: Size of each symbol in bytes
        """
        if not RAPTORQ_AVAILABLE:
            raise ImportError("raptorq package not available")
        
        self.data = data
        self.symbol_size = symbol_size
        
        # Create encoder
        self.encoder = raptorq.Encoder.with_defaults(data, symbol_size)
        
        # Calculate num_source_symbols first
        self.num_source_symbols = len(data) // symbol_size + (1 if len(data) % symbol_size else 0)
        
        # Pre-generate ALL packets upfront (source + 50% repair overhead)
        # This avoids regeneration delays during transmission
        repair_packets_needed = max(self.num_source_symbols // 2, 20)
        self.packets = self.encoder.get_encoded_packets(repair_packets_needed)
        self.symbols_generated = 0
        
        logger.debug(
            f"RaptorQ encoder: {len(data)} bytes -> {self.num_source_symbols} source symbols, "
            f"{len(self.packets)} total packets (pre-generated)"
        )
    
    def generate_symbol(self) -> Tuple[int, bytes]:
        """
        Generate the next encoding symbol
        
        Returns:
            Tuple of (symbol_id, symbol_data)
        """
        if self.symbols_generated >= len(self.packets):
            # Generate more repair packets
            # get_encoded_packets returns source + repair, so we need to skip source packets
            current_repair_count = len(self.packets) - self.num_source_symbols
            new_repair_count = current_repair_count + self.num_source_symbols  # Double repair packets
            all_packets = self.encoder.get_encoded_packets(new_repair_count)
            # Only add the NEW repair packets (skip source packets we already have)
            new_packets = all_packets[len(self.packets):]
            self.packets.extend(new_packets)
            logger.debug(f"RaptorQ: extended to {len(self.packets)} packets")
        
        packet = self.packets[self.symbols_generated]
        symbol_id = self.symbols_generated
        self.symbols_generated += 1
        
        # raptorq returns bytes directly, not objects with serialize()
        if isinstance(packet, bytes):
            return symbol_id, packet
        return symbol_id, packet.serialize()
    
    def generate_symbols(self, count: int) -> Generator[Tuple[int, bytes], None, None]:
        """Generate multiple encoding symbols"""
        for _ in range(count):
            yield self.generate_symbol()
    
    def get_recommended_symbol_count(self, overhead_percent: float = 25) -> int:
        """Get recommended number of symbols"""
        return int(self.num_source_symbols * (1 + overhead_percent / 100))


class FountainEncoder:
    """
    Fountain code encoder facade
    Uses RaptorQ if available, falls back to LT codes
    """
    
    def __init__(
        self,
        data: bytes,
        symbol_size: int = 200,
        seed: int = None,
        prefer_raptorq: bool = True
    ):
        """
        Initialize fountain encoder
        
        Args:
            data: Data to encode
            symbol_size: Size of each symbol in bytes
            seed: Random seed (for LT codes)
            prefer_raptorq: Prefer RaptorQ if available
        """
        self.data = data
        self.symbol_size = symbol_size
        self.original_size = len(data)
        
        # Choose encoder
        if prefer_raptorq and RAPTORQ_AVAILABLE:
            try:
                self._encoder = RaptorQEncoder(data, symbol_size)
                self._encoder_type = "RaptorQ"
            except Exception as e:
                logger.warning(f"RaptorQ failed, using LT codes: {e}")
                self._encoder = LTEncoder(data, symbol_size, seed)
                self._encoder_type = "LT"
        else:
            self._encoder = LTEncoder(data, symbol_size, seed)
            self._encoder_type = "LT"
        
        logger.info(f"Using {self._encoder_type} fountain encoder")
    
    @property
    def num_source_symbols(self) -> int:
        """Number of source symbols"""
        return self._encoder.num_source_symbols
    
    @property
    def symbols_generated(self) -> int:
        """Number of symbols generated so far"""
        return self._encoder.symbols_generated
    
    def generate_symbol(self) -> Tuple[int, bytes]:
        """
        Generate the next encoding symbol
        
        Returns:
            Tuple of (symbol_id, symbol_data)
        """
        return self._encoder.generate_symbol()
    
    def generate_symbols(self, count: int) -> Generator[Tuple[int, bytes], None, None]:
        """
        Generate multiple encoding symbols
        
        Args:
            count: Number of symbols to generate
            
        Yields:
            Tuples of (symbol_id, symbol_data)
        """
        yield from self._encoder.generate_symbols(count)
    
    def get_recommended_symbol_count(self, overhead_percent: float = 25) -> int:
        """
        Get recommended number of symbols for successful decoding
        
        Args:
            overhead_percent: Desired overhead percentage
            
        Returns:
            Number of symbols to generate
        """
        return self._encoder.get_recommended_symbol_count(overhead_percent)
    
    def create_session(self) -> EncodingSession:
        """
        Create an encoding session object
        
        Returns:
            EncodingSession with current state
        """
        seed = getattr(self._encoder, 'seed', 0)
        return EncodingSession(
            data=self.data,
            symbol_size=self.symbol_size,
            num_source_symbols=self.num_source_symbols,
            symbols_generated=self.symbols_generated,
            random_seed=seed
        )


def encode_image(
    image_data: bytes,
    symbol_size: int = 200,
    overhead_percent: float = 25
) -> Tuple[List[Tuple[int, bytes]], int, int]:
    """
    Encode image data for transmission
    
    Args:
        image_data: Raw image data (WebP bytes)
        symbol_size: Size of each symbol
        overhead_percent: Extra symbols to generate
        
    Returns:
        Tuple of (list of (symbol_id, symbol_data), num_source_symbols, total_size)
    """
    encoder = FountainEncoder(image_data, symbol_size)
    
    num_symbols = encoder.get_recommended_symbol_count(overhead_percent)
    symbols = list(encoder.generate_symbols(num_symbols))
    
    logger.info(
        f"Encoded {len(image_data)} bytes into {len(symbols)} symbols "
        f"({encoder.num_source_symbols} source + {len(symbols) - encoder.num_source_symbols} repair)"
    )
    
    return symbols, encoder.num_source_symbols, len(image_data)
