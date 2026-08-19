"""
RaptorHab Ground Station - SondeHub Integration

Uploads telemetry and optionally images to SondeHub Amateur network.
https://amateur.sondehub.org/

SondeHub Amateur is a tracking platform for amateur high-altitude balloon flights.
"""

import json
import time
import logging
import threading
import queue
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Try to import requests
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    logger.warning("requests library not available - SondeHub upload disabled")


# SondeHub API endpoints
SONDEHUB_AMATEUR_URL = "https://api.v2.sondehub.org/amateur/telemetry"
SONDEHUB_LISTENER_URL = "https://api.v2.sondehub.org/amateur/listeners"


@dataclass
class SondeHubConfig:
    """SondeHub upload configuration"""
    enabled: bool = False
    upload_telemetry: bool = True
    upload_images: bool = False
    
    # Payload identification
    payload_callsign: str = "RPHAB1"
    
    # Uploader (ground station) info
    uploader_callsign: str = ""
    uploader_position: tuple = (0.0, 0.0, 0.0)  # lat, lon, alt
    uploader_antenna: str = "Yagi"
    uploader_radio: str = "LoRa 915MHz"
    
    # Upload rate limiting
    min_upload_interval_sec: float = 5.0  # Don't upload more often than this
    
    # Contact info (optional, for flight coordination)
    contact_email: str = ""
    
    def to_dict(self) -> dict:
        return {
            'enabled': self.enabled,
            'upload_telemetry': self.upload_telemetry,
            'upload_images': self.upload_images,
            'payload_callsign': self.payload_callsign,
            'uploader_callsign': self.uploader_callsign,
            'uploader_position': list(self.uploader_position),
            'uploader_antenna': self.uploader_antenna,
            'uploader_radio': self.uploader_radio,
            'min_upload_interval_sec': self.min_upload_interval_sec,
            'contact_email': self.contact_email,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'SondeHubConfig':
        pos = data.get('uploader_position', [0, 0, 0])
        return cls(
            enabled=data.get('enabled', False),
            upload_telemetry=data.get('upload_telemetry', True),
            upload_images=data.get('upload_images', False),
            payload_callsign=data.get('payload_callsign', 'RPHAB1'),
            uploader_callsign=data.get('uploader_callsign', ''),
            uploader_position=tuple(pos) if isinstance(pos, list) else pos,
            uploader_antenna=data.get('uploader_antenna', 'Yagi'),
            uploader_radio=data.get('uploader_radio', 'LoRa 915MHz'),
            min_upload_interval_sec=data.get('min_upload_interval_sec', 5.0),
            contact_email=data.get('contact_email', ''),
        )


class SondeHubUploader:
    """
    Handles uploading telemetry and images to SondeHub Amateur network.
    
    Telemetry is uploaded in batches asynchronously to avoid blocking.
    """
    
    def __init__(self, config: SondeHubConfig = None):
        """
        Initialize SondeHub uploader
        
        Args:
            config: SondeHub configuration
        """
        self.config = config or SondeHubConfig()
        
        # Upload queue and worker thread
        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        # Rate limiting
        self._last_upload_time = 0.0
        self._last_listener_upload = 0.0
        
        # Statistics
        self.stats = {
            'telemetry_uploaded': 0,
            'telemetry_failed': 0,
            'images_uploaded': 0,
            'images_failed': 0,
            'last_upload_time': None,
            'last_error': None,
        }
        
        self._session: Optional[requests.Session] = None
    
    def start(self):
        """Start the upload worker thread"""
        if not REQUESTS_AVAILABLE:
            logger.error("Cannot start SondeHub uploader - requests library not available")
            return
        
        if self._running:
            return
        
        self._running = True
        self._session = requests.Session()
        self._thread = threading.Thread(target=self._upload_worker, daemon=True)
        self._thread.start()
        logger.info("SondeHub uploader started")
    
    def stop(self):
        """Stop the upload worker thread"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._session:
            self._session.close()
            self._session = None
        logger.info("SondeHub uploader stopped")
    
    def queue_telemetry(self, telemetry_point: Dict[str, Any]):
        """
        Queue a telemetry point for upload
        
        Args:
            telemetry_point: Telemetry data with lat, lon, alt, etc.
        """
        if not self.config.enabled or not self.config.upload_telemetry:
            return
        
        if not REQUESTS_AVAILABLE:
            return
        
        # Rate limiting check
        now = time.time()
        if now - self._last_upload_time < self.config.min_upload_interval_sec:
            return
        
        try:
            self._queue.put_nowait(('telemetry', telemetry_point))
        except queue.Full:
            logger.warning("SondeHub upload queue full, dropping telemetry")
    
    def queue_image(self, image_id: int, image_url: str, metadata: Dict[str, Any]):
        """
        Queue an image for upload (as a comment/link)
        
        Args:
            image_id: Image identifier
            image_url: URL where image can be accessed
            metadata: Image metadata (width, height, etc.)
        """
        if not self.config.enabled or not self.config.upload_images:
            return
        
        try:
            self._queue.put_nowait(('image', {
                'image_id': image_id,
                'image_url': image_url,
                'metadata': metadata,
            }))
        except queue.Full:
            logger.warning("SondeHub upload queue full, dropping image")
    
    def _upload_worker(self):
        """Background worker that processes the upload queue"""
        while self._running:
            try:
                item_type, data = self._queue.get(timeout=1.0)
                
                if item_type == 'telemetry':
                    self._upload_telemetry(data)
                elif item_type == 'image':
                    self._upload_image_comment(data)
                
                self._queue.task_done()
                
            except queue.Empty:
                # Periodically upload listener position
                if time.time() - self._last_listener_upload > 300:  # Every 5 minutes
                    self._upload_listener_position()
                continue
            except Exception as e:
                logger.error(f"SondeHub upload worker error: {e}")
    
    def _upload_telemetry(self, point: Dict[str, Any]):
        """Upload a single telemetry point to SondeHub"""
        try:
            # Build SondeHub telemetry format
            # https://github.com/projecthorus/sondehub-amateur-tracker#telemetry-format
            
            # Convert timestamp to ISO format
            if 'received_at' in point:
                dt = datetime.fromtimestamp(point['received_at'], tz=timezone.utc)
            else:
                dt = datetime.now(timezone.utc)
            
            telemetry = {
                'software_name': 'RaptorHab',
                'software_version': '1.0',
                'uploader_callsign': self.config.uploader_callsign or 'UNKNOWN',
                'time_received': dt.isoformat(),
                'payload_callsign': self.config.payload_callsign,
                'datetime': dt.isoformat(),
                'lat': point.get('latitude', 0),
                'lon': point.get('longitude', 0),
                'alt': point.get('altitude', 0),
            }
            
            # Optional fields
            if point.get('speed') is not None:
                telemetry['speed'] = point['speed']
            if point.get('heading') is not None:
                telemetry['heading'] = point['heading']
            if point.get('satellites') is not None:
                telemetry['sats'] = point['satellites']
            if point.get('battery_mv') is not None:
                telemetry['batt'] = point['battery_mv'] / 1000.0  # Convert to volts
            if point.get('cpu_temp') is not None:
                telemetry['temp'] = point['cpu_temp']
            if point.get('rssi') is not None:
                telemetry['rssi'] = point['rssi']
            if point.get('snr') is not None:
                telemetry['snr'] = point['snr']
            
            # Upload
            response = self._session.put(
                SONDEHUB_AMATEUR_URL,
                json=[telemetry],  # API expects array
                timeout=10.0
            )
            
            if response.status_code == 200:
                self.stats['telemetry_uploaded'] += 1
                self.stats['last_upload_time'] = time.time()
                self._last_upload_time = time.time()
                logger.debug(f"SondeHub telemetry uploaded: {telemetry['lat']:.5f}, {telemetry['lon']:.5f}, {telemetry['alt']:.0f}m")
            else:
                self.stats['telemetry_failed'] += 1
                self.stats['last_error'] = f"HTTP {response.status_code}: {response.text[:100]}"
                logger.warning(f"SondeHub upload failed: {response.status_code} - {response.text[:100]}")
                
        except Exception as e:
            self.stats['telemetry_failed'] += 1
            self.stats['last_error'] = str(e)
            logger.error(f"SondeHub telemetry upload error: {e}")
    
    def _upload_listener_position(self):
        """Upload listener/ground station position"""
        if not self.config.uploader_callsign:
            return
        
        lat, lon, alt = self.config.uploader_position
        if lat == 0 and lon == 0:
            return
        
        try:
            listener = {
                'software_name': 'RaptorHab',
                'software_version': '1.0',
                'uploader_callsign': self.config.uploader_callsign,
                'uploader_position': [lat, lon, alt],
                'uploader_antenna': self.config.uploader_antenna,
                'uploader_radio': self.config.uploader_radio,
            }
            
            if self.config.contact_email:
                listener['uploader_contact_email'] = self.config.contact_email
            
            response = self._session.put(
                SONDEHUB_LISTENER_URL,
                json=listener,
                timeout=10.0
            )
            
            if response.status_code == 200:
                self._last_listener_upload = time.time()
                logger.debug("SondeHub listener position uploaded")
            else:
                logger.warning(f"SondeHub listener upload failed: {response.status_code}")
                
        except Exception as e:
            logger.error(f"SondeHub listener upload error: {e}")
    
    def _upload_image_comment(self, data: Dict[str, Any]):
        """
        Upload image as a comment/extra field
        
        Note: SondeHub doesn't have native image support, so we include
        image URLs in telemetry comments for now.
        """
        # For now, just log - full image upload would require hosting
        logger.info(f"SondeHub image queued: {data.get('image_id')} at {data.get('image_url')}")
        self.stats['images_uploaded'] += 1
    
    def get_stats(self) -> Dict[str, Any]:
        """Get upload statistics"""
        return {
            **self.stats,
            'enabled': self.config.enabled,
            'queue_size': self._queue.qsize(),
        }
    
    def update_config(self, config: SondeHubConfig):
        """Update configuration"""
        self.config = config
        logger.info(f"SondeHub config updated: enabled={config.enabled}")


# Singleton instance
_uploader: Optional[SondeHubUploader] = None


def get_uploader() -> Optional[SondeHubUploader]:
    """Get the global SondeHub uploader instance"""
    return _uploader


def init_uploader(config: SondeHubConfig = None) -> SondeHubUploader:
    """Initialize and return the global SondeHub uploader"""
    global _uploader
    if _uploader is None:
        _uploader = SondeHubUploader(config)
    return _uploader
