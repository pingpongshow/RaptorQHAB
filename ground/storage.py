"""
RaptorHab Ground Station - Storage Module
Manages storage of received images and data
"""

import logging
import os
import json
import time
import sqlite3
import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from threading import Lock
from pathlib import Path

from ground.decoder import ImageMetadata

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """Information about a mission/session"""
    session_id: str
    name: str
    start_time: float
    end_time: Optional[float] = None
    image_count: int = 0
    total_size_bytes: int = 0
    
    @property
    def display_name(self) -> str:
        """Human-readable session name"""
        if self.name:
            return self.name
        # Format: "Mission 2026-01-19 18:14"
        dt = datetime.fromtimestamp(self.start_time)
        return f"Mission {dt.strftime('%Y-%m-%d %H:%M')}"


@dataclass
class StoredImage:
    """Information about a stored image"""
    image_id: int
    filename: str
    filepath: str
    size_bytes: int
    width: int
    height: int
    capture_time: int
    received_time: float
    checksum: str
    thumbnail_path: Optional[str] = None
    session_id: Optional[str] = None


class ImageStorage:
    """
    Manages storage of received images organized by mission/session
    
    Features:
    - Automatic filename generation
    - Thumbnail generation
    - Image metadata database
    - Storage quota management
    - Mission/session organization
    """
    
    def __init__(
        self,
        image_path: str,
        db_path: str,
        max_images: int = 1000,
        generate_thumbnails: bool = True,
        thumbnail_size: Tuple[int, int] = (320, 240),
        session_name: str = None
    ):
        """
        Initialize image storage
        
        Args:
            image_path: Directory for storing images
            db_path: Path to image database
            max_images: Maximum number of images to store
            generate_thumbnails: Whether to generate thumbnails
            thumbnail_size: Thumbnail dimensions
            session_name: Optional custom name for this session/mission
        """
        self.image_path = image_path
        self.db_path = db_path
        self.max_images = max_images
        self.generate_thumbnails = generate_thumbnails
        self.thumbnail_size = thumbnail_size
        
        self._lock = Lock()
        self._conn: Optional[sqlite3.Connection] = None
        
        # Generate session_id based on startup time
        # This ensures each ground station run gets unique IDs
        self.session_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.session_name = session_name or ""
        self.session_start_time = time.time()
        
        # Create base directories
        os.makedirs(image_path, exist_ok=True)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # Create session-specific folder
        self.session_path = os.path.join(image_path, self.session_id)
        os.makedirs(self.session_path, exist_ok=True)
        os.makedirs(os.path.join(self.session_path, "thumbnails"), exist_ok=True)
        
        self._init_db()
        self._register_session()
        logger.info(f"Image storage session: {self.session_id}")
    
    def _init_db(self):
        """Initialize database schema"""
        with self._get_conn() as conn:
            # Check if table exists and needs migration
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            
            if 'images' in tables:
                # Check current schema
                cursor = conn.execute("PRAGMA table_info(images)")
                columns = {row[1] for row in cursor.fetchall()}
                
                if 'session_id' not in columns:
                    # Need to migrate - recreate table with new schema
                    logger.info("Migrating database schema to add session support...")
                    try:
                        # Rename old table
                        conn.execute("ALTER TABLE images RENAME TO images_old")
                        
                        # Create new table with session support
                        conn.execute('''
                            CREATE TABLE images (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                image_id INTEGER NOT NULL,
                                session_id TEXT NOT NULL DEFAULT 'legacy',
                                filename TEXT NOT NULL,
                                filepath TEXT NOT NULL,
                                size_bytes INTEGER,
                                width INTEGER,
                                height INTEGER,
                                capture_time INTEGER,
                                received_time REAL,
                                checksum TEXT,
                                thumbnail_path TEXT,
                                UNIQUE(image_id, session_id)
                            )
                        ''')
                        
                        # Copy data from old table
                        conn.execute('''
                            INSERT INTO images (
                                image_id, session_id, filename, filepath, size_bytes,
                                width, height, capture_time, received_time, checksum, thumbnail_path
                            )
                            SELECT 
                                image_id, 'legacy', filename, filepath, size_bytes,
                                width, height, capture_time, received_time, checksum, thumbnail_path
                            FROM images_old
                        ''')
                        
                        # Drop old table
                        conn.execute("DROP TABLE images_old")
                        conn.commit()
                        logger.info("Database migration complete")
                    except Exception as e:
                        logger.error(f"Migration failed: {e}")
                        # Try to recover by dropping and recreating
                        try:
                            conn.execute("DROP TABLE IF EXISTS images_old")
                            conn.execute("DROP TABLE IF EXISTS images")
                            conn.commit()
                            logger.warning("Dropped old tables, will recreate")
                        except:
                            pass
            
            # Create images table if it doesn't exist
            conn.execute('''
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL DEFAULT 'unknown',
                    filename TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    size_bytes INTEGER,
                    width INTEGER,
                    height INTEGER,
                    capture_time INTEGER,
                    received_time REAL,
                    checksum TEXT,
                    thumbnail_path TEXT,
                    UNIQUE(image_id, session_id)
                )
            ''')
            
            # Create sessions table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    name TEXT,
                    start_time REAL NOT NULL,
                    end_time REAL,
                    folder_path TEXT
                )
            ''')
            
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_images_image_id
                ON images(image_id)
            ''')
            
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_images_received_time
                ON images(received_time)
            ''')
            
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_images_session
                ON images(session_id)
            ''')
            
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_sessions_start_time
                ON sessions(start_time)
            ''')
            
            conn.commit()
        
        logger.info(f"Image database initialized: {self.db_path}")
    
    def _register_session(self):
        """Register the current session in the database"""
        with self._get_conn() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO sessions (session_id, name, start_time, folder_path)
                VALUES (?, ?, ?, ?)
            ''', (self.session_id, self.session_name, self.session_start_time, self.session_path))
            conn.commit()
            
            # Also create a legacy session for old images if needed
            conn.execute('''
                INSERT OR IGNORE INTO sessions (session_id, name, start_time, folder_path)
                VALUES ('legacy', 'Legacy Images', 0, ?)
            ''', (self.image_path,))
            conn.commit()
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get database connection"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn
    
    def store_image(
        self,
        image_id: int,
        image_data: bytes,
        metadata: ImageMetadata
    ) -> Optional[StoredImage]:
        """
        Store a received image
        
        Args:
            image_id: Image identifier
            image_data: WebP image data
            metadata: Image metadata
            
        Returns:
            StoredImage if successful, None otherwise
        """
        with self._lock:
            try:
                # Check if already stored (for this session)
                if self._image_exists(image_id, self.session_id):
                    logger.debug(f"Image {image_id} already stored in session {self.session_id}")
                    return self._get_image_info(image_id, self.session_id)
                
                # Generate filename and save to session folder
                timestamp = datetime.fromtimestamp(metadata.timestamp)
                filename = f"img_{image_id:05d}_{timestamp.strftime('%H%M%S')}.webp"
                filepath = os.path.join(self.session_path, filename)
                
                # Write image file
                with open(filepath, 'wb') as f:
                    f.write(image_data)
                
                # Calculate checksum
                checksum = hashlib.md5(image_data).hexdigest()
                
                # Generate thumbnail in session's thumbnail folder
                thumbnail_path = None
                if self.generate_thumbnails:
                    thumbnail_path = self._create_thumbnail(filepath, filename, self.session_path)
                
                # Store in database
                stored = StoredImage(
                    image_id=image_id,
                    filename=filename,
                    filepath=filepath,
                    size_bytes=len(image_data),
                    width=metadata.width,
                    height=metadata.height,
                    capture_time=metadata.timestamp,
                    received_time=time.time(),
                    checksum=checksum,
                    thumbnail_path=thumbnail_path,
                )
                
                self._insert_image(stored, self.session_id)
                
                logger.info(f"Stored image {image_id}: {filename} ({len(image_data)} bytes)")
                
                # Cleanup old images if needed
                self._cleanup_old_images()
                
                return stored
                
            except Exception as e:
                logger.error(f"Failed to store image {image_id}: {e}")
                return None
    
    def _create_thumbnail(self, filepath: str, filename: str, base_path: str = None) -> Optional[str]:
        """Create thumbnail for an image"""
        try:
            from PIL import Image
            
            thumb_filename = f"thumb_{filename}"
            if base_path:
                thumb_path = os.path.join(base_path, "thumbnails", thumb_filename)
            else:
                thumb_path = os.path.join(self.image_path, "thumbnails", thumb_filename)
            
            # Ensure thumbnail directory exists
            os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
            
            with Image.open(filepath) as img:
                img.thumbnail(self.thumbnail_size)
                img.save(thumb_path, "WEBP", quality=70)
            
            return thumb_path
            
        except ImportError:
            logger.warning("PIL not available, skipping thumbnail generation")
            return None
        except Exception as e:
            logger.error(f"Thumbnail generation failed: {e}")
            return None
    
    def _image_exists(self, image_id: int, session_id: str = None) -> bool:
        """Check if image exists in database for this session"""
        with self._get_conn() as conn:
            if session_id:
                row = conn.execute(
                    "SELECT 1 FROM images WHERE image_id = ? AND session_id = ?",
                    (image_id, session_id)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM images WHERE image_id = ?",
                    (image_id,)
                ).fetchone()
            return row is not None
    
    def _get_image_info(self, image_id: int, session_id: str = None) -> Optional[StoredImage]:
        """Get image info from database"""
        with self._get_conn() as conn:
            if session_id:
                row = conn.execute(
                    "SELECT * FROM images WHERE image_id = ? AND session_id = ?",
                    (image_id, session_id)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM images WHERE image_id = ?",
                    (image_id,)
                ).fetchone()
            
            if row:
                return self._row_to_stored_image(row)
            return None
    
    def _insert_image(self, stored: StoredImage, session_id: str = 'unknown'):
        """Insert image record into database"""
        with self._get_conn() as conn:
            conn.execute('''
                INSERT INTO images (
                    image_id, session_id, filename, filepath, size_bytes,
                    width, height, capture_time, received_time,
                    checksum, thumbnail_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                stored.image_id, session_id, stored.filename, stored.filepath,
                stored.size_bytes, stored.width, stored.height,
                stored.capture_time, stored.received_time,
                stored.checksum, stored.thumbnail_path
            ))
            conn.commit()
    
    def _cleanup_old_images(self):
        """Remove oldest images if over quota"""
        with self._get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            
            if count <= self.max_images:
                return
            
            # Get oldest images to remove
            to_remove = count - self.max_images
            rows = conn.execute('''
                SELECT id, filepath, thumbnail_path FROM images
                ORDER BY received_time ASC
                LIMIT ?
            ''', (to_remove,)).fetchall()
            
            for row in rows:
                # Delete files
                try:
                    if os.path.exists(row['filepath']):
                        os.remove(row['filepath'])
                    if row['thumbnail_path'] and os.path.exists(row['thumbnail_path']):
                        os.remove(row['thumbnail_path'])
                except OSError as e:
                    logger.error(f"Failed to delete image file: {e}")
                
                # Delete from database
                conn.execute("DELETE FROM images WHERE id = ?", (row['id'],))
            
            conn.commit()
            logger.info(f"Cleaned up {to_remove} old images")
    
    def _row_to_stored_image(self, row: sqlite3.Row) -> StoredImage:
        """Convert database row to StoredImage"""
        return StoredImage(
            image_id=row['image_id'],
            filename=row['filename'],
            filepath=row['filepath'],
            size_bytes=row['size_bytes'],
            width=row['width'],
            height=row['height'],
            capture_time=row['capture_time'],
            received_time=row['received_time'],
            checksum=row['checksum'],
            thumbnail_path=row['thumbnail_path'],
            session_id=row['session_id'] if 'session_id' in row.keys() else None,
        )
    
    def get_image(self, image_id: int, session_id: str = None) -> Optional[StoredImage]:
        """Get image by ID, optionally filtered by session"""
        with self._lock:
            return self._get_image_info(image_id, session_id)
    
    def get_image_data(self, image_id: int) -> Optional[bytes]:
        """Get image data by ID"""
        info = self.get_image(image_id)
        if info and os.path.exists(info.filepath):
            with open(info.filepath, 'rb') as f:
                return f.read()
        return None
    
    def get_thumbnail_data(self, image_id: int) -> Optional[bytes]:
        """Get thumbnail data by ID"""
        info = self.get_image(image_id)
        if info and info.thumbnail_path and os.path.exists(info.thumbnail_path):
            with open(info.thumbnail_path, 'rb') as f:
                return f.read()
        return None
    
    def get_recent_images(self, count: int = 20) -> List[StoredImage]:
        """Get most recent images"""
        with self._lock:
            with self._get_conn() as conn:
                rows = conn.execute('''
                    SELECT * FROM images
                    ORDER BY received_time DESC
                    LIMIT ?
                ''', (count,)).fetchall()
                
                return [self._row_to_stored_image(row) for row in rows]
    
    def get_images_in_range(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None
    ) -> List[StoredImage]:
        """Get images within time range"""
        with self._lock:
            query = "SELECT * FROM images WHERE 1=1"
            params = []
            
            if start_time is not None:
                query += " AND received_time >= ?"
                params.append(start_time)
            
            if end_time is not None:
                query += " AND received_time <= ?"
                params.append(end_time)
            
            query += " ORDER BY received_time DESC"
            
            with self._get_conn() as conn:
                rows = conn.execute(query, params).fetchall()
                return [self._row_to_stored_image(row) for row in rows]
    
    def get_image_count(self) -> int:
        """Get total number of stored images"""
        with self._lock:
            with self._get_conn() as conn:
                return conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    
    def get_storage_stats(self) -> Dict:
        """Get storage statistics"""
        with self._lock:
            with self._get_conn() as conn:
                count = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
                total_size = conn.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) FROM images"
                ).fetchone()[0]
                
                if count > 0:
                    first = conn.execute(
                        "SELECT received_time FROM images ORDER BY received_time ASC LIMIT 1"
                    ).fetchone()[0]
                    last = conn.execute(
                        "SELECT received_time FROM images ORDER BY received_time DESC LIMIT 1"
                    ).fetchone()[0]
                else:
                    first = last = None
                
                return {
                    'image_count': count,
                    'total_size_bytes': total_size,
                    'total_size_mb': total_size / (1024 * 1024),
                    'first_received': first,
                    'last_received': last,
                    'max_images': self.max_images,
                    'current_session': self.session_id,
                }
    
    # ========== Session/Mission Management ==========
    
    def get_sessions(self) -> List[Session]:
        """Get all sessions/missions, most recent first"""
        with self._lock:
            with self._get_conn() as conn:
                # Get sessions with image counts
                rows = conn.execute('''
                    SELECT 
                        s.session_id,
                        s.name,
                        s.start_time,
                        s.end_time,
                        s.folder_path,
                        COUNT(i.id) as image_count,
                        COALESCE(SUM(i.size_bytes), 0) as total_size
                    FROM sessions s
                    LEFT JOIN images i ON s.session_id = i.session_id
                    GROUP BY s.session_id
                    ORDER BY s.start_time DESC
                ''').fetchall()
                
                sessions = []
                for row in rows:
                    sessions.append(Session(
                        session_id=row['session_id'],
                        name=row['name'] or '',
                        start_time=row['start_time'],
                        end_time=row['end_time'],
                        image_count=row['image_count'],
                        total_size_bytes=row['total_size'],
                    ))
                return sessions
    
    def get_session(self, session_id: str) -> Optional[Session]:
        """Get a specific session by ID"""
        with self._lock:
            with self._get_conn() as conn:
                row = conn.execute('''
                    SELECT 
                        s.session_id,
                        s.name,
                        s.start_time,
                        s.end_time,
                        s.folder_path,
                        COUNT(i.id) as image_count,
                        COALESCE(SUM(i.size_bytes), 0) as total_size
                    FROM sessions s
                    LEFT JOIN images i ON s.session_id = i.session_id
                    WHERE s.session_id = ?
                    GROUP BY s.session_id
                ''', (session_id,)).fetchone()
                
                if row:
                    return Session(
                        session_id=row['session_id'],
                        name=row['name'] or '',
                        start_time=row['start_time'],
                        end_time=row['end_time'],
                        image_count=row['image_count'],
                        total_size_bytes=row['total_size'],
                    )
                return None
    
    def get_session_images(self, session_id: str, count: int = 100) -> List[StoredImage]:
        """Get images for a specific session"""
        with self._lock:
            with self._get_conn() as conn:
                rows = conn.execute('''
                    SELECT * FROM images
                    WHERE session_id = ?
                    ORDER BY image_id ASC
                    LIMIT ?
                ''', (session_id, count)).fetchall()
                
                return [self._row_to_stored_image(row) for row in rows]
    
    def rename_session(self, session_id: str, new_name: str) -> bool:
        """Rename a session/mission"""
        with self._lock:
            with self._get_conn() as conn:
                conn.execute('''
                    UPDATE sessions SET name = ? WHERE session_id = ?
                ''', (new_name, session_id))
                conn.commit()
                
                # Also update current session name if it's this session
                if session_id == self.session_id:
                    self.session_name = new_name
                
                return True
    
    def get_image_by_session(self, session_id: str, image_id: int) -> Optional[StoredImage]:
        """Get a specific image from a specific session"""
        with self._lock:
            return self._get_image_info(image_id, session_id)
    
    def get_image_data_by_session(self, session_id: str, image_id: int) -> Optional[bytes]:
        """Get image data for a specific session and image ID"""
        info = self.get_image_by_session(session_id, image_id)
        if info and os.path.exists(info.filepath):
            with open(info.filepath, 'rb') as f:
                return f.read()
        return None
    
    def get_thumbnail_data_by_session(self, session_id: str, image_id: int) -> Optional[bytes]:
        """Get thumbnail data for a specific session and image ID"""
        info = self.get_image_by_session(session_id, image_id)
        if info and info.thumbnail_path and os.path.exists(info.thumbnail_path):
            with open(info.thumbnail_path, 'rb') as f:
                return f.read()
        # Fall back to full image
        return self.get_image_data_by_session(session_id, image_id)
    
    def export_images(self, output_dir: str, start_time: Optional[float] = None) -> int:
        """
        Export images to a directory
        
        Args:
            output_dir: Output directory
            start_time: Only export images after this time
            
        Returns:
            Number of images exported
        """
        import shutil
        
        os.makedirs(output_dir, exist_ok=True)
        
        images = self.get_images_in_range(start_time=start_time)
        count = 0
        
        for img in images:
            if os.path.exists(img.filepath):
                dest = os.path.join(output_dir, img.filename)
                shutil.copy2(img.filepath, dest)
                count += 1
        
        logger.info(f"Exported {count} images to {output_dir}")
        return count
    
    # ========== Delete Operations ==========
    
    def delete_image(self, session_id: str, image_id: int) -> bool:
        """Delete a single image"""
        with self._lock:
            try:
                with self._get_conn() as conn:
                    # Get image info first
                    row = conn.execute(
                        "SELECT filepath, thumbnail_path FROM images WHERE session_id = ? AND image_id = ?",
                        (session_id, image_id)
                    ).fetchone()
                    
                    if not row:
                        return False
                    
                    # Delete files
                    if row['filepath'] and os.path.exists(row['filepath']):
                        os.remove(row['filepath'])
                    if row['thumbnail_path'] and os.path.exists(row['thumbnail_path']):
                        os.remove(row['thumbnail_path'])
                    
                    # Delete from database
                    conn.execute(
                        "DELETE FROM images WHERE session_id = ? AND image_id = ?",
                        (session_id, image_id)
                    )
                    conn.commit()
                    
                    logger.info(f"Deleted image {image_id} from session {session_id}")
                    return True
                    
            except Exception as e:
                logger.error(f"Failed to delete image {image_id}: {e}")
                return False
    
    def delete_images(self, session_id: str, image_ids: List[int]) -> int:
        """Delete multiple images from a session"""
        deleted = 0
        for image_id in image_ids:
            if self.delete_image(session_id, image_id):
                deleted += 1
        return deleted
    
    def delete_session(self, session_id: str) -> bool:
        """Delete an entire session and all its images"""
        with self._lock:
            try:
                with self._get_conn() as conn:
                    # Get session folder path
                    row = conn.execute(
                        "SELECT folder_path FROM sessions WHERE session_id = ?",
                        (session_id,)
                    ).fetchone()
                    
                    folder_path = row['folder_path'] if row else None
                    
                    # Delete all image files in session
                    rows = conn.execute(
                        "SELECT filepath, thumbnail_path FROM images WHERE session_id = ?",
                        (session_id,)
                    ).fetchall()
                    
                    for row in rows:
                        try:
                            if row['filepath'] and os.path.exists(row['filepath']):
                                os.remove(row['filepath'])
                            if row['thumbnail_path'] and os.path.exists(row['thumbnail_path']):
                                os.remove(row['thumbnail_path'])
                        except OSError as e:
                            logger.warning(f"Failed to delete file: {e}")
                    
                    # Delete from database
                    conn.execute("DELETE FROM images WHERE session_id = ?", (session_id,))
                    conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
                    conn.commit()
                    
                    # Try to remove the session folder if empty
                    if folder_path and os.path.exists(folder_path):
                        try:
                            import shutil
                            shutil.rmtree(folder_path)
                        except OSError as e:
                            logger.warning(f"Failed to remove session folder: {e}")
                    
                    logger.info(f"Deleted session {session_id}")
                    return True
                    
            except Exception as e:
                logger.error(f"Failed to delete session {session_id}: {e}")
                return False
    
    def export_session_zip(self, session_id: str, output_path: str) -> Optional[str]:
        """Export a session as a ZIP file"""
        import zipfile
        
        try:
            images = self.get_session_images(session_id, count=10000)
            if not images:
                return None
            
            session = self.get_session(session_id)
            zip_name = f"{session.display_name.replace(' ', '_').replace(':', '-')}.zip" if session else f"{session_id}.zip"
            zip_path = os.path.join(output_path, zip_name)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for img in images:
                    if os.path.exists(img.filepath):
                        # Add image with just filename (no path)
                        zf.write(img.filepath, img.filename)
            
            logger.info(f"Exported session {session_id} to {zip_path}")
            return zip_path
            
        except Exception as e:
            logger.error(f"Failed to export session {session_id}: {e}")
            return None
    
    def close(self):
        """Close database connection"""
        if self._conn:
            self._conn.close()
            self._conn = None


class DataExporter:
    """Utility class for exporting flight data"""
    
    def __init__(self, storage: ImageStorage, telemetry_db_path: str):
        """
        Initialize exporter
        
        Args:
            storage: Image storage instance
            telemetry_db_path: Path to telemetry database
        """
        self.storage = storage
        self.telemetry_db_path = telemetry_db_path
    
    def export_flight(
        self,
        output_dir: str,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None
    ) -> Dict:
        """
        Export complete flight data
        
        Args:
            output_dir: Output directory
            start_time: Start of time range
            end_time: End of time range
            
        Returns:
            Export summary
        """
        import shutil
        
        os.makedirs(output_dir, exist_ok=True)
        
        summary = {
            'images_exported': 0,
            'telemetry_points': 0,
            'export_time': time.time(),
        }
        
        # Export images
        images_dir = os.path.join(output_dir, "images")
        summary['images_exported'] = self.storage.export_images(images_dir, start_time)
        
        # Export telemetry
        telemetry_file = os.path.join(output_dir, "telemetry.csv")
        summary['telemetry_points'] = self._export_telemetry_csv(
            telemetry_file, start_time, end_time
        )
        
        # Export KML track
        kml_file = os.path.join(output_dir, "track.kml")
        self._export_kml(kml_file, start_time, end_time)
        
        # Write summary
        summary_file = os.path.join(output_dir, "summary.json")
        with open(summary_file, 'w') as f:
            json.dump(summary, f, indent=2)
        
        logger.info(f"Flight data exported to {output_dir}")
        return summary
    
    def _export_telemetry_csv(
        self,
        filepath: str,
        start_time: Optional[float],
        end_time: Optional[float]
    ) -> int:
        """Export telemetry to CSV"""
        import csv
        
        conn = sqlite3.connect(self.telemetry_db_path)
        conn.row_factory = sqlite3.Row
        
        query = "SELECT * FROM telemetry WHERE 1=1"
        params = []
        
        if start_time:
            query += " AND received_at >= ?"
            params.append(start_time)
        if end_time:
            query += " AND received_at <= ?"
            params.append(end_time)
        
        query += " ORDER BY received_at ASC"
        
        rows = conn.execute(query, params).fetchall()
        conn.close()
        
        if not rows:
            return 0
        
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow(list(row))
        
        return len(rows)
    
    def _export_kml(
        self,
        filepath: str,
        start_time: Optional[float],
        end_time: Optional[float]
    ):
        """Export flight track to KML"""
        conn = sqlite3.connect(self.telemetry_db_path)
        
        query = """
            SELECT latitude, longitude, altitude FROM telemetry
            WHERE latitude != 0 AND longitude != 0
        """
        params = []
        
        if start_time:
            query += " AND received_at >= ?"
            params.append(start_time)
        if end_time:
            query += " AND received_at <= ?"
            params.append(end_time)
        
        query += " ORDER BY received_at ASC"
        
        rows = conn.execute(query, params).fetchall()
        conn.close()
        
        kml = '''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
    <name>RaptorHab Flight Track</name>
    <Style id="track">
        <LineStyle><color>ff0000ff</color><width>3</width></LineStyle>
    </Style>
    <Placemark>
        <name>Flight Path</name>
        <styleUrl>#track</styleUrl>
        <LineString>
            <altitudeMode>absolute</altitudeMode>
            <coordinates>
'''
        
        for lat, lon, alt in rows:
            kml += f"                {lon},{lat},{alt}\n"
        
        kml += '''            </coordinates>
        </LineString>
    </Placemark>
</Document>
</kml>'''
        
        with open(filepath, 'w') as f:
            f.write(kml)
