#!/usr/bin/env python3
"""
RaptorHab Ground Station - Offline Map Downloader

Downloads OpenStreetMap tiles and packages them into MBTiles format
for offline use with the ground station.

Usage:
    python download_maps.py --preset worldwide-low
    python download_maps.py --bounds -125,24,-66,50 --zoom 0-10 --output usa.mbtiles
    python download_maps.py --around 40.7,-74.0 --radius 500 --zoom 0-14 --output nyc.mbtiles

Presets:
    worldwide-low   : Zoom 0-6, ~50MB, basic worldwide coverage
    worldwide-med   : Zoom 0-8, ~200MB, good for HAB tracking
    worldwide-high  : Zoom 0-10, ~2GB, detailed tracking (takes hours)
"""

import argparse
import sqlite3
import os
import sys
import time
import math
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Tuple, List, Generator, Optional

# Tile server configuration
TILE_SERVERS = [
    'https://a.tile.openstreetmap.org/{z}/{x}/{y}.png',
    'https://b.tile.openstreetmap.org/{z}/{x}/{y}.png',
    'https://c.tile.openstreetmap.org/{z}/{x}/{y}.png',
]

USER_AGENT = 'RaptorHab-OfflineMapDownloader/1.0 (HAB tracking ground station)'
REQUESTS_PER_SECOND = 2


def lat_lon_to_tile(lat: float, lon: float, zoom: int) -> Tuple[int, int]:
    """Convert lat/lon to tile coordinates at given zoom level"""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(lat_rad)) / math.pi) / 2 * n)
    return x, y


def get_tiles_in_bounds(
    west: float, south: float, east: float, north: float,
    min_zoom: int, max_zoom: int
) -> Generator[Tuple[int, int, int], None, None]:
    """Generate all tile coordinates within bounds for zoom range"""
    for z in range(min_zoom, max_zoom + 1):
        x_min, y_max = lat_lon_to_tile(north, west, z)
        x_max, y_min = lat_lon_to_tile(south, east, z)
        
        n = 2 ** z
        x_min = max(0, x_min)
        x_max = min(n - 1, x_max)
        y_min = max(0, y_min)
        y_max = min(n - 1, y_max)
        
        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                yield z, x, y


def get_tiles_around_point(
    lat: float, lon: float, radius_km: float,
    min_zoom: int, max_zoom: int
) -> Generator[Tuple[int, int, int], None, None]:
    """Generate tiles within radius of a point"""
    lat_delta = radius_km / 111
    lon_delta = radius_km / (111 * math.cos(math.radians(lat)))
    
    west = lon - lon_delta
    east = lon + lon_delta
    south = lat - lat_delta
    north = lat + lat_delta
    
    yield from get_tiles_in_bounds(west, south, east, north, min_zoom, max_zoom)


def download_tile(z: int, x: int, y: int, server_idx: int = 0) -> Optional[bytes]:
    """Download a single tile from OSM servers"""
    url = TILE_SERVERS[server_idx % len(TILE_SERVERS)].format(z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read()
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None


def create_mbtiles(output_path: str, name: str, description: str, 
                   bounds: Tuple[float, float, float, float], min_zoom: int, max_zoom: int):
    """Create a new MBTiles database"""
    conn = sqlite3.connect(output_path)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tiles (
            zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER,
            tile_data BLOB, PRIMARY KEY (zoom_level, tile_column, tile_row)
        )
    ''')
    
    west, south, east, north = bounds
    metadata = [
        ('name', name), ('type', 'baselayer'), ('version', '1.0'),
        ('description', description), ('format', 'png'),
        ('bounds', f'{west},{south},{east},{north}'),
        ('minzoom', str(min_zoom)), ('maxzoom', str(max_zoom)),
        ('attribution', '© OpenStreetMap contributors'),
    ]
    cursor.executemany('INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)', metadata)
    conn.commit()
    return conn


def xyz_to_tms(z: int, y: int) -> int:
    """Convert XYZ y to TMS y (MBTiles uses TMS convention)"""
    return (1 << z) - 1 - y


def download_tiles_to_mbtiles(
    output_path: str, tiles: List[Tuple[int, int, int]],
    name: str = 'Offline OSM', description: str = 'OpenStreetMap tiles for offline use',
    bounds: Tuple[float, float, float, float] = (-180, -85, 180, 85),
    min_zoom: int = 0, max_zoom: int = 18, workers: int = 4
):
    """Download tiles and save to MBTiles"""
    conn = create_mbtiles(output_path, name, description, bounds, min_zoom, max_zoom)
    cursor = conn.cursor()
    
    total = len(tiles)
    downloaded = 0
    failed = 0
    start_time = time.time()
    
    print(f"Downloading {total:,} tiles to {output_path}")
    print(f"Using {workers} workers, rate limit: {REQUESTS_PER_SECOND}/sec\n")
    
    min_interval = workers / REQUESTS_PER_SECOND
    
    for i, (z, x, y) in enumerate(tiles):
        data = download_tile(z, x, y, i)
        downloaded += 1
        
        if data:
            tms_y = xyz_to_tms(z, y)
            cursor.execute(
                'INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)',
                (z, x, tms_y, data)
            )
            if downloaded % 100 == 0:
                conn.commit()
        else:
            failed += 1
        
        if downloaded % 50 == 0 or downloaded == total:
            elapsed = time.time() - start_time
            rate = downloaded / elapsed if elapsed > 0 else 0
            eta = (total - downloaded) / rate if rate > 0 else 0
            print(f"\rProgress: {downloaded:,}/{total:,} ({100*downloaded/total:.1f}%) "
                  f"| {rate:.1f}/sec | ETA: {eta/60:.1f}m | Failed: {failed}", end='')
        
        time.sleep(min_interval)
    
    conn.commit()
    conn.close()
    
    print(f"\n\nDownload complete!")
    print(f"  Total tiles: {downloaded:,}, Failed: {failed}")
    print(f"  File size: {os.path.getsize(output_path) / 1024 / 1024:.1f} MB")


PRESETS = {
    'worldwide-low': {
        'bounds': (-180, -85, 180, 85), 'zoom': (0, 6),
        'description': 'Worldwide coverage, zoom 0-6 (~50MB)',
    },
    'worldwide-med': {
        'bounds': (-180, -85, 180, 85), 'zoom': (0, 8),
        'description': 'Worldwide coverage, zoom 0-8 (~200MB)',
    },
    'usa': {
        'bounds': (-125, 24, -66, 50), 'zoom': (0, 10),
        'description': 'USA coverage, zoom 0-10',
    },
}


def main():
    parser = argparse.ArgumentParser(description='Download OSM tiles for offline use')
    parser.add_argument('--preset', choices=list(PRESETS.keys()), help='Use preset configuration')
    parser.add_argument('--bounds', help='Bounding box: west,south,east,north')
    parser.add_argument('--around', help='Center point: lat,lon')
    parser.add_argument('--radius', type=float, default=100, help='Radius in km (with --around)')
    parser.add_argument('--zoom', default='0-8', help='Zoom range: min-max')
    parser.add_argument('--output', '-o', default='world.mbtiles', help='Output file')
    parser.add_argument('--workers', type=int, default=1, help='Number of workers')
    
    args = parser.parse_args()
    
    if args.preset:
        preset = PRESETS[args.preset]
        bounds = preset['bounds']
        min_zoom, max_zoom = preset['zoom']
        description = preset['description']
    elif args.bounds:
        bounds = tuple(map(float, args.bounds.split(',')))
        min_zoom, max_zoom = map(int, args.zoom.split('-'))
        description = f'Custom bounds, zoom {min_zoom}-{max_zoom}'
    elif args.around:
        lat, lon = map(float, args.around.split(','))
        min_zoom, max_zoom = map(int, args.zoom.split('-'))
        lat_delta = args.radius / 111
        lon_delta = args.radius / (111 * math.cos(math.radians(lat)))
        bounds = (lon - lon_delta, lat - lat_delta, lon + lon_delta, lat + lat_delta)
        description = f'{args.radius}km around ({lat}, {lon}), zoom {min_zoom}-{max_zoom}'
    else:
        print("Specify --preset, --bounds, or --around")
        sys.exit(1)
    
    print(f"Configuration: {description}")
    print(f"Bounds: {bounds}")
    print(f"Zoom: {min_zoom}-{max_zoom}")
    
    tiles = list(get_tiles_in_bounds(*bounds, min_zoom, max_zoom))
    print(f"Total tiles to download: {len(tiles):,}\n")
    
    if input("Continue? [y/N] ").lower() != 'y':
        sys.exit(0)
    
    download_tiles_to_mbtiles(
        args.output, tiles, 'Offline OSM', description,
        bounds, min_zoom, max_zoom, args.workers
    )


if __name__ == '__main__':
    main()
