# Offline Maps for RaptorHab Ground Station

The ground station supports offline OpenStreetMap tiles for field operations where internet connectivity may be limited or unavailable.

## Quick Start

1. **Download pre-made tiles** (recommended for most users):
   - Visit [Protomaps Downloads](https://protomaps.com/downloads) 
   - Download a `.pmtiles` or `.mbtiles` file
   - Or use [OpenMapTiles](https://openmaptiles.org/downloads/)

2. **Place the file**:
   ```bash
   cp world.mbtiles /RaptorQHAB/ground/maps/
   ```

3. **Restart the ground station** - offline tiles will be automatically detected

4. **Toggle offline mode** in the web interface:
   - Go to Map view
   - Click "Layers" dropdown
   - Toggle "Offline Tiles" on/off

## File Sizes vs Coverage

| Preset | Zoom Levels | Approx Size | Use Case |
|--------|-------------|-------------|----------|
| worldwide-low | 0-6 | ~50 MB | Basic overview, navigation only |
| worldwide-med | 0-8 | ~200 MB | **Recommended for HAB tracking** |
| worldwide-high | 0-10 | ~2 GB | Detailed tracking, recovery |
| Regional (500km) | 0-12 | ~500 MB | Focused area with high detail |

## Downloading Your Own Tiles

### Using the Built-in Downloader

```bash
cd /RaptorQHAB/ground

# Preset: Worldwide medium detail (recommended)
python download_maps.py --preset worldwide-med --output maps/world.mbtiles

# Custom bounds (USA)
python download_maps.py --bounds -125,24,-66,50 --zoom 0-10 --output maps/usa.mbtiles

# Area around launch site (500km radius, high detail)
python download_maps.py --around 40.7,-74.0 --radius 500 --zoom 0-14 --output maps/launch.mbtiles
```

**Note**: Downloading tiles takes time and bandwidth. Be respectful of OSM's [tile usage policy](https://operations.osmfoundation.org/policies/tiles/).

### Using External Tools

For faster downloads or more control, use these tools:

#### 1. Protomaps (Recommended)
Download pre-generated planet extracts from [protomaps.com/downloads](https://protomaps.com/downloads)

#### 2. tilemaker
Build tiles from OSM PBF extracts:
```bash
# Install
brew install tilemaker  # macOS
# or build from source: https://github.com/systemed/tilemaker

# Download region extract
wget https://download.geofabrik.de/north-america/us-northeast-latest.osm.pbf

# Generate tiles
tilemaker --input us-northeast-latest.osm.pbf --output maps/northeast.mbtiles
```

#### 3. MapTiler
Commercial tool with free tier: [maptiler.com](https://www.maptiler.com/)

## MBTiles Format

The ground station uses [MBTiles](https://github.com/mapbox/mbtiles-spec), an SQLite-based format:

```
world.mbtiles
├── metadata (table)
│   ├── name: "World Map"
│   ├── format: "png"
│   ├── bounds: "-180,-85,180,85"
│   ├── minzoom: "0"
│   └── maxzoom: "8"
└── tiles (table)
    └── (zoom_level, tile_column, tile_row, tile_data)
```

**Note**: MBTiles uses TMS y-coordinate convention (y=0 at bottom), which the ground station handles automatically.

## Hybrid Mode

By default, the ground station operates in **hybrid mode**:

1. **Offline First**: Try to load tiles from local MBTiles
2. **Online Fallback**: If offline tile not available, fetch from internet
3. **Seamless Transition**: Works transparently at zoom levels beyond offline coverage

This means you can have zoom 0-8 offline for worldwide coverage, and the map will automatically fetch higher zoom levels from the internet when available.

## Configuration

In `config.py`:

```python
# Enable offline maps
map_offline_enabled: bool = True

# Path to MBTiles files
map_offline_path: str = "/RaptorQHAB/ground/maps"

# Default map file
map_offline_file: str = "world.mbtiles"

# Prefer offline when available (hybrid mode)
map_prefer_offline: bool = True
```

Environment variables:
```bash
export RAPTORHAB_MAP_OFFLINE_PATH=/path/to/maps
export RAPTORHAB_MAP_PREFER_OFFLINE=true
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/tiles/{z}/{x}/{y}.png` | Serve tiles (offline or fallback) |
| `GET /api/maps/status` | Get offline maps status |
| `POST /api/maps/config` | Update map preferences |

## Multiple Map Files

You can have multiple `.mbtiles` files for different purposes:

```
maps/
├── world.mbtiles        # Worldwide low-detail (default)
├── usa.mbtiles          # USA high-detail
└── launch-site.mbtiles  # Launch area very high-detail
```

The ground station loads all available files and uses the default as primary.

## Troubleshooting

### "No offline maps found"
- Check that `.mbtiles` files are in the configured path
- Verify file permissions
- Check the Maps Status in the web interface

### Tiles not loading
- Verify the MBTiles file is not corrupted: `sqlite3 world.mbtiles "SELECT COUNT(*) FROM tiles;"`
- Check zoom levels in metadata match your viewing zoom
- Look at browser console for errors

### Large file sizes
- Use lower zoom levels for worldwide coverage
- Use regional extracts instead of planet files
- Consider vector tiles (`.pbf` format) if your map renderer supports them

## Storage Requirements

Rough estimates for raster PNG tiles:

| Coverage | Zoom 0-6 | Zoom 0-8 | Zoom 0-10 | Zoom 0-12 |
|----------|----------|----------|-----------|-----------|
| World | 50 MB | 200 MB | 2 GB | 20 GB |
| USA | 5 MB | 20 MB | 200 MB | 2 GB |
| State | 1 MB | 4 MB | 40 MB | 400 MB |
| 100km radius | 100 KB | 1 MB | 10 MB | 100 MB |
