"""
RaptorHab Ground Station - Offline Map Tile Server
Serves OSM tiles from MBTiles files for offline operation

MBTiles is a SQLite database containing pre-rendered map tiles.
This module provides tile serving with transparent fallback to online tiles.

DOWNLOADING OFFLINE MAPS:
For worldwide coverage at reasonable size, download from:
- OpenMapTiles: https://openmaptiles.org/downloads/planet/
- MapTiler: https://data.maptiler.com/downloads/planet/

Recommended zoom levels for HAB tracking:
- Zoom 0-6: Worldwide overview (~50MB)
- Zoom 0-8: Good for tracking (~200MB)  
- Zoom 0-10: Detailed tracking (~2GB)
- Zoom 0-12: Very detailed (~20GB)

For smaller regional files, use tools like:
- tilemaker: https://github.com/systemed/tilemaker
- osmium + tippecanoe
- Download from https://protomaps.com/downloads/
"""

import os
import sqlite3
import logging
import gzip
import io
from typing import Optional, Dict, Tuple, List, Any
from threading import Lock
from pathlib import Path

logger = logging.getLogger(__name__)


class MBTilesReader:
    """
    Reader for MBTiles format (SQLite database with map tiles)
    
    MBTiles schema:
    - metadata table: name, value pairs (name, description, format, bounds, etc.)
    - tiles table: zoom_level, tile_column, tile_row, tile_data
    
    Note: MBTiles uses TMS (Tile Map Service) y-coordinate which is flipped
    from the XYZ/Slippy map convention used by Leaflet/OSM.
    """
    
    def __init__(self, mbtiles_path: str):
        """
        Initialize MBTiles reader
        
        Args:
            mbtiles_path: Path to .mbtiles file
        """
        self.path = mbtiles_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = Lock()
        self._metadata: Dict[str, str] = {}
        self._is_valid = False
        
        if os.path.exists(mbtiles_path):
            self._init_connection()
    
    def _init_connection(self):
        """Initialize database connection and load metadata"""
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            
            # Load metadata
            cursor = self._conn.execute("SELECT name, value FROM metadata")
            for row in cursor:
                self._metadata[row['name']] = row['value']
            
            self._is_valid = True
            logger.info(f"Loaded MBTiles: {self.path}")
            logger.info(f"  Name: {self._metadata.get('name', 'Unknown')}")
            logger.info(f"  Format: {self._metadata.get('format', 'Unknown')}")
            logger.info(f"  Bounds: {self._metadata.get('bounds', 'Unknown')}")
            
        except Exception as e:
            logger.error(f"Failed to open MBTiles {self.path}: {e}")
            self._is_valid = False
    
    @property
    def is_valid(self) -> bool:
        return self._is_valid
    
    @property
    def metadata(self) -> Dict[str, str]:
        return self._metadata.copy()
    
    @property
    def format(self) -> str:
        """Get tile format (png, jpg, pbf, webp)"""
        return self._metadata.get('format', 'png')
    
    @property
    def min_zoom(self) -> int:
        """Get minimum zoom level"""
        return int(self._metadata.get('minzoom', 0))
    
    @property
    def max_zoom(self) -> int:
        """Get maximum zoom level"""
        return int(self._metadata.get('maxzoom', 18))
    
    @property
    def bounds(self) -> Optional[Tuple[float, float, float, float]]:
        """Get bounds as (west, south, east, north)"""
        bounds_str = self._metadata.get('bounds')
        if bounds_str:
            parts = [float(x) for x in bounds_str.split(',')]
            if len(parts) == 4:
                return tuple(parts)
        return None
    
    def _tms_to_xyz(self, z: int, y: int) -> int:
        """
        Convert TMS y-coordinate to XYZ/Slippy y-coordinate
        MBTiles uses TMS where y=0 is at the bottom
        Leaflet/OSM use XYZ where y=0 is at the top
        """
        return (1 << z) - 1 - y
    
    def _xyz_to_tms(self, z: int, y: int) -> int:
        """Convert XYZ y-coordinate to TMS y-coordinate"""
        return (1 << z) - 1 - y
    
    def get_tile(self, z: int, x: int, y: int) -> Optional[bytes]:
        """
        Get tile data for given coordinates (XYZ/Slippy convention)
        
        Args:
            z: Zoom level
            x: Tile column
            y: Tile row (XYZ convention - 0 at top)
            
        Returns:
            Tile image data as bytes, or None if not found
        """
        if not self._is_valid:
            return None
        
        # Convert XYZ y to TMS y
        tms_y = self._xyz_to_tms(z, y)
        
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                    (z, x, tms_y)
                )
                row = cursor.fetchone()
                if row:
                    data = row['tile_data']
                    
                    # Check if data is gzip compressed (common for vector tiles)
                    if data[:2] == b'\x1f\x8b':
                        try:
                            data = gzip.decompress(data)
                        except:
                            pass  # Not actually gzipped or decompression failed
                    
                    return data
                    
            except Exception as e:
                logger.debug(f"Tile fetch error z={z} x={x} y={y}: {e}")
        
        return None
    
    def has_tile(self, z: int, x: int, y: int) -> bool:
        """Check if tile exists without fetching data"""
        if not self._is_valid:
            return False
        
        tms_y = self._xyz_to_tms(z, y)
        
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1",
                    (z, x, tms_y)
                )
                return cursor.fetchone() is not None
            except:
                return False
    
    def get_tile_count(self) -> int:
        """Get total number of tiles in the database"""
        if not self._is_valid:
            return 0
        
        with self._lock:
            try:
                cursor = self._conn.execute("SELECT COUNT(*) FROM tiles")
                return cursor.fetchone()[0]
            except:
                return 0
    
    def get_zoom_stats(self) -> Dict[int, int]:
        """Get tile count per zoom level"""
        if not self._is_valid:
            return {}
        
        stats = {}
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "SELECT zoom_level, COUNT(*) as count FROM tiles GROUP BY zoom_level ORDER BY zoom_level"
                )
                for row in cursor:
                    stats[row['zoom_level']] = row['count']
            except:
                pass
        
        return stats
    
    def close(self):
        """Close database connection"""
        if self._conn:
            self._conn.close()
            self._conn = None
            self._is_valid = False


class OfflineMapManager:
    """
    Manages multiple MBTiles files and provides unified tile access
    Supports fallback between different map sources
    """
    
    def __init__(self, maps_dir: str):
        """
        Initialize offline map manager
        
        Args:
            maps_dir: Directory containing .mbtiles files
        """
        self.maps_dir = maps_dir
        self._readers: Dict[str, MBTilesReader] = {}
        self._default_reader: Optional[MBTilesReader] = None
        self._lock = Lock()
        
        # Create maps directory if needed
        os.makedirs(maps_dir, exist_ok=True)
        
        # Scan for available MBTiles files
        self._scan_maps()
    
    def _scan_maps(self):
        """Scan maps directory for .mbtiles files"""
        if not os.path.exists(self.maps_dir):
            return
        
        for filename in os.listdir(self.maps_dir):
            if filename.endswith('.mbtiles'):
                filepath = os.path.join(self.maps_dir, filename)
                name = filename[:-8]  # Remove .mbtiles extension
                
                reader = MBTilesReader(filepath)
                if reader.is_valid:
                    self._readers[name] = reader
                    logger.info(f"Loaded offline map: {name}")
    
    def set_default(self, name: str) -> bool:
        """Set the default map source"""
        if name in self._readers:
            self._default_reader = self._readers[name]
            logger.info(f"Set default offline map: {name}")
            return True
        
        # Try loading by filename
        if not name.endswith('.mbtiles'):
            name += '.mbtiles'
        
        filepath = os.path.join(self.maps_dir, name)
        if os.path.exists(filepath):
            map_name = name[:-8]
            reader = MBTilesReader(filepath)
            if reader.is_valid:
                self._readers[map_name] = reader
                self._default_reader = reader
                logger.info(f"Loaded and set default offline map: {map_name}")
                return True
        
        logger.warning(f"Offline map not found: {name}")
        return False
    
    @property
    def available_maps(self) -> List[str]:
        """Get list of available map names"""
        return list(self._readers.keys())
    
    @property
    def has_offline_maps(self) -> bool:
        """Check if any offline maps are available"""
        return len(self._readers) > 0
    
    def get_tile(self, z: int, x: int, y: int, map_name: Optional[str] = None) -> Optional[Tuple[bytes, str]]:
        """
        Get tile from offline maps
        
        Args:
            z: Zoom level
            x: Tile column
            y: Tile row (XYZ convention)
            map_name: Specific map to use (None for default)
            
        Returns:
            Tuple of (tile_data, format) or None if not found
        """
        reader = None
        
        if map_name and map_name in self._readers:
            reader = self._readers[map_name]
        elif self._default_reader:
            reader = self._default_reader
        elif self._readers:
            # Use first available
            reader = next(iter(self._readers.values()))
        
        if reader:
            data = reader.get_tile(z, x, y)
            if data:
                return (data, reader.format)
        
        return None
    
    def get_status(self) -> Dict[str, Any]:
        """Get status of offline maps"""
        status = {
            'maps_directory': self.maps_dir,
            'available': self.has_offline_maps,
            'maps': {}
        }
        
        for name, reader in self._readers.items():
            status['maps'][name] = {
                'name': reader.metadata.get('name', name),
                'format': reader.format,
                'min_zoom': reader.min_zoom,
                'max_zoom': reader.max_zoom,
                'bounds': reader.bounds,
                'tile_count': reader.get_tile_count(),
                'is_default': reader == self._default_reader
            }
        
        return status
    
    def close(self):
        """Close all readers"""
        for reader in self._readers.values():
            reader.close()
        self._readers.clear()
        self._default_reader = None


def get_content_type(format: str) -> str:
    """Get MIME content type for tile format"""
    formats = {
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'webp': 'image/webp',
        'pbf': 'application/x-protobuf',
        'mvt': 'application/vnd.mapbox-vector-tile',
    }
    return formats.get(format.lower(), 'application/octet-stream')
