"""
Main application window with tabbed interface.
"""

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QStatusBar, QToolBar, 
    QLabel, QMessageBox, QTabWidget, QInputDialog
)
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction

from ..core.ground_station import GroundStationManager
from ..core.gps_manager import GPSManager
from ..core.sondehub import SondeHubManager
from ..core.prediction import LandingPredictionManager
from ..core.mission_manager import MissionManager
from ..core.config import get_config, save_config

from .tracking_tab import TrackingTab
from .prediction_tab import PredictionTab
from .images_tab import ImagesTab
from .missions_tab import MissionsTab
from .settings_dialogs import SettingsDialog


class MainWindow(QMainWindow):
    """Main application window with tabbed interface."""
    
    def __init__(self):
        super().__init__()
        
        self.setWindowTitle("RaptorHabGS - Ground Station")
        self.setMinimumSize(1100, 750)
        self.resize(1400, 900)
        
        # Initialize managers
        self.config = get_config()
        self.ground_station = GroundStationManager()
        self.gps_manager = GPSManager()
        self.sondehub = SondeHubManager()
        self.prediction_manager = LandingPredictionManager()
        self.mission_manager = MissionManager()
        
        # Apply config
        self.mission_manager.auto_record_enabled = self.config.auto_record
        self.sondehub.set_config(self.config.sondehub)
        
        # Setup UI
        self._setup_ui()
        self._setup_menubar()
        self._setup_toolbar()
        self._setup_statusbar()
        self._connect_signals()
        
        # Auto-connect GPS if configured
        if self.config.gps.enabled and self.config.gps.port:
            QTimer.singleShot(500, self._auto_connect_gps)
    
    def _setup_ui(self):
        """Setup the main UI layout."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Create tab widget
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        
        # Create tabs
        self.tracking_tab = TrackingTab(
            self.ground_station, self.gps_manager, self.sondehub
        )
        self.prediction_tab = PredictionTab(
            self.ground_station, self.gps_manager, self.prediction_manager
        )
        self.images_tab = ImagesTab(self.ground_station)
        self.missions_tab = MissionsTab(self.mission_manager)
        
        # Add tabs
        self.tabs.addTab(self.tracking_tab, "📍 Live Tracking")
        self.tabs.addTab(self.prediction_tab, "🎯 Landing Prediction")
        self.tabs.addTab(self.images_tab, "🖼️ Images")
        self.tabs.addTab(self.missions_tab, "📁 Missions")
        
        layout.addWidget(self.tabs)
        
        # Connect tab signals
        self.tracking_tab.start_requested.connect(self._start_receiving)
        self.tracking_tab.stop_requested.connect(self._stop_receiving)
    
    def _setup_menubar(self):
        """Setup the menu bar."""
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu("&File")
        
        settings_action = QAction("&Settings...", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._show_settings)
        file_menu.addAction(settings_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("E&xit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Radio menu
        radio_menu = menubar.addMenu("&Radio")
        
        self.start_action = QAction("&Start Receiving", self)
        self.start_action.setShortcut("Ctrl+R")
        self.start_action.triggered.connect(self._start_receiving)
        radio_menu.addAction(self.start_action)
        
        self.stop_action = QAction("S&top Receiving", self)
        self.stop_action.setShortcut("Ctrl+.")
        self.stop_action.setEnabled(False)
        self.stop_action.triggered.connect(self._stop_receiving)
        radio_menu.addAction(self.stop_action)
        
        # Mission menu
        mission_menu = menubar.addMenu("&Mission")
        
        start_mission_action = QAction("Start &New Mission...", self)
        start_mission_action.triggered.connect(self._start_new_mission)
        mission_menu.addAction(start_mission_action)
        
        stop_mission_action = QAction("&Stop Recording", self)
        stop_mission_action.triggered.connect(self._stop_mission)
        mission_menu.addAction(stop_mission_action)
        
        # View menu
        view_menu = menubar.addMenu("&View")
        
        clear_track_action = QAction("&Clear Track", self)
        clear_track_action.triggered.connect(self._clear_track)
        view_menu.addAction(clear_track_action)
        
        center_map_action = QAction("Center &Map on Payload", self)
        center_map_action.setShortcut("Ctrl+M")
        center_map_action.triggered.connect(self._center_on_payload)
        view_menu.addAction(center_map_action)
        
        # Help menu
        help_menu = menubar.addMenu("&Help")
        
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)
    
    def _setup_toolbar(self):
        """Setup the toolbar."""
        toolbar = QToolBar("Main Toolbar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        
        self.start_btn_action = toolbar.addAction("▶ Start")
        self.start_btn_action.triggered.connect(self._toggle_receiving)
        
        toolbar.addSeparator()
        
        self.status_indicator = QLabel(" ● Disconnected")
        self.status_indicator.setStyleSheet("color: gray; font-weight: bold;")
        toolbar.addWidget(self.status_indicator)
        
        toolbar.addSeparator()
        
        self.signal_label = QLabel("RSSI: -- dBm  SNR: -- dB")
        toolbar.addWidget(self.signal_label)
        
        toolbar.addSeparator()
        
        self.recording_label = QLabel("")
        self.recording_label.setStyleSheet("color: #ff4444; font-weight: bold;")
        toolbar.addWidget(self.recording_label)
    
    def _setup_statusbar(self):
        """Setup the status bar."""
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        
        self.packets_label = QLabel("Packets: 0")
        self.statusbar.addPermanentWidget(self.packets_label)
        
        self.gps_status_label = QLabel("GPS: --")
        self.statusbar.addPermanentWidget(self.gps_status_label)
    
    def _connect_signals(self):
        """Connect signals from managers."""
        self.ground_station.telemetry_received.connect(self._on_telemetry)
        self.ground_station.status_changed.connect(self._on_status_changed)
        self.ground_station.error.connect(self._on_error)
        self.ground_station.stats_updated.connect(self._on_stats_updated)
        self.ground_station.image_decoded.connect(self._on_image_decoded)
        self.ground_station.image_progress.connect(self._on_image_progress)
        
        self.gps_manager.position_updated.connect(self._on_gps_position)
        self.gps_manager.bearing_updated.connect(self._on_bearing_updated)
    
    def _auto_connect_gps(self):
        if self.config.gps.port:
            self.gps_manager.connect(self.config.gps.port, self.config.gps.baud_rate)
    
    def _start_receiving(self):
        port = self.tracking_tab.get_selected_port()
        if not port:
            QMessageBox.warning(self, "No Port", "Please select a serial port first.")
            return
        
        self.config.serial_port = port
        save_config()
        
        if self.ground_station.start_receiving(port):
            self.start_action.setEnabled(False)
            self.stop_action.setEnabled(True)
            self.start_btn_action.setText("⏹ Stop")
            self.tracking_tab.set_receiving(True)
    
    def _stop_receiving(self):
        self.ground_station.stop_receiving()
        self.start_action.setEnabled(True)
        self.stop_action.setEnabled(False)
        self.start_btn_action.setText("▶ Start")
        self.tracking_tab.set_receiving(False)
    
    def _toggle_receiving(self):
        if self.ground_station.is_receiving:
            self._stop_receiving()
        else:
            self._start_receiving()
    
    def _on_telemetry(self, telemetry):
        self.tracking_tab.update_telemetry(telemetry)
        
        prediction = self.prediction_manager.update(telemetry)
        self.prediction_tab.update_telemetry(telemetry)
        if prediction:
            self.prediction_tab.update_prediction(prediction)
        
        self.mission_manager.record_telemetry(telemetry)
        self._update_recording_indicator()
        
        if self.sondehub.config.enabled:
            if self.gps_manager.current_position:
                pos = self.gps_manager.current_position
                self.sondehub.set_ground_station_position(
                    pos.latitude, pos.longitude, pos.altitude
                )
            self.sondehub.upload_telemetry(
                telemetry,
                self.ground_station.current_rssi,
                self.ground_station.current_snr
            )
        
        rssi = self.ground_station.current_rssi
        snr = self.ground_station.current_snr
        self.signal_label.setText(f"RSSI: {rssi:.1f} dBm  SNR: {snr:.1f} dB")
    
    def _on_status_changed(self, is_receiving: bool, message: str):
        if is_receiving:
            self.status_indicator.setText(f" ● {message}")
            self.status_indicator.setStyleSheet("color: #00ff00; font-weight: bold;")
        else:
            self.status_indicator.setText(f" ● {message}")
            self.status_indicator.setStyleSheet("color: gray; font-weight: bold;")
        self.statusbar.showMessage(message, 3000)
    
    def _on_error(self, message: str):
        self.statusbar.showMessage(f"Error: {message}", 5000)
    
    def _on_stats_updated(self, stats):
        self.packets_label.setText(f"Packets: {stats.packets_valid}")
    
    def _on_gps_position(self, position):
        if position.is_valid:
            self.gps_status_label.setText(
                f"GPS: {position.latitude:.5f}, {position.longitude:.5f} ({position.satellites} sats)"
            )
            self.tracking_tab.update_ground_station(position)
            self.prediction_tab.update_ground_station(position)
            
            if self.ground_station.latest_telemetry:
                t = self.ground_station.latest_telemetry
                self.gps_manager.update_bearing(t.latitude, t.longitude, t.altitude)
        else:
            self.gps_status_label.setText("GPS: No fix")
    
    def _on_bearing_updated(self, bearing):
        self.tracking_tab.update_bearing(bearing)
    
    def _on_image_decoded(self, path: str, image_id: int):
        self.statusbar.showMessage(f"Image {image_id} decoded: {path}", 5000)
        self.images_tab.add_image(path, image_id)
        self.mission_manager.record_image(path, image_id)
    
    def _on_image_progress(self, image_id: int, progress: float):
        self.images_tab.update_progress(image_id, progress)
    
    def _update_recording_indicator(self):
        if self.mission_manager.is_recording:
            count = len(self.mission_manager.recorded_telemetry)
            self.recording_label.setText(f"🔴 Recording ({count} pts)")
        else:
            self.recording_label.setText("")
        
        self.missions_tab.update_recording_status(
            self.mission_manager.is_recording,
            len(self.mission_manager.recorded_telemetry)
        )
    
    def _clear_track(self):
        self.ground_station.clear_history()
        self.prediction_manager.reset()
        self.tracking_tab.clear_track()
        self.prediction_tab.clear_track()
    
    def _center_on_payload(self):
        if self.ground_station.latest_telemetry:
            telem = self.ground_station.latest_telemetry
            self.tracking_tab.center_on(telem.latitude, telem.longitude)
    
    def _start_new_mission(self):
        name, ok = QInputDialog.getText(
            self, "New Mission", "Mission name:"
        )
        if ok and name:
            self.mission_manager.start_recording(name)
            self._update_recording_indicator()
            self.missions_tab.refresh_missions()
    
    def _stop_mission(self):
        if self.mission_manager.is_recording:
            self.mission_manager.stop_recording(save=True)
            self._update_recording_indicator()
            self.missions_tab.refresh_missions()
    
    def _show_settings(self):
        dialog = SettingsDialog(self.config, self)
        if dialog.exec():
            self.config = get_config()
            self.sondehub.set_config(self.config.sondehub)
            self.mission_manager.auto_record_enabled = self.config.auto_record
    
    def _show_about(self):
        QMessageBox.about(
            self, "About RaptorHabGS",
            "<h3>RaptorHabGS</h3>"
            "<p>High Altitude Balloon Ground Station</p>"
            "<p>Version 1.0.0</p>"
        )
    
    def closeEvent(self, event):
        if self.mission_manager.has_unsaved_recording:
            reply = QMessageBox.question(
                self, "Save Recording?",
                "There is an active mission recording. Save before closing?",
                QMessageBox.StandardButton.Save | 
                QMessageBox.StandardButton.Discard | 
                QMessageBox.StandardButton.Cancel
            )
            
            if reply == QMessageBox.StandardButton.Save:
                self.mission_manager.stop_recording(save=True)
            elif reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            else:
                self.mission_manager.stop_recording(save=False)
        
        if self.ground_station.is_receiving:
            self.ground_station.stop_receiving()
        if self.gps_manager.is_connected:
            self.gps_manager.disconnect()
        
        save_config()
        event.accept()
