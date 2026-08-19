"""
Offline map tiles, for chasing a balloon where there is no signal.

Recovery happens in exactly the places phone coverage does not: fields, forest,
the far side of a ridge. A map that needs the internet is a map that stops
working when you start needing it, so tiles for the flight area are downloaded
before launch and served from disk afterwards.

Tiles live in a SQLite database using the MBTiles table layout, so the same
file can be opened by other mapping tools. One caveat is recorded in the schema
below: MBTiles numbers rows from the bottom (TMS) while web maps number them
from the top (XYZ). This stores XYZ directly and says so, rather than silently
producing a file that looks like MBTiles and draws upside down elsewhere.

On OpenStreetMap's tile policy: bulk downloading is explicitly discouraged, and
their servers are donated. Downloads here are rate limited, sequential, sent
with a real User-Agent, and bounded by a tile-count ceiling that must be
acknowledged before a large region starts. Please point this at your own tile
server or a commercial provider for anything beyond a launch-day area.
"""

import contextlib
import logging
import math
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# Identifying the client is a condition of using OSM's tiles at all.
USER_AGENT = "RaptorHAB-GroundStation/2.0 (high-altitude balloon recovery)"

# One request at a time, with a pause. Slower than the servers could go, which
# is the point.
MIN_REQUEST_INTERVAL_SEC = 0.12

# A region larger than this needs an explicit acknowledgement. 20,000 tiles is
# already a big ask of a donated service.
LARGE_DOWNLOAD_TILES = 20_000


def deg_to_tile(latitude: float, longitude: float, zoom: int) -> Tuple[int, int]:
    """Slippy-map tile containing a coordinate (XYZ convention)."""
    latitude = max(-85.05112878, min(85.05112878, latitude))
    n = 2 ** zoom
    x = int((longitude + 180.0) / 360.0 * n)
    lat_rad = math.radians(latitude)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tiles_for_region(latitude: float, longitude: float, radius_km: float,
                     min_zoom: int, max_zoom: int) -> List[Tuple[int, int, int]]:
    """Every (z, x, y) covering a circle, as a bounding box per zoom."""
    tiles: List[Tuple[int, int, int]] = []
    # Degrees of latitude per km is constant; longitude shrinks with latitude.
    lat_span = radius_km / 111.32
    lon_span = radius_km / (111.32 * max(0.01, math.cos(math.radians(latitude))))

    for zoom in range(min_zoom, max_zoom + 1):
        x_min, y_max = deg_to_tile(latitude - lat_span, longitude - lon_span, zoom)
        x_max, y_min = deg_to_tile(latitude + lat_span, longitude + lon_span, zoom)
        for x in range(min(x_min, x_max), max(x_min, x_max) + 1):
            for y in range(min(y_min, y_max), max(y_min, y_max) + 1):
                tiles.append((zoom, x, y))
    return tiles


@dataclass
class DownloadProgress:
    running: bool = False
    total: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    message: str = ""

    @property
    def percent(self) -> float:
        return 0.0 if not self.total else round(100.0 * self.completed / self.total, 1)


class TileCache:
    """SQLite-backed tile store. Safe to use from several threads."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextlib.contextmanager
    def _connect(self):
        """
        A connection that is actually closed afterwards.

        sqlite3's own context manager commits a transaction but leaves the
        connection open, so `with sqlite3.connect(...)` leaks one handle per
        call. Enough of those and every subsequent open fails with "database
        is locked" -- which, for a tile cache consulted on every map pan, is a
        matter of minutes.
        """
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            try:
                connection.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                # WAL is an optimisation, not a requirement. A cache file left
                # in an odd state -- a deleted sidecar, a filesystem that does
                # not support it -- must not stop the ground station starting.
                pass
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_schema(self) -> None:
        try:
            self._create_schema()
        except sqlite3.DatabaseError as exc:
            # The cache is disposable. If the file is unusable, say so and
            # start again rather than refusing to run: losing cached tiles is
            # an inconvenience, failing to launch the ground station is not.
            logger.warning(f"Tile cache at {self.path} is unusable ({exc}); "
                           f"starting a fresh one")
            for suffix in ("", "-wal", "-shm"):
                sidecar = self.path.with_name(self.path.name + suffix)
                try:
                    sidecar.unlink()
                except FileNotFoundError:
                    pass
            self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS tiles (
                    zoom_level  INTEGER NOT NULL,
                    tile_column INTEGER NOT NULL,
                    tile_row    INTEGER NOT NULL,  -- XYZ (top-down), not TMS
                    tile_data   BLOB    NOT NULL,
                    fetched_at  INTEGER NOT NULL,
                    PRIMARY KEY (zoom_level, tile_column, tile_row)
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS tiles_idx "
                "ON tiles (zoom_level, tile_column, tile_row)")

    def get(self, z: int, x: int, y: int) -> Optional[bytes]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND "
                "tile_column=? AND tile_row=?", (z, x, y)).fetchone()
        return bytes(row[0]) if row else None

    def has(self, z: int, x: int, y: int) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? "
                "AND tile_row=?", (z, x, y)).fetchone()
        return row is not None

    def put(self, z: int, x: int, y: int, data: bytes) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO tiles "
                "(zoom_level, tile_column, tile_row, tile_data, fetched_at) "
                "VALUES (?,?,?,?,?)", (z, x, y, data, int(time.time())))

    def clear(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM tiles")
            connection.execute("VACUUM")

    def stats(self) -> dict:
        with self._lock, self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
            zooms = connection.execute(
                "SELECT zoom_level, COUNT(*) FROM tiles GROUP BY zoom_level "
                "ORDER BY zoom_level").fetchall()
        # Include the write-ahead log. Without it a freshly written cache
        # reports a few kilobytes while holding megabytes of tiles, because
        # the data has not been checkpointed into the main file yet -- which
        # reads as "the download did nothing".
        size = self.path.stat().st_size if self.path.exists() else 0
        for suffix in ("-wal", "-shm"):
            sidecar = self.path.with_name(self.path.name + suffix)
            if sidecar.exists():
                size += sidecar.stat().st_size
        return {
            "tiles": count,
            "bytes": size,
            "megabytes": round(size / 1e6, 1),
            "by_zoom": {int(z): int(n) for z, n in zooms},
            "path": str(self.path),
        }


class OfflineMapManager:
    """Downloads a region ahead of a flight and serves it back afterwards."""

    def __init__(self, cache_path: Path,
                 tile_url: str = DEFAULT_TILE_URL,
                 allow_network: bool = True):
        self.cache = TileCache(cache_path)
        self.tile_url = tile_url
        self.allow_network = allow_network
        self.progress = DownloadProgress()
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._last_request = 0.0
        self.on_progress: Optional[Callable[[DownloadProgress], None]] = None

    # -- serving -----------------------------------------------------------

    def get_tile(self, z: int, x: int, y: int,
                 fetch_if_missing: bool = True) -> Optional[bytes]:
        """
        A tile, from the cache if possible.

        Falling back to the network keeps the map usable while planning; in the
        field the cache is all there is, and a miss simply draws nothing rather
        than blocking on a request that cannot succeed.
        """
        cached = self.cache.get(z, x, y)
        if cached is not None:
            return cached
        if not (fetch_if_missing and self.allow_network):
            return None
        data = self._fetch(z, x, y)
        if data:
            self.cache.put(z, x, y, data)
        return data

    def _fetch(self, z: int, x: int, y: int, timeout: float = 10.0) -> Optional[bytes]:
        # Sequential and paced: see the note about tile-server policy above.
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_REQUEST_INTERVAL_SEC:
            time.sleep(MIN_REQUEST_INTERVAL_SEC - elapsed)
        self._last_request = time.monotonic()

        url = self.tile_url.format(z=z, x=x, y=y)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # 429 means we are being told to slow down. Respect it.
            if exc.code == 429:
                logger.warning("Tile server asked us to slow down; pausing")
                time.sleep(5.0)
            else:
                logger.debug(f"tile {z}/{x}/{y}: HTTP {exc.code}")
        except Exception as exc:
            logger.debug(f"tile {z}/{x}/{y}: {exc}")
        return None

    # -- downloading -------------------------------------------------------

    def estimate(self, latitude: float, longitude: float, radius_km: float,
                 min_zoom: int, max_zoom: int) -> dict:
        tiles = tiles_for_region(latitude, longitude, radius_km, min_zoom, max_zoom)
        missing = sum(1 for z, x, y in tiles if not self.cache.has(z, x, y))
        return {
            "tiles": len(tiles),
            "missing": missing,
            "estimated_megabytes": round(missing * 15_000 / 1e6, 1),  # ~15 kB/tile
            "estimated_minutes": round(missing * MIN_REQUEST_INTERVAL_SEC / 60.0, 1),
            "large": missing > LARGE_DOWNLOAD_TILES,
            "large_threshold": LARGE_DOWNLOAD_TILES,
        }

    def download_region(self, latitude: float, longitude: float, radius_km: float,
                        min_zoom: int = 8, max_zoom: int = 13,
                        acknowledge_large: bool = False) -> dict:
        """Start a background download. Returns the estimate it is working to."""
        if self._thread and self._thread.is_alive():
            raise RuntimeError("a download is already running")

        estimate = self.estimate(latitude, longitude, radius_km, min_zoom, max_zoom)
        if estimate["large"] and not acknowledge_large:
            raise ValueError(
                f"{estimate['missing']} tiles is a large request of a donated "
                f"tile service; confirm explicitly, or narrow the radius or "
                f"zoom range")

        tiles = tiles_for_region(latitude, longitude, radius_km, min_zoom, max_zoom)
        self._cancel.clear()
        self.progress = DownloadProgress(running=True, total=len(tiles),
                                         message="starting")
        self._thread = threading.Thread(target=self._download, args=(tiles,),
                                        daemon=True, name="tile-download")
        self._thread.start()
        return estimate

    def cancel(self) -> None:
        self._cancel.set()

    def _download(self, tiles: List[Tuple[int, int, int]]) -> None:
        try:
            for z, x, y in tiles:
                if self._cancel.is_set():
                    self.progress.message = "cancelled"
                    break
                if self.cache.has(z, x, y):
                    self.progress.skipped += 1
                    self.progress.completed += 1
                    continue
                data = self._fetch(z, x, y)
                if data:
                    self.cache.put(z, x, y, data)
                else:
                    self.progress.failed += 1
                self.progress.completed += 1
                self.progress.message = f"zoom {z}"
                if self.on_progress and self.progress.completed % 25 == 0:
                    self.on_progress(self.progress)
            else:
                self.progress.message = "complete"
        finally:
            self.progress.running = False
            if self.on_progress:
                self.on_progress(self.progress)

    def status(self) -> dict:
        return {
            "cache": self.cache.stats(),
            "download": {
                "running": self.progress.running,
                "total": self.progress.total,
                "completed": self.progress.completed,
                "skipped": self.progress.skipped,
                "failed": self.progress.failed,
                "percent": self.progress.percent,
                "message": self.progress.message,
            },
            "tile_url": self.tile_url,
            "allow_network": self.allow_network,
        }
