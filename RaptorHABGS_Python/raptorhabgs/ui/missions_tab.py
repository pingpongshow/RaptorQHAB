"""
Missions Tab - Mission recording, list, and export functionality.
"""

import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGroupBox, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QFrame,
    QScrollArea, QMessageBox, QFileDialog, QInputDialog
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from ..core.mission_manager import MissionManager


class MissionListItem(QListWidgetItem):
    """Custom list item for mission display."""
    
    def __init__(self, mission):
        super().__init__()
        self.mission = mission
        self.mission_id = mission.id
        
        # Create display text
        date_str = mission.created_at.strftime("%Y-%m-%d %H:%M")
        name = mission.name or "Untitled Mission"
        
        text = f"{name}\n{date_str}"
        if mission.max_altitude > 0:
            text += f" • {mission.max_altitude:.0f}m"
        if mission.telemetry_count > 0:
            text += f" • {mission.telemetry_count} pts"
        
        self.setText(text)


class MissionsTab(QWidget):
    """Missions tab with recording and playback."""
    
    def __init__(self, mission_manager: MissionManager):
        super().__init__()
        
        self.mission_manager = mission_manager
        self.selected_mission_id = None
        
        self._setup_ui()
        self.refresh_missions()
    
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Left panel - Mission list
        list_panel = self._create_list_panel()
        layout.addWidget(list_panel)
        
        # Right panel - Mission details
        detail_panel = self._create_detail_panel()
        layout.addWidget(detail_panel, 1)
    
    def _create_list_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMaximumWidth(350)
        panel.setMinimumWidth(300)
        panel.setStyleSheet("background-color: #252525;")
        
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Recording indicator
        self.recording_frame = QFrame()
        self.recording_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(255, 68, 68, 0.2);
                border: 1px solid #ff4444;
                border-radius: 8px;
                margin: 10px;
            }
        """)
        self.recording_frame.setVisible(False)
        
        rec_layout = QHBoxLayout(self.recording_frame)
        rec_layout.setContentsMargins(15, 10, 15, 10)
        
        # Recording dot (animated via stylesheet)
        rec_dot = QLabel("●")
        rec_dot.setStyleSheet("color: #ff4444; font-size: 16px;")
        rec_layout.addWidget(rec_dot)
        
        rec_info = QVBoxLayout()
        rec_title = QLabel("Recording")
        rec_title.setStyleSheet("font-weight: bold;")
        rec_info.addWidget(rec_title)
        
        self.recording_stats_label = QLabel("0 points")
        self.recording_stats_label.setStyleSheet("color: #888; font-size: 11px;")
        rec_info.addWidget(self.recording_stats_label)
        rec_layout.addLayout(rec_info)
        
        rec_layout.addStretch()
        
        self.stop_recording_btn = QPushButton("Stop")
        self.stop_recording_btn.setStyleSheet("""
            QPushButton {
                background-color: #ff4444;
                color: white;
                padding: 5px 15px;
                border-radius: 4px;
            }
        """)
        self.stop_recording_btn.clicked.connect(self._stop_recording)
        rec_layout.addWidget(self.stop_recording_btn)
        
        layout.addWidget(self.recording_frame)
        
        # New mission button
        new_btn_frame = QFrame()
        new_btn_layout = QVBoxLayout(new_btn_frame)
        new_btn_layout.setContentsMargins(10, 10, 10, 10)
        
        self.new_mission_btn = QPushButton("+ Start New Mission")
        self.new_mission_btn.setStyleSheet("""
            QPushButton {
                background-color: #4488ff;
                color: white;
                padding: 10px;
                font-weight: bold;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #5599ff;
            }
        """)
        self.new_mission_btn.clicked.connect(self._start_new_mission)
        new_btn_layout.addWidget(self.new_mission_btn)
        
        layout.addWidget(new_btn_frame)
        
        # Mission list
        self.mission_list = QListWidget()
        self.mission_list.setStyleSheet("""
            QListWidget {
                background-color: #252525;
                border: none;
            }
            QListWidget::item {
                padding: 15px;
                border-bottom: 1px solid #3a3a3a;
            }
            QListWidget::item:selected {
                background-color: #2d2d2d;
                border-left: 3px solid #4488ff;
            }
            QListWidget::item:hover {
                background-color: #2d2d2d;
            }
        """)
        self.mission_list.itemClicked.connect(self._on_mission_selected)
        layout.addWidget(self.mission_list)
        
        return panel
    
    def _create_detail_panel(self) -> QWidget:
        panel = QWidget()
        
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)
        
        # Placeholder for when no mission is selected
        self.no_selection_label = QLabel("Select a mission to view details")
        self.no_selection_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.no_selection_label.setStyleSheet("color: #888; font-size: 14px;")
        layout.addWidget(self.no_selection_label)
        
        # Mission details container
        self.details_widget = QWidget()
        self.details_widget.setVisible(False)
        details_layout = QVBoxLayout(self.details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(20)
        
        # Header
        header_layout = QHBoxLayout()
        
        self.mission_title = QLabel("Mission Title")
        self.mission_title.setFont(QFont("Arial", 18, QFont.Weight.Bold))
        header_layout.addWidget(self.mission_title)
        
        header_layout.addStretch()
        
        self.mission_date = QLabel("Date")
        self.mission_date.setStyleSheet("color: #888;")
        header_layout.addWidget(self.mission_date)
        
        details_layout.addLayout(header_layout)
        
        # Stats grid
        stats_frame = QFrame()
        stats_frame.setStyleSheet("""
            QFrame {
                background-color: #2d2d2d;
                border-radius: 8px;
                padding: 20px;
            }
        """)
        stats_layout = QHBoxLayout(stats_frame)
        stats_layout.setSpacing(30)
        
        self.stat_widgets = {}
        stat_items = [
            ("max_altitude", "Max Altitude", "m"),
            ("total_distance", "Distance", "km"),
            ("duration", "Duration", ""),
            ("telemetry_count", "Telemetry", "pts"),
            ("image_count", "Images", ""),
            ("burst_altitude", "Burst Alt", "m"),
        ]
        
        for key, label, unit in stat_items:
            stat_widget = self._create_stat_widget(label, "--", unit)
            self.stat_widgets[key] = stat_widget
            stats_layout.addWidget(stat_widget)
        
        details_layout.addWidget(stats_frame)
        
        # Actions
        actions_layout = QHBoxLayout()
        
        self.export_csv_btn = QPushButton("📥 Export CSV")
        self.export_csv_btn.clicked.connect(self._export_csv)
        actions_layout.addWidget(self.export_csv_btn)
        
        self.export_kml_btn = QPushButton("🗺️ Export KML")
        self.export_kml_btn.clicked.connect(self._export_kml)
        actions_layout.addWidget(self.export_kml_btn)
        
        self.open_folder_btn = QPushButton("📁 Open Folder")
        self.open_folder_btn.clicked.connect(self._open_folder)
        actions_layout.addWidget(self.open_folder_btn)
        
        actions_layout.addStretch()
        
        self.delete_btn = QPushButton("🗑️ Delete Mission")
        self.delete_btn.setStyleSheet("""
            QPushButton {
                background-color: #8b0000;
                color: white;
            }
            QPushButton:hover {
                background-color: #a00000;
            }
        """)
        self.delete_btn.clicked.connect(self._delete_mission)
        actions_layout.addWidget(self.delete_btn)
        
        details_layout.addLayout(actions_layout)
        
        # Additional info
        info_group = QGroupBox("Mission Details")
        info_layout = QVBoxLayout(info_group)
        
        self.launch_time_label = QLabel("Launch Time: --")
        self.landing_time_label = QLabel("Landing Time: --")
        self.folder_label = QLabel("Folder: --")
        self.folder_label.setWordWrap(True)
        self.folder_label.setStyleSheet("color: #888; font-size: 11px;")
        
        info_layout.addWidget(self.launch_time_label)
        info_layout.addWidget(self.landing_time_label)
        info_layout.addWidget(self.folder_label)
        
        details_layout.addWidget(info_group)
        
        details_layout.addStretch()
        
        layout.addWidget(self.details_widget)
        
        return panel
    
    def _create_stat_widget(self, label: str, value: str, unit: str) -> QWidget:
        """Create a stat display widget."""
        widget = QFrame()
        widget.setStyleSheet("background-color: transparent;")
        
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(5)
        
        value_label = QLabel(value)
        value_label.setFont(QFont("Arial", 24, QFont.Weight.Bold))
        value_label.setStyleSheet("color: #4488ff;")
        value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value_label.setObjectName("value")
        layout.addWidget(value_label)
        
        label_widget = QLabel(f"{label}" + (f" ({unit})" if unit else ""))
        label_widget.setStyleSheet("color: #888; font-size: 11px;")
        label_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label_widget)
        
        return widget
    
    def _set_stat_value(self, key: str, value: str):
        """Set a stat widget value."""
        widget = self.stat_widgets.get(key)
        if widget:
            value_label = widget.findChild(QLabel, "value")
            if value_label:
                value_label.setText(value)
    
    def refresh_missions(self):
        """Refresh the mission list."""
        self.mission_list.clear()
        
        missions = MissionManager.list_missions()
        for mission in missions:
            item = MissionListItem(mission)
            self.mission_list.addItem(item)
    
    def update_recording_status(self, is_recording: bool, telemetry_count: int):
        """Update the recording indicator."""
        self.recording_frame.setVisible(is_recording)
        self.recording_stats_label.setText(f"{telemetry_count} points")
    
    def _start_new_mission(self):
        """Start a new mission."""
        name, ok = QInputDialog.getText(
            self, "New Mission", "Mission name:",
            text=f"Mission {datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        
        if ok and name:
            self.mission_manager.start_recording(name)
            self.update_recording_status(True, 0)
            self.refresh_missions()
    
    def _stop_recording(self):
        """Stop current recording."""
        if self.mission_manager.is_recording:
            reply = QMessageBox.question(
                self, "Stop Recording",
                "Stop recording and save this mission?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                self.mission_manager.stop_recording(save=True)
                self.update_recording_status(False, 0)
                self.refresh_missions()
    
    def _on_mission_selected(self, item: MissionListItem):
        """Handle mission selection."""
        self.selected_mission_id = item.mission_id
        
        # Load full mission data
        mission_data = MissionManager.load_mission(item.mission_id)
        if not mission_data:
            return
        
        self.no_selection_label.setVisible(False)
        self.details_widget.setVisible(True)
        
        # Update header
        self.mission_title.setText(mission_data.get('name', 'Untitled Mission'))
        
        created_at = mission_data.get('created_at')
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except:
                created_at = None
        
        if created_at:
            self.mission_date.setText(created_at.strftime("%Y-%m-%d %H:%M:%S"))
        
        # Update stats
        max_alt = mission_data.get('max_altitude', 0)
        self._set_stat_value('max_altitude', f"{max_alt:.0f}")
        
        total_dist = mission_data.get('total_distance', 0)
        self._set_stat_value('total_distance', f"{total_dist/1000:.1f}")
        
        # Calculate duration
        launch_time = mission_data.get('launch_time')
        landing_time = mission_data.get('landing_time')
        
        if launch_time and landing_time:
            if isinstance(launch_time, str):
                launch_time = datetime.fromisoformat(launch_time)
            if isinstance(landing_time, str):
                landing_time = datetime.fromisoformat(landing_time)
            
            duration = (landing_time - launch_time).total_seconds()
            hours = int(duration // 3600)
            minutes = int((duration % 3600) // 60)
            if hours > 0:
                self._set_stat_value('duration', f"{hours}h {minutes}m")
            else:
                self._set_stat_value('duration', f"{minutes}m")
            
            self.launch_time_label.setText(f"Launch Time: {launch_time.strftime('%H:%M:%S')}")
            self.landing_time_label.setText(f"Landing Time: {landing_time.strftime('%H:%M:%S')}")
        else:
            self._set_stat_value('duration', "--")
            self.launch_time_label.setText("Launch Time: --")
            self.landing_time_label.setText("Landing Time: --")
        
        self._set_stat_value('telemetry_count', str(mission_data.get('telemetry_count', 0)))
        self._set_stat_value('image_count', str(mission_data.get('image_count', 0)))
        
        burst_alt = mission_data.get('burst_altitude')
        if burst_alt:
            self._set_stat_value('burst_altitude', f"{burst_alt:.0f}")
        else:
            self._set_stat_value('burst_altitude', "--")
        
        # Folder path
        folder = mission_data.get('folder', '')
        self.folder_label.setText(f"Folder: {folder}")
        self.current_folder = folder
    
    def _export_csv(self):
        """Export mission to CSV."""
        if not self.selected_mission_id:
            return
        
        mission_data = MissionManager.load_mission(self.selected_mission_id)
        if not mission_data:
            return
        
        name = mission_data.get('name', 'mission')
        dest_path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV",
            str(Path.home() / f"{name}.csv"),
            "CSV Files (*.csv)"
        )
        
        if not dest_path:
            return
        
        # Write CSV
        import csv
        
        telemetry = mission_data.get('telemetry', [])
        
        with open(dest_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'latitude', 'longitude', 'altitude_m',
                'speed_ms', 'heading', 'vertical_speed_ms', 'satellites',
                'battery_mv', 'cpu_temp_c', 'rssi', 'snr'
            ])
            
            for t in telemetry:
                writer.writerow([
                    t.get('timestamp', ''),
                    t.get('latitude', 0),
                    t.get('longitude', 0),
                    t.get('altitude', 0),
                    t.get('speed', 0),
                    t.get('heading', 0),
                    t.get('vertical_speed', 0),
                    t.get('satellites', 0),
                    t.get('battery_mv', 0),
                    t.get('cpu_temp', 0),
                    t.get('rx_rssi', 0),
                    t.get('rx_snr', 0),
                ])
        
        QMessageBox.information(self, "Exported", f"Mission exported to:\n{dest_path}")
    
    def _export_kml(self):
        """Export mission track to KML."""
        if not self.selected_mission_id:
            return
        
        mission_data = MissionManager.load_mission(self.selected_mission_id)
        if not mission_data:
            return
        
        name = mission_data.get('name', 'mission')
        dest_path, _ = QFileDialog.getSaveFileName(
            self, "Export KML",
            str(Path.home() / f"{name}.kml"),
            "KML Files (*.kml)"
        )
        
        if not dest_path:
            return
        
        telemetry = mission_data.get('telemetry', [])
        
        # Build KML
        kml = f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{name}</name>
    <Style id="track">
      <LineStyle>
        <color>ff0088ff</color>
        <width>3</width>
      </LineStyle>
    </Style>
    <Placemark>
      <name>Flight Track</name>
      <styleUrl>#track</styleUrl>
      <LineString>
        <altitudeMode>absolute</altitudeMode>
        <coordinates>
'''
        
        for t in telemetry:
            lat = t.get('latitude', 0)
            lon = t.get('longitude', 0)
            alt = t.get('altitude', 0)
            if lat != 0 or lon != 0:
                kml += f"          {lon},{lat},{alt}\n"
        
        kml += '''        </coordinates>
      </LineString>
    </Placemark>
  </Document>
</kml>'''
        
        with open(dest_path, 'w') as f:
            f.write(kml)
        
        QMessageBox.information(self, "Exported", f"Track exported to:\n{dest_path}")
    
    def _open_folder(self):
        """Open mission folder."""
        if hasattr(self, 'current_folder') and self.current_folder:
            folder = Path(self.current_folder)
            if folder.exists():
                if sys.platform == "win32":
                    os.startfile(str(folder))
                elif sys.platform == "darwin":
                    subprocess.run(["open", str(folder)])
                else:
                    subprocess.run(["xdg-open", str(folder)])
    
    def _delete_mission(self):
        """Delete selected mission."""
        if not self.selected_mission_id:
            return
        
        reply = QMessageBox.warning(
            self, "Delete Mission",
            "Are you sure you want to delete this mission?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            if MissionManager.delete_mission(self.selected_mission_id):
                self.selected_mission_id = None
                self.no_selection_label.setVisible(True)
                self.details_widget.setVisible(False)
                self.refresh_missions()
            else:
                QMessageBox.critical(self, "Error", "Failed to delete mission.")
