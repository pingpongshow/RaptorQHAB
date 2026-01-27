"""
RaptorHab Ground Station - Telemetry Processor
Processes, stores, and provides access to received telemetry
"""

import logging
import time
import sqlite3
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any
from threading import Lock
from collections import deque

from common.constants import FixType
from common.protocol import TelemetryPayload

logger = logging.getLogger(__name__)


@dataclass
class TelemetryPoint:
    """A single telemetry data point"""
    # Reception info
    received_at: float
    rssi: int
    packet_seq: int
    
    # GPS
    latitude: float
    longitude: float
    altitude: float
    speed: float
    heading: float
    satellites: int
    fix_type: int
    gps_time: int
    
    # System
    battery_mv: int
    cpu_temp: float
    radio_temp: float
    
    # Transmission status
    image_id: int
    image_progress: int
    payload_rssi: int  # RSSI reported by payload
    
    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return asdict(self)
    
    @classmethod
    def from_payload(
        cls,
        payload: TelemetryPayload,
        received_at: float,
        rssi: int,
        packet_seq: int
    ) -> 'TelemetryPoint':
        """Create from TelemetryPayload"""
        return cls(
            received_at=received_at,
            rssi=rssi,
            packet_seq=packet_seq,
            latitude=payload.latitude,
            longitude=payload.longitude,
            altitude=payload.altitude,
            speed=payload.speed,
            heading=payload.heading,
            satellites=payload.satellites,
            fix_type=payload.fix_type,
            gps_time=payload.gps_time,
            battery_mv=payload.battery_mv,
            cpu_temp=payload.cpu_temp,
            radio_temp=payload.radio_temp,
            image_id=payload.image_id,
            image_progress=payload.image_progress,
            payload_rssi=payload.rssi,
        )


class TelemetryBuffer:
    """In-memory circular buffer for recent telemetry"""
    
    def __init__(self, max_size: int = 1000):
        """
        Initialize buffer
        
        Args:
            max_size: Maximum number of points to keep
        """
        self._buffer: deque = deque(maxlen=max_size)
        self._lock = Lock()
    
    def add(self, point: TelemetryPoint):
        """Add a telemetry point"""
        with self._lock:
            self._buffer.append(point)
    
    def get_latest(self, count: int = 1) -> List[TelemetryPoint]:
        """Get the most recent points"""
        with self._lock:
            if count >= len(self._buffer):
                return list(self._buffer)
            return list(self._buffer)[-count:]
    
    def get_all(self) -> List[TelemetryPoint]:
        """Get all buffered points"""
        with self._lock:
            return list(self._buffer)
    
    def get_since(self, timestamp: float) -> List[TelemetryPoint]:
        """Get points since a timestamp"""
        with self._lock:
            return [p for p in self._buffer if p.received_at >= timestamp]
    
    def clear(self):
        """Clear the buffer"""
        with self._lock:
            self._buffer.clear()
    
    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)


class TelemetryDatabase:
    """SQLite database for persistent telemetry storage"""
    
    def __init__(self, db_path: str, session_id: str = None):
        """
        Initialize database
        
        Args:
            db_path: Path to SQLite database file
            session_id: Current session identifier
        """
        self.db_path = db_path
        self.session_id = session_id
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = Lock()
        
        self._init_db()
    
    def _init_db(self):
        """Initialize database schema"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        with self._get_conn() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at REAL NOT NULL,
                    rssi INTEGER,
                    packet_seq INTEGER,
                    latitude REAL,
                    longitude REAL,
                    altitude REAL,
                    speed REAL,
                    heading REAL,
                    satellites INTEGER,
                    fix_type INTEGER,
                    gps_time INTEGER,
                    battery_mv INTEGER,
                    cpu_temp REAL,
                    radio_temp REAL,
                    image_id INTEGER,
                    image_progress INTEGER,
                    payload_rssi INTEGER,
                    session_id TEXT
                )
            ''')
            
            # Migration: add session_id column if missing
            cursor = conn.execute("PRAGMA table_info(telemetry)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'session_id' not in columns:
                conn.execute("ALTER TABLE telemetry ADD COLUMN session_id TEXT")
                logger.info("Added session_id column to telemetry table")
            
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_telemetry_received_at
                ON telemetry(received_at)
            ''')
            
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_telemetry_gps_time
                ON telemetry(gps_time)
            ''')
            
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_telemetry_session
                ON telemetry(session_id)
            ''')
            
            # Flight sessions table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_time REAL NOT NULL,
                    end_time REAL,
                    callsign TEXT,
                    notes TEXT
                )
            ''')
            
            conn.commit()
        
        logger.info(f"Telemetry database initialized: {self.db_path}")
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get database connection"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn
    
    def insert(self, point: TelemetryPoint, session_id: str = None) -> int:
        """Insert a telemetry point"""
        sid = session_id or self.session_id
        with self._lock:
            with self._get_conn() as conn:
                cursor = conn.execute('''
                    INSERT INTO telemetry (
                        received_at, rssi, packet_seq,
                        latitude, longitude, altitude,
                        speed, heading, satellites, fix_type, gps_time,
                        battery_mv, cpu_temp, radio_temp,
                        image_id, image_progress, payload_rssi, session_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    point.received_at, point.rssi, point.packet_seq,
                    point.latitude, point.longitude, point.altitude,
                    point.speed, point.heading, point.satellites,
                    point.fix_type, point.gps_time,
                    point.battery_mv, point.cpu_temp, point.radio_temp,
                    point.image_id, point.image_progress, point.payload_rssi,
                    sid
                ))
                conn.commit()
                return cursor.lastrowid
    
    def query(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 1000
    ) -> List[TelemetryPoint]:
        """Query telemetry points"""
        with self._lock:
            query = "SELECT * FROM telemetry WHERE 1=1"
            params = []
            
            if start_time is not None:
                query += " AND received_at >= ?"
                params.append(start_time)
            
            if end_time is not None:
                query += " AND received_at <= ?"
                params.append(end_time)
            
            query += " ORDER BY received_at DESC LIMIT ?"
            params.append(limit)
            
            with self._get_conn() as conn:
                rows = conn.execute(query, params).fetchall()
            
            return [self._row_to_point(row) for row in rows]
    
    def get_track(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        min_interval_sec: float = 1.0,
        session_id: str = None
    ) -> List[Dict]:
        """
        Get GPS track for mapping
        
        Args:
            start_time: Start of time range
            end_time: End of time range
            min_interval_sec: Minimum interval between points (for thinning)
            session_id: Filter by session (None = all, 'current' = current session)
        
        Returns:
            List of {lat, lon, alt, time} dicts
        """
        with self._lock:
            query = """
                SELECT latitude, longitude, altitude, gps_time, received_at
                FROM telemetry
                WHERE latitude != 0 AND longitude != 0
            """
            params = []
            
            # Session filter
            if session_id == 'current' and self.session_id:
                query += " AND session_id = ?"
                params.append(self.session_id)
            elif session_id and session_id != 'all':
                query += " AND session_id = ?"
                params.append(session_id)
            
            if start_time is not None:
                query += " AND received_at >= ?"
                params.append(start_time)
            
            if end_time is not None:
                query += " AND received_at <= ?"
                params.append(end_time)
            
            query += " ORDER BY received_at ASC"
            
            with self._get_conn() as conn:
                rows = conn.execute(query, params).fetchall()
            
            # Thin the track if needed
            track = []
            last_time = 0
            
            for row in rows:
                if row['received_at'] - last_time >= min_interval_sec:
                    track.append({
                        'lat': row['latitude'],
                        'lon': row['longitude'],
                        'alt': row['altitude'],
                        'time': row['gps_time'],
                    })
                    last_time = row['received_at']
            
            return track
    
    def clear_track(self, session_id: str = None) -> int:
        """
        Clear track data for a session
        
        Args:
            session_id: Session to clear (None = current session)
        
        Returns:
            Number of points deleted
        """
        sid = session_id or self.session_id
        if not sid:
            return 0
        
        with self._lock:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    "DELETE FROM telemetry WHERE session_id = ?",
                    (sid,)
                )
                conn.commit()
                return cursor.rowcount
    
    def get_sessions(self) -> List[Dict]:
        """Get list of all sessions with telemetry data"""
        with self._lock:
            with self._get_conn() as conn:
                rows = conn.execute('''
                    SELECT session_id, 
                           COUNT(*) as point_count,
                           MIN(received_at) as start_time,
                           MAX(received_at) as end_time,
                           MAX(altitude) as max_altitude
                    FROM telemetry
                    WHERE session_id IS NOT NULL
                    GROUP BY session_id
                    ORDER BY start_time DESC
                ''').fetchall()
                
                return [
                    {
                        'session_id': row['session_id'],
                        'point_count': row['point_count'],
                        'start_time': row['start_time'],
                        'end_time': row['end_time'],
                        'max_altitude': row['max_altitude'],
                    }
                    for row in rows
                ]
    
    def get_stats(self) -> Dict:
        """Get database statistics"""
        with self._lock:
            with self._get_conn() as conn:
                count = conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
                
                if count > 0:
                    first = conn.execute(
                        "SELECT received_at FROM telemetry ORDER BY received_at ASC LIMIT 1"
                    ).fetchone()[0]
                    last = conn.execute(
                        "SELECT received_at FROM telemetry ORDER BY received_at DESC LIMIT 1"
                    ).fetchone()[0]
                    
                    max_alt = conn.execute(
                        "SELECT MAX(altitude) FROM telemetry"
                    ).fetchone()[0]
                else:
                    first = last = max_alt = None
                
                return {
                    'total_points': count,
                    'first_received': first,
                    'last_received': last,
                    'max_altitude': max_alt,
                }
    
    def _row_to_point(self, row: sqlite3.Row) -> TelemetryPoint:
        """Convert database row to TelemetryPoint"""
        return TelemetryPoint(
            received_at=row['received_at'],
            rssi=row['rssi'],
            packet_seq=row['packet_seq'],
            latitude=row['latitude'],
            longitude=row['longitude'],
            altitude=row['altitude'],
            speed=row['speed'],
            heading=row['heading'],
            satellites=row['satellites'],
            fix_type=row['fix_type'],
            gps_time=row['gps_time'],
            battery_mv=row['battery_mv'],
            cpu_temp=row['cpu_temp'],
            radio_temp=row['radio_temp'],
            image_id=row['image_id'],
            image_progress=row['image_progress'],
            payload_rssi=row['payload_rssi'],
        )
    
    def close(self):
        """Close database connection"""
        if self._conn:
            self._conn.close()
            self._conn = None


class TelemetryProcessor:
    """
    Main telemetry processing class
    
    Handles incoming telemetry, stores it, and provides analysis
    """
    
    def __init__(
        self,
        db_path: str,
        buffer_size: int = 1000,
        on_telemetry: Optional[Callable[[TelemetryPoint], None]] = None,
        on_alert: Optional[Callable[[str, str, Any], None]] = None,
        session_id: str = None
    ):
        """
        Initialize telemetry processor
        
        Args:
            db_path: Path to telemetry database
            buffer_size: Size of in-memory buffer
            on_telemetry: Callback for new telemetry
            on_alert: Callback for alerts (type, message, data)
            session_id: Current session identifier
        """
        self.session_id = session_id
        self.buffer = TelemetryBuffer(buffer_size)
        self.database = TelemetryDatabase(db_path, session_id=session_id)
        self.on_telemetry = on_telemetry
        self.on_alert = on_alert
        
        # Current state
        self._latest: Optional[TelemetryPoint] = None
        self._lock = Lock()
        
        # Alert thresholds
        self.alert_low_battery_mv = 3300
        self.alert_high_altitude_m = 30000
        self.alert_descent_rate_mps = 10.0
        
        # Statistics
        self.stats = {
            'packets_received': 0,
            'packets_invalid': 0,
            'alerts_triggered': 0,
        }
        
        # Previous values for rate calculations
        self._prev_altitude: Optional[float] = None
        self._prev_time: Optional[float] = None
    
    def set_session_id(self, session_id: str):
        """Update session ID (e.g. when starting new mission)"""
        self.session_id = session_id
        self.database.session_id = session_id
    
    def process_packet(
        self,
        payload: TelemetryPayload,
        rssi: int,
        packet_seq: int
    ) -> TelemetryPoint:
        """
        Process a received telemetry packet
        
        Args:
            payload: Decoded telemetry payload
            rssi: Received signal strength
            packet_seq: Packet sequence number
            
        Returns:
            Processed telemetry point
        """
        received_at = time.time()
        
        point = TelemetryPoint.from_payload(payload, received_at, rssi, packet_seq)
        
        with self._lock:
            self._latest = point
            self.stats['packets_received'] += 1
        
        # Store in buffer and database
        self.buffer.add(point)
        self.database.insert(point)
        
        # Check for alerts
        self._check_alerts(point)
        
        # Callback
        if self.on_telemetry:
            try:
                self.on_telemetry(point)
            except Exception as e:
                logger.error(f"Telemetry callback error: {e}")
        
        return point
    
    def _check_alerts(self, point: TelemetryPoint):
        """Check for alert conditions"""
        alerts = []
        
        # Low battery
        if point.battery_mv > 0 and point.battery_mv < self.alert_low_battery_mv:
            alerts.append(('low_battery', f"Low battery: {point.battery_mv}mV", point.battery_mv))
        
        # High altitude
        if point.altitude > self.alert_high_altitude_m:
            alerts.append(('high_altitude', f"High altitude: {point.altitude:.0f}m", point.altitude))
        
        # Descent rate - only check when above 500m to avoid ground-level GPS noise
        if self._prev_altitude is not None and self._prev_time is not None:
            dt = point.received_at - self._prev_time
            # Only calculate if we have reasonable time interval (>0.5s) and altitude (>500m)
            if dt > 0.5 and point.altitude > 500:
                descent_rate = (self._prev_altitude - point.altitude) / dt
                if descent_rate > self.alert_descent_rate_mps:
                    alerts.append((
                        'rapid_descent',
                        f"Rapid descent: {descent_rate:.1f} m/s",
                        descent_rate
                    ))
        
        # Update previous values
        self._prev_altitude = point.altitude
        self._prev_time = point.received_at
        
        # Trigger alerts
        for alert_type, message, data in alerts:
            self.stats['alerts_triggered'] += 1
            logger.warning(f"ALERT: {message}")
            if self.on_alert:
                try:
                    self.on_alert(alert_type, message, data)
                except Exception as e:
                    logger.error(f"Alert callback error: {e}")
    
    def get_latest(self) -> Optional[TelemetryPoint]:
        """Get the most recent telemetry point"""
        with self._lock:
            return self._latest
    
    def get_current_position(self) -> Optional[Dict]:
        """Get current position for mapping"""
        with self._lock:
            if self._latest is None:
                return None
            return {
                'lat': self._latest.latitude,
                'lon': self._latest.longitude,
                'alt': self._latest.altitude,
                'heading': self._latest.heading,
                'speed': self._latest.speed,
                'time': self._latest.gps_time,
            }
    
    def get_flight_stats(self) -> Dict:
        """Get current flight statistics"""
        db_stats = self.database.get_stats()
        
        with self._lock:
            latest = self._latest
        
        stats = {
            'total_packets': self.stats['packets_received'],
            'invalid_packets': self.stats['packets_invalid'],
            'alerts': self.stats['alerts_triggered'],
            **db_stats,
        }
        
        if latest:
            stats.update({
                'current_altitude': latest.altitude,
                'current_speed': latest.speed,
                'current_battery': latest.battery_mv,
                'current_satellites': latest.satellites,
                'last_rssi': latest.rssi,
            })
        
        return stats
    
    def export_csv(self, filepath: str, start_time: Optional[float] = None):
        """Export telemetry to CSV"""
        import csv
        
        points = self.database.query(start_time=start_time, limit=100000)
        
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            
            # Header
            writer.writerow([
                'received_at', 'gps_time', 'latitude', 'longitude', 'altitude',
                'speed', 'heading', 'satellites', 'fix_type',
                'battery_mv', 'cpu_temp', 'radio_temp',
                'rssi', 'packet_seq', 'image_id', 'image_progress'
            ])
            
            # Data
            for p in reversed(points):  # Chronological order
                writer.writerow([
                    p.received_at, p.gps_time, p.latitude, p.longitude, p.altitude,
                    p.speed, p.heading, p.satellites, p.fix_type,
                    p.battery_mv, p.cpu_temp, p.radio_temp,
                    p.rssi, p.packet_seq, p.image_id, p.image_progress
                ])
        
        logger.info(f"Exported {len(points)} telemetry points to {filepath}")
    
    def export_kml(self, filepath: str, start_time: Optional[float] = None):
        """Export flight track to KML"""
        track = self.database.get_track(start_time=start_time)
        
        kml_content = '''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
    <name>RaptorHab Flight Track</name>
    <Style id="flightPath">
        <LineStyle>
            <color>ff0000ff</color>
            <width>3</width>
        </LineStyle>
    </Style>
    <Placemark>
        <name>Flight Path</name>
        <styleUrl>#flightPath</styleUrl>
        <LineString>
            <altitudeMode>absolute</altitudeMode>
            <coordinates>
'''
        
        for point in track:
            kml_content += f"                {point['lon']},{point['lat']},{point['alt']}\n"
        
        kml_content += '''            </coordinates>
        </LineString>
    </Placemark>
</Document>
</kml>'''
        
        with open(filepath, 'w') as f:
            f.write(kml_content)
        
        logger.info(f"Exported KML track to {filepath}")
    
    def close(self):
        """Close resources"""
        self.database.close()
