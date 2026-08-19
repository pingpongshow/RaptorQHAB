"""
Mission recording and playback manager.
Automatically records telemetry, images, and flight data.
"""

import json
import csv
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass, field, asdict

from .telemetry import TelemetryPoint, Mission
from .config import get_data_directory


class MissionManager:
    """
    Manages mission recording and playback.
    
    Features:
    - Auto-record on first telemetry
    - Save telemetry to CSV and JSON
    - Track mission statistics
    - Copy images to mission folder
    """
    
    def __init__(self):
        self.current_mission: Optional[Mission] = None
        self.is_recording: bool = False
        self.is_auto_recording: bool = False
        
        # Recorded data
        self.recorded_telemetry: List[TelemetryPoint] = []
        self.recorded_images: List[str] = []
        
        # Mission folder
        self.mission_folder: Optional[Path] = None
        
        # Statistics tracking
        self.max_altitude: float = 0.0
        self.total_distance: float = 0.0
        self.last_position: Optional[tuple] = None
        
        # Auto-record settings
        self.auto_record_enabled: bool = True
        self.auto_record_triggered: bool = False
    
    def start_recording(self, name: str = None, auto: bool = False) -> bool:
        """
        Start recording a new mission.
        
        Args:
            name: Mission name (auto-generated if not provided)
            auto: Whether this is auto-triggered recording
        
        Returns:
            True if recording started successfully
        """
        if self.is_recording:
            return False
        
        # Create mission
        self.current_mission = Mission()
        
        if name:
            self.current_mission.name = name
        else:
            self.current_mission.name = datetime.now().strftime("Mission_%Y%m%d_%H%M%S")
        
        # Create mission folder
        missions_dir = get_data_directory() / "missions"
        self.mission_folder = missions_dir / self.current_mission.folder_name
        self.mission_folder.mkdir(parents=True, exist_ok=True)
        
        # Create subdirectories
        (self.mission_folder / "images").mkdir(exist_ok=True)
        (self.mission_folder / "telemetry").mkdir(exist_ok=True)
        
        # Reset state
        self.recorded_telemetry.clear()
        self.recorded_images.clear()
        self.max_altitude = 0.0
        self.total_distance = 0.0
        self.last_position = None
        
        self.is_recording = True
        self.is_auto_recording = auto
        
        # Save initial mission metadata
        self._save_mission_metadata()
        
        return True
    
    def stop_recording(self, save: bool = True) -> Optional[str]:
        """
        Stop recording and optionally save the mission.
        
        Args:
            save: Whether to save the mission data
        
        Returns:
            Path to mission folder if saved, None otherwise
        """
        if not self.is_recording:
            return None
        
        self.is_recording = False
        
        if save and self.current_mission and self.mission_folder:
            # Update mission stats
            self.current_mission.landing_time = datetime.now()
            self.current_mission.max_altitude = self.max_altitude
            self.current_mission.total_distance = self.total_distance
            self.current_mission.telemetry_count = len(self.recorded_telemetry)
            self.current_mission.image_count = len(self.recorded_images)
            
            # Save final data
            self._save_mission_metadata()
            self._save_telemetry_csv()
            self._save_telemetry_json()
            
            folder = str(self.mission_folder)
            
            # Reset
            self.current_mission = None
            self.mission_folder = None
            self.is_auto_recording = False
            
            return folder
        else:
            # Discard mission
            if self.mission_folder and self.mission_folder.exists():
                shutil.rmtree(self.mission_folder)
            
            self.current_mission = None
            self.mission_folder = None
            self.is_auto_recording = False
            
            return None
    
    def record_telemetry(self, telemetry: TelemetryPoint):
        """Record a telemetry point."""
        # Auto-start recording on first valid telemetry
        if self.auto_record_enabled and not self.is_recording and not self.auto_record_triggered:
            if telemetry.is_valid and telemetry.altitude > 50:
                self.auto_record_triggered = True
                self.start_recording(auto=True)
        
        if not self.is_recording:
            return
        
        self.recorded_telemetry.append(telemetry)
        
        # Update statistics
        if telemetry.altitude > self.max_altitude:
            self.max_altitude = telemetry.altitude
        
        # Track distance
        if telemetry.is_valid:
            current_pos = (telemetry.latitude, telemetry.longitude)
            if self.last_position:
                dist = self._haversine(
                    self.last_position[0], self.last_position[1],
                    current_pos[0], current_pos[1]
                )
                self.total_distance += dist
            self.last_position = current_pos
        
        # Set launch time on first telemetry
        if self.current_mission and not self.current_mission.launch_time:
            if telemetry.altitude > 100 and telemetry.vertical_speed > 1:
                self.current_mission.launch_time = datetime.now()
        
        # Update mission stats
        if self.current_mission:
            self.current_mission.max_altitude = self.max_altitude
            self.current_mission.telemetry_count = len(self.recorded_telemetry)
    
    def record_image(self, image_path: str, image_id: int):
        """Record an image to the mission."""
        if not self.is_recording or not self.mission_folder:
            return
        
        self.recorded_images.append(image_path)
        
        # Copy image to mission folder
        src = Path(image_path)
        if src.exists():
            dst = self.mission_folder / "images" / src.name
            shutil.copy2(src, dst)
        
        if self.current_mission:
            self.current_mission.image_count = len(self.recorded_images)
    
    def _save_mission_metadata(self):
        """Save mission metadata to JSON."""
        if not self.current_mission or not self.mission_folder:
            return
        
        metadata_path = self.mission_folder / "mission.json"
        with open(metadata_path, 'w') as f:
            json.dump(self.current_mission.to_dict(), f, indent=2)
    
    def _save_telemetry_csv(self):
        """Save telemetry to CSV."""
        if not self.recorded_telemetry or not self.mission_folder:
            return
        
        csv_path = self.mission_folder / "telemetry" / "telemetry.csv"
        
        with open(csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            
            # Header
            writer.writerow([
                'timestamp', 'sequence', 'latitude', 'longitude', 'altitude_m',
                'speed_ms', 'heading', 'vertical_speed_ms', 'satellites',
                'battery_mv', 'cpu_temp_c', 'rssi', 'snr'
            ])
            
            # Data
            for t in self.recorded_telemetry:
                writer.writerow([
                    t.timestamp.isoformat(),
                    t.sequence,
                    t.latitude,
                    t.longitude,
                    t.altitude,
                    t.speed,
                    t.heading,
                    t.vertical_speed,
                    t.satellites,
                    t.battery_mv,
                    t.cpu_temp,
                    t.rx_rssi,
                    t.rx_snr
                ])
    
    def _save_telemetry_json(self):
        """Save telemetry to JSON."""
        if not self.recorded_telemetry or not self.mission_folder:
            return
        
        json_path = self.mission_folder / "telemetry" / "telemetry.json"
        
        data = [t.to_dict() for t in self.recorded_telemetry]
        
        with open(json_path, 'w') as f:
            json.dump(data, f, indent=2)
    
    @staticmethod
    def list_missions() -> List[Mission]:
        """List all recorded missions."""
        missions = []
        missions_dir = get_data_directory() / "missions"
        
        if not missions_dir.exists():
            return missions
        
        for folder in sorted(missions_dir.iterdir(), reverse=True):
            if not folder.is_dir():
                continue
            
            metadata_path = folder / "mission.json"
            if metadata_path.exists():
                try:
                    with open(metadata_path) as f:
                        data = json.load(f)
                    mission = Mission.from_dict(data)
                    missions.append(mission)
                except Exception:
                    pass
        
        return missions
    
    @staticmethod
    def load_mission(mission_id: str) -> Optional[dict]:
        """Load a mission's full data."""
        missions_dir = get_data_directory() / "missions"
        
        for folder in missions_dir.iterdir():
            metadata_path = folder / "mission.json"
            if metadata_path.exists():
                try:
                    with open(metadata_path) as f:
                        data = json.load(f)
                    if data.get('id') == mission_id:
                        # Load telemetry
                        telemetry_path = folder / "telemetry" / "telemetry.json"
                        if telemetry_path.exists():
                            with open(telemetry_path) as f:
                                data['telemetry'] = json.load(f)
                        
                        # List images
                        images_dir = folder / "images"
                        if images_dir.exists():
                            data['images'] = [str(p) for p in images_dir.glob("*.webp")]
                        
                        data['folder'] = str(folder)
                        return data
                except Exception:
                    pass
        
        return None
    
    @staticmethod
    def delete_mission(mission_id: str) -> bool:
        """Delete a mission."""
        missions_dir = get_data_directory() / "missions"
        
        for folder in missions_dir.iterdir():
            metadata_path = folder / "mission.json"
            if metadata_path.exists():
                try:
                    with open(metadata_path) as f:
                        data = json.load(f)
                    if data.get('id') == mission_id:
                        shutil.rmtree(folder)
                        return True
                except Exception:
                    pass
        
        return False
    
    @staticmethod
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points in meters."""
        import math
        R = 6371000
        
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        
        a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        
        return R * c
    
    @property
    def has_unsaved_recording(self) -> bool:
        """Check if there's an unsaved recording."""
        return self.is_recording and len(self.recorded_telemetry) > 0
