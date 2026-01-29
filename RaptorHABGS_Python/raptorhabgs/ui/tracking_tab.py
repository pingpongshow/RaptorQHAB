"""
Live Tracking Tab - Map with telemetry sidebar and bearing display.
"""

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QSplitter, QGroupBox,
    QLabel, QComboBox, QPushButton, QFrame, QScrollArea
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from .map_widget import MapWidget


class StatusRow(QWidget):
    """A single status row with label and value."""
    
    def __init__(self, label: str, monospace: bool = True):
        super().__init__()
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        
        self.label = QLabel(label)
        self.label.setStyleSheet("color: #888;")
        
        self.value = QLabel("--")
        if monospace:
            font = QFont("Courier New", 11)
            self.value.setFont(font)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight)
        
        layout.addWidget(self.label)
        layout.addStretch()
        layout.addWidget(self.value)
    
    def set_value(self, text: str, color: str = None):
        self.value.setText(text)
        if color:
            self.value.setStyleSheet(f"color: {color};")
        else:
            self.value.setStyleSheet("")


class TrackingTab(QWidget):
    """Live tracking tab with map and telemetry sidebar."""
    
    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    
    def __init__(self, ground_station, gps_manager, sondehub):
        super().__init__()
        
        self.ground_station = ground_station
        self.gps_manager = gps_manager
        self.sondehub = sondehub
        self.is_receiving = False
        
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Sidebar
        sidebar = self._create_sidebar()
        splitter.addWidget(sidebar)
        
        # Map
        self.map_widget = MapWidget()
        splitter.addWidget(self.map_widget)
        
        splitter.setSizes([300, 900])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        
        layout.addWidget(splitter)
    
    def _create_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setMaximumWidth(350)
        sidebar.setMinimumWidth(280)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Connection group
        conn_group = QGroupBox("Connection")
        conn_layout = QVBoxLayout(conn_group)
        
        port_layout = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(150)
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setMaximumWidth(40)
        self.refresh_btn.clicked.connect(self.refresh_ports)
        port_layout.addWidget(self.port_combo)
        port_layout.addWidget(self.refresh_btn)
        conn_layout.addLayout(port_layout)
        
        self.start_stop_btn = QPushButton("▶ Start Receiving")
        self.start_stop_btn.setStyleSheet("""
            QPushButton {
                background-color: #2d5a27;
                color: white;
                padding: 10px;
                font-weight: bold;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #3d7a37;
            }
        """)
        self.start_stop_btn.clicked.connect(self._toggle_receiving)
        conn_layout.addWidget(self.start_stop_btn)
        
        layout.addWidget(conn_group)
        
        # Telemetry group
        telem_group = QGroupBox("Payload Telemetry")
        telem_layout = QVBoxLayout(telem_group)
        
        self.lat_row = StatusRow("Latitude")
        self.lon_row = StatusRow("Longitude")
        self.alt_row = StatusRow("Altitude")
        self.speed_row = StatusRow("Speed")
        self.vspeed_row = StatusRow("Vertical")
        self.heading_row = StatusRow("Heading")
        self.sats_row = StatusRow("Satellites")
        self.battery_row = StatusRow("Battery")
        self.temp_row = StatusRow("Temperature")
        
        for row in [self.lat_row, self.lon_row, self.alt_row, 
                    self.speed_row, self.vspeed_row, self.heading_row,
                    self.sats_row, self.battery_row, self.temp_row]:
            telem_layout.addWidget(row)
        
        layout.addWidget(telem_group)
        
        # Signal group
        signal_group = QGroupBox("Signal Quality")
        signal_layout = QVBoxLayout(signal_group)
        
        self.rssi_row = StatusRow("RSSI")
        self.snr_row = StatusRow("SNR")
        self.packets_row = StatusRow("Packets")
        
        for row in [self.rssi_row, self.snr_row, self.packets_row]:
            signal_layout.addWidget(row)
        
        layout.addWidget(signal_group)
        
        # GPS group
        gps_group = QGroupBox("Ground Station GPS")
        gps_layout = QVBoxLayout(gps_group)
        
        gps_port_layout = QHBoxLayout()
        self.gps_port_combo = QComboBox()
        self.gps_connect_btn = QPushButton("Connect")
        self.gps_connect_btn.clicked.connect(self._toggle_gps)
        gps_port_layout.addWidget(self.gps_port_combo)
        gps_port_layout.addWidget(self.gps_connect_btn)
        gps_layout.addLayout(gps_port_layout)
        
        self.gps_status_row = StatusRow("Status")
        self.gps_pos_row = StatusRow("Position")
        gps_layout.addWidget(self.gps_status_row)
        gps_layout.addWidget(self.gps_pos_row)
        
        layout.addWidget(gps_group)
        
        # Bearing group
        bearing_group = QGroupBox("Bearing to Payload")
        bearing_layout = QVBoxLayout(bearing_group)
        
        self.bearing_row = StatusRow("Bearing")
        self.distance_row = StatusRow("Distance")
        self.elevation_row = StatusRow("Elevation")
        
        for row in [self.bearing_row, self.distance_row, self.elevation_row]:
            bearing_layout.addWidget(row)
        
        layout.addWidget(bearing_group)
        
        layout.addStretch()
        
        scroll.setWidget(content)
        
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addWidget(scroll)
        
        # Initial port refresh
        self.refresh_ports()
        
        return sidebar
    
    def refresh_ports(self):
        """Refresh available serial ports."""
        ports = self.ground_station.get_available_ports()
        
        current_port = self.port_combo.currentText()
        current_gps_port = self.gps_port_combo.currentText()
        
        self.port_combo.clear()
        self.gps_port_combo.clear()
        
        for port in ports:
            display_name = port.split('/')[-1]
            self.port_combo.addItem(display_name, port)
            self.gps_port_combo.addItem(display_name, port)
        
        # Restore selection
        if current_port:
            idx = self.port_combo.findText(current_port.split('/')[-1])
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)
        
        if current_gps_port:
            idx = self.gps_port_combo.findText(current_gps_port.split('/')[-1])
            if idx >= 0:
                self.gps_port_combo.setCurrentIndex(idx)
    
    def get_selected_port(self) -> str:
        """Get the selected serial port."""
        return self.port_combo.currentData() or ""
    
    def set_receiving(self, receiving: bool):
        """Update UI for receiving state."""
        self.is_receiving = receiving
        if receiving:
            self.start_stop_btn.setText("⏹ Stop Receiving")
            self.start_stop_btn.setStyleSheet("""
                QPushButton {
                    background-color: #8b0000;
                    color: white;
                    padding: 10px;
                    font-weight: bold;
                    border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #a00000;
                }
            """)
        else:
            self.start_stop_btn.setText("▶ Start Receiving")
            self.start_stop_btn.setStyleSheet("""
                QPushButton {
                    background-color: #2d5a27;
                    color: white;
                    padding: 10px;
                    font-weight: bold;
                    border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #3d7a37;
                }
            """)
    
    def _toggle_receiving(self):
        if self.is_receiving:
            self.stop_requested.emit()
        else:
            self.start_requested.emit()
    
    def _toggle_gps(self):
        if self.gps_manager.is_connected:
            self.gps_manager.disconnect()
            self.gps_connect_btn.setText("Connect")
            self.gps_status_row.set_value("Disconnected")
            self.gps_pos_row.set_value("--")
        else:
            port = self.gps_port_combo.currentData()
            if port and self.gps_manager.connect(port, 9600):
                self.gps_connect_btn.setText("Disconnect")
                self.gps_status_row.set_value("Connected", "#00ff00")
    
    def update_telemetry(self, telem):
        """Update telemetry display."""
        self.lat_row.set_value(f"{telem.latitude:.6f}°")
        self.lon_row.set_value(f"{telem.longitude:.6f}°")
        self.alt_row.set_value(f"{telem.altitude:.1f} m")
        self.speed_row.set_value(f"{telem.speed:.1f} m/s")
        
        vspeed = telem.vertical_speed
        sign = "+" if vspeed >= 0 else ""
        color = "#00ff00" if vspeed > 0 else "#ffff00" if vspeed < 0 else None
        self.vspeed_row.set_value(f"{sign}{vspeed:.1f} m/s", color)
        
        self.heading_row.set_value(f"{telem.heading:.0f}°")
        
        sats_color = "#00ff00" if telem.satellites >= 6 else "#ffff00" if telem.satellites >= 4 else "#ff4444"
        self.sats_row.set_value(str(telem.satellites), sats_color)
        
        batt_v = telem.battery_mv / 1000
        batt_color = "#00ff00" if batt_v >= 3.7 else "#ffff00" if batt_v >= 3.5 else "#ff4444"
        self.battery_row.set_value(f"{batt_v:.2f} V", batt_color)
        
        self.temp_row.set_value(f"{telem.cpu_temp:.1f}°C")
        
        # Signal
        rssi = self.ground_station.current_rssi
        snr = self.ground_station.current_snr
        
        rssi_color = "#00ff00" if rssi > -100 else "#ffff00" if rssi > -110 else "#ff4444"
        self.rssi_row.set_value(f"{rssi:.0f} dBm", rssi_color)
        
        snr_color = "#00ff00" if snr > 5 else "#ffff00" if snr > 0 else "#ff4444"
        self.snr_row.set_value(f"{snr:.1f} dB", snr_color)
        
        self.packets_row.set_value(str(self.ground_station.statistics.packets_valid))
        
        # Update map
        if telem.latitude != 0 or telem.longitude != 0:
            self.map_widget.update_payload(telem.latitude, telem.longitude, telem.altitude)
            self.map_widget.add_track_point(telem.latitude, telem.longitude)
    
    def update_ground_station(self, position):
        """Update ground station position."""
        self.gps_status_row.set_value(f"{position.satellites} satellites", "#00ff00")
        self.gps_pos_row.set_value(f"{position.latitude:.5f}, {position.longitude:.5f}")
        self.map_widget.update_ground_station(position.latitude, position.longitude)
    
    def update_bearing(self, bearing):
        """Update bearing display."""
        self.bearing_row.set_value(f"{bearing.bearing:.1f}° ({bearing.cardinal_direction})")
        
        if bearing.distance >= 1000:
            self.distance_row.set_value(f"{bearing.distance/1000:.2f} km")
        else:
            self.distance_row.set_value(f"{bearing.distance:.0f} m")
        
        self.elevation_row.set_value(f"{bearing.elevation:.1f}°")
        
        # Update bearing line on map
        self.map_widget.update_bearing_line()
    
    def clear_track(self):
        """Clear the track on the map."""
        self.map_widget.clear_track()
    
    def center_on(self, lat: float, lon: float):
        """Center map on coordinates."""
        self.map_widget.center_on(lat, lon)
