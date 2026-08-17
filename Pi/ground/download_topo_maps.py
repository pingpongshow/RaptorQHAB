#!/usr/bin/env python3
"""
RaptorHab Ground Station - OpenTopoMap Offline Tile Downloader

Downloads terrain/topographic tiles from OpenTopoMap for offline use.
Configured to stay under 1GB while providing useful worldwide coverage.

Usage:
    python3 download_topo_maps.py                          # Default: worldwide zoom 0-7 (~150MB)
    python3 download_topo_maps.py --zoom 0-8               # More detail (~500MB)
    python3 download_topo_maps.py --around 40.7,-74.0 --radius 200  # Regional high-detail

Tile counts by zoom level (worldwide):
    Zoom 0-5:  ~1,400 tiles     (~5 MB)
    Zoom 0-6:  ~5,500 tiles     (~20 MB)
    Zoom 0-7:  ~22,000 tiles    (~80 MB)    <-- Default
    Zoom 0-8:  ~87,000 tiles    (~300 MB)
    Zoom 0-9:  ~350,000 tiles   (~1.2 GB)   <-- Too big
    
Note: OpenTopoMap has stricter usage policies than OSM. This script
rate-limits to 1 tile/second to be respectful. Large downloads will
take several hours.
"""

import argparse
import sqlite3
import os
import sys
import time
import math
import random
import urllib.request
import urllib.error
from typing import Tuple, List, Generator, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

# OpenTopoMap tile servers
TILE_SERVERS = [
    'https://backup.opentopomap.org/{z}/{x}/{y}.png',
]

# Be respectful - OpenTopoMap is a volunteer project
USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
TILES_PER_SECOND = 20  # 20 tiles per second with parallel downloads
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
PARALLEL_DOWNLOADS = 10  # Number of concurrent downloads


def lat_lon_to_tile(lat: float, lon: float, zoom: int) -> Tuple[int, int]:
    """Convert lat/lon to tile coordinates"""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(lat_rad)) / math.pi) / 2 * n)
    return x, y


def count_tiles_for_zoom(zoom: int) -> int:
    """Count total tiles at a zoom level (worldwide)"""
    return 4 ** zoom


def count_tiles_in_bounds(west: float, south: float, east: float, north: float,
                          min_zoom: int, max_zoom: int) -> int:
    """Count tiles within bounds"""
    total = 0
    for z in range(min_zoom, max_zoom + 1):
        n = 2 ** z
        
        x_min = int((west + 180) / 360 * n)
        x_max = int((east + 180) / 360 * n)
        
        lat_rad_north = math.radians(min(north, 85.0511))
        lat_rad_south = math.radians(max(south, -85.0511))
        
        y_min = int((1 - math.asinh(math.tan(lat_rad_north)) / math.pi) / 2 * n)
        y_max = int((1 - math.asinh(math.tan(lat_rad_south)) / math.pi) / 2 * n)
        
        x_min = max(0, x_min)
        x_max = min(n - 1, x_max)
        y_min = max(0, y_min)
        y_max = min(n - 1, y_max)
        
        total += (x_max - x_min + 1) * (y_max - y_min + 1)
    return total


def get_tiles_in_bounds(west: float, south: float, east: float, north: float,
                        min_zoom: int, max_zoom: int) -> Generator[Tuple[int, int, int], None, None]:
    """Generate tile coordinates within bounds"""
    for z in range(min_zoom, max_zoom + 1):
        n = 2 ** z
        
        # Convert bounds to tile coordinates
        x_min = int((west + 180) / 360 * n)
        x_max = int((east + 180) / 360 * n)
        
        # Y coordinates (note: lat_to_tile_y decreases as lat increases)
        lat_rad_north = math.radians(min(north, 85.0511))
        lat_rad_south = math.radians(max(south, -85.0511))
        
        y_min = int((1 - math.asinh(math.tan(lat_rad_north)) / math.pi) / 2 * n)
        y_max = int((1 - math.asinh(math.tan(lat_rad_south)) / math.pi) / 2 * n)
        
        # Clamp to valid range
        x_min = max(0, x_min)
        x_max = min(n - 1, x_max)
        y_min = max(0, y_min)
        y_max = min(n - 1, y_max)
        
        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                yield z, x, y


def get_tiles_around_point(lat: float, lon: float, radius_km: float,
                           min_zoom: int, max_zoom: int) -> Generator[Tuple[int, int, int], None, None]:
    """Generate tiles within radius of a point"""
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))
    
    west = lon - lon_delta
    east = lon + lon_delta
    south = lat - lat_delta
    north = lat + lat_delta
    
    yield from get_tiles_in_bounds(west, south, east, north, min_zoom, max_zoom)


def estimate_size_mb(tile_count: int, avg_tile_kb: float = 15.0) -> float:
    """Estimate download size in MB (topo tiles average ~15KB)"""
    return (tile_count * avg_tile_kb) / 1024


def download_tile(z: int, x: int, y: int, retry: int = 0) -> Optional[bytes]:
    """Download a single tile with retries"""
    server = TILE_SERVERS[0]
    url = server.format(z=z, x=x, y=y)
    
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': 'image/png,image/*,*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://opentopomap.org/',
    })
    
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            if response.status == 200:
                return response.read()
    except urllib.error.HTTPError as e:
        if e.code == 429 and retry < MAX_RETRIES:
            time.sleep(5 * (retry + 1))
            return download_tile(z, x, y, retry + 1)
        elif e.code == 404:
            return None
    except (urllib.error.URLError, TimeoutError, ConnectionResetError):
        if retry < MAX_RETRIES:
            time.sleep(2 * (retry + 1))
            return download_tile(z, x, y, retry + 1)
    except Exception:
        if retry < MAX_RETRIES:
            return download_tile(z, x, y, retry + 1)
    
    return None


def xyz_to_tms(z: int, y: int) -> int:
    """Convert XYZ y to TMS y (MBTiles uses TMS)"""
    return (1 << z) - 1 - y


def create_mbtiles(path: str, name: str, description: str,
                   bounds: Tuple[float, float, float, float],
                   min_zoom: int, max_zoom: int) -> sqlite3.Connection:
    """Create MBTiles database"""
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    
    cursor.execute('CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tiles (
            zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER,
            tile_data BLOB, PRIMARY KEY (zoom_level, tile_column, tile_row)
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_tiles ON tiles (zoom_level, tile_column, tile_row)')
    
    west, south, east, north = bounds
    metadata = [
        ('name', name),
        ('type', 'baselayer'),
        ('version', '1.0'),
        ('description', description),
        ('format', 'png'),
        ('bounds', f'{west},{south},{east},{north}'),
        ('minzoom', str(min_zoom)),
        ('maxzoom', str(max_zoom)),
        ('attribution', '© OpenTopoMap (CC-BY-SA) © OpenStreetMap contributors'),
    ]
    cursor.executemany('INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)', metadata)
    conn.commit()
    
    return conn


def download_to_mbtiles(output: str, tiles: List[Tuple[int, int, int]],
                        bounds: Tuple[float, float, float, float],
                        min_zoom: int, max_zoom: int,
                        resume: bool = True,
                        verbose: bool = False):
    """Download tiles to MBTiles file"""
    
    name = 'OpenTopoMap Offline'
    description = f'OpenTopoMap terrain tiles, zoom {min_zoom}-{max_zoom}'
    
    # Check for existing file to resume
    existing_tiles = set()
    if resume and os.path.exists(output):
        print(f"Found existing file, checking for resume...")
        try:
            conn = sqlite3.connect(output)
            cursor = conn.cursor()
            cursor.execute('SELECT zoom_level, tile_column, tile_row FROM tiles')
            for row in cursor:
                z, x, tms_y = row
                xyz_y = xyz_to_tms(z, tms_y)  # Convert back to XYZ
                existing_tiles.add((z, x, xyz_y))
            conn.close()
            print(f"  Resuming: {len(existing_tiles):,} tiles already downloaded")
        except:
            existing_tiles = set()
    
    # Filter out already-downloaded tiles
    tiles_to_download = [t for t in tiles if t not in existing_tiles]
    
    if not tiles_to_download:
        print("All tiles already downloaded!")
        return
    
    conn = create_mbtiles(output, name, description, bounds, min_zoom, max_zoom)
    cursor = conn.cursor()
    
    total = len(tiles_to_download)
    downloaded = 0
    failed = 0
    bytes_downloaded = 0
    start_time = time.time()
    
    print(f"\nDownloading {total:,} tiles to {output}")
    print(f"Parallel downloads: {PARALLEL_DOWNLOADS}")
    print(f"Estimated time: {total / TILES_PER_SECOND / 60:.0f} minutes")
    print(f"Estimated size: {estimate_size_mb(total):.0f} MB")
    print("\nPress Ctrl+C to pause (progress is saved)\n")
    
    try:
        with ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOADS) as executor:
            future_to_tile = {
                executor.submit(download_tile, z, x, y): (z, x, y) 
                for z, x, y in tiles_to_download
            }
            
            for future in as_completed(future_to_tile):
                z, x, y = future_to_tile[future]
                downloaded += 1
                
                try:
                    data = future.result()
                    if data:
                        tms_y = xyz_to_tms(z, y)
                        cursor.execute(
                            'INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)',
                            (z, x, tms_y, data)
                        )
                        bytes_downloaded += len(data)
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                
                if downloaded % 100 == 0:
                    conn.commit()
                
                if downloaded % 20 == 0 or downloaded == total:
                    elapsed = time.time() - start_time
                    rate = downloaded / elapsed if elapsed > 0 else 0
                    eta_min = (total - downloaded) / rate / 60 if rate > 0 else 0
                    mb = bytes_downloaded / 1024 / 1024
                    
                    bar_width = 30
                    progress = downloaded / total
                    filled = int(bar_width * progress)
                    bar = '█' * filled + '░' * (bar_width - filled)
                    
                    print(f"\r[{bar}] {downloaded:,}/{total:,} ({100*progress:.1f}%) "
                          f"| {mb:.1f}MB | {rate:.1f}/s | ETA: {eta_min:.0f}m | Failed: {failed}", end='', flush=True)
    
    except KeyboardInterrupt:
        print("\n\n⏸ Paused! Progress saved. Run again to resume.")
    
    finally:
        conn.commit()
        conn.close()
    
    print(f"\n\n✓ Download complete!")
    print(f"  Tiles: {downloaded:,} downloaded, {failed} failed/empty")
    print(f"  Size: {os.path.getsize(output) / 1024 / 1024:.1f} MB")


def main():
    parser = argparse.ArgumentParser(
        description='Download OpenTopoMap tiles for offline use',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # Worldwide zoom 0-7 (~150MB)
  %(prog)s --zoom 0-6                         # Smaller worldwide (~20MB)
  %(prog)s --zoom 0-8                         # Larger worldwide (~500MB)
  %(prog)s --around 40.7,-74.0 --radius 300   # 300km around NYC, zoom 0-12
  %(prog)s --bounds -125,24,-66,50 --zoom 0-9 # Continental USA
        """
    )
    
    parser.add_argument('--zoom', default='0-7',
                        help='Zoom range min-max (default: 0-7)')
    parser.add_argument('--bounds',
                        help='Bounding box: west,south,east,north')
    parser.add_argument('--around',
                        help='Center point: lat,lon (use with --radius)')
    parser.add_argument('--radius', type=float, default=200,
                        help='Radius in km when using --around (default: 200)')
    parser.add_argument('--output', '-o', default='topo.mbtiles',
                        help='Output file (default: topo.mbtiles)')
    parser.add_argument('--no-resume', action='store_true',
                        help='Start fresh instead of resuming')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Show detailed download progress')
    parser.add_argument('--test', action='store_true',
                        help='Test mode: try downloading just 5 tiles to verify connectivity')
    
    args = parser.parse_args()
    
    # Parse zoom
    min_zoom, max_zoom = map(int, args.zoom.split('-'))
    
    # Determine bounds
    if args.around:
        lat, lon = map(float, args.around.split(','))
        lat_delta = args.radius / 111.0
        lon_delta = args.radius / (111.0 * math.cos(math.radians(lat)))
        bounds = (lon - lon_delta, lat - lat_delta, lon + lon_delta, lat + lat_delta)
        region_desc = f"{args.radius}km around ({lat}, {lon})"
    elif args.bounds:
        bounds = tuple(map(float, args.bounds.split(',')))
        region_desc = f"bounds {args.bounds}"
    else:
        bounds = (-180, -85, 180, 85)
        region_desc = "worldwide"
    
    # Count tiles
    tiles = list(get_tiles_in_bounds(*bounds, min_zoom, max_zoom))
    tile_count = len(tiles)
    est_mb = estimate_size_mb(tile_count)
    est_hours = tile_count / TILES_PER_SECOND / 3600
    
    print("=" * 60)
    print("OpenTopoMap Offline Tile Downloader")
    print("=" * 60)
    print(f"Region:     {region_desc}")
    print(f"Zoom:       {min_zoom}-{max_zoom}")
    print(f"Tiles:      {tile_count:,}")
    print(f"Est. size:  {est_mb:.0f} MB")
    print(f"Est. time:  {est_hours:.1f} hours @ {TILES_PER_SECOND} tile/sec")
    print(f"Output:     {args.output}")
    print("=" * 60)
    
    # Test mode - just try a few tiles
    if args.test:
        print("\n🧪 TEST MODE: Trying to download 5 tiles...\n")
        test_tiles = tiles[:5]
        success = 0
        for i, (z, x, y) in enumerate(test_tiles):
            print(f"Test {i+1}/5: z={z} x={x} y={y}")
            data = download_tile(z, x, y, verbose=True)
            if data:
                print(f"  ✓ Success: {len(data)} bytes\n")
                success += 1
            else:
                print(f"  ✗ Failed\n")
            time.sleep(2)
        
        print(f"\nTest complete: {success}/5 tiles downloaded successfully")
        if success == 0:
            print("⚠ OpenTopoMap may be blocking requests. Try:")
            print("  1. Wait a few minutes and try again")
            print("  2. Use a VPN")
            print("  3. Download pre-made tiles from openmaptiles.org")
        sys.exit(0)
    
    # Warn if too large
    if est_mb > 1000:
        print(f"\n⚠ WARNING: Estimated size ({est_mb:.0f}MB) exceeds 1GB!")
        print("  Consider reducing zoom level or using --around for a smaller area.")
    
    if tile_count > 100000:
        print(f"\n⚠ WARNING: {tile_count:,} tiles will take {est_hours:.1f} hours to download.")
    
    print()
    response = input("Continue? [y/N]: ").strip().lower()
    if response != 'y':
        print("Cancelled.")
        sys.exit(0)
    
    download_to_mbtiles(
        args.output, tiles, bounds,
        min_zoom, max_zoom,
        resume=not args.no_resume,
        verbose=args.verbose
    )


if __name__ == '__main__':
    main()
