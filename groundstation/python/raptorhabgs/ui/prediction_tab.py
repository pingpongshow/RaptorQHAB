"""
Landing Prediction Tab - Shows predicted landing location with confidence.
"""

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGroupBox, QLabel,
    QLineEdit, QPushButton, QFrame, QProgressBar, QScrollArea,
    QSpinBox, QDoubleSpinBox
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from .map_widget import MapWidget


class PredictionTab(QWidget):
    """Landing prediction tab with map and prediction data."""
    
    def __init__(self, ground_station, gps_manager, prediction_manager):
        super().__init__()
        
        self.ground_station = ground_station
        self.gps_manager = gps_manager
        self.prediction_manager = prediction_manager
        
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Sidebar
        sidebar = self._create_sidebar()
        layout.addWidget(sidebar)
        
        # Map
        self.map_widget = MapWidget()
        layout.addWidget(self.map_widget, 1)
    
    def _create_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setMaximumWidth(350)
        sidebar.setMinimumWidth(300)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(15)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # Flight Phase group
        phase_group = QGroupBox("Flight Phase")
        phase_layout = QVBoxLayout(phase_group)
        
        self.phase_label = QLabel("PRELAUNCH")
        self.phase_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.phase_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        self.phase_label.setStyleSheet("""
            QLabel {
                background-color: #2d2d2d;
                color: #888;
                padding: 10px;
                border-radius: 8px;
            }
        """)
        phase_layout.addWidget(self.phase_label)
        
        # Current flight stats
        stats_frame = QFrame()
        stats_layout = QVBoxLayout(stats_frame)
        stats_layout.setSpacing(5)
        
        self.max_alt_label = self._create_stat_row("Max Altitude:", "--")
        self.cur_alt_label = self._create_stat_row("Current Altitude:", "--")
        self.vspeed_label = self._create_stat_row("Vertical Speed:", "--")
        
        stats_layout.addWidget(self.max_alt_label)
        stats_layout.addWidget(self.cur_alt_label)
        stats_layout.addWidget(self.vspeed_label)
        
        phase_layout.addWidget(stats_frame)
        layout.addWidget(phase_group)
        
        # Prediction group
        pred_group = QGroupBox("Landing Prediction")
        pred_layout = QVBoxLayout(pred_group)
        
        self.pred_lat_label = self._create_stat_row("Latitude:", "--")
        self.pred_lon_label = self._create_stat_row("Longitude:", "--")
        self.pred_distance_label = self._create_stat_row("Distance:", "--")
        self.pred_bearing_label = self._create_stat_row("Bearing:", "--")
        self.pred_time_label = self._create_stat_row("Time to Land:", "--")
        
        pred_layout.addWidget(self.pred_lat_label)
        pred_layout.addWidget(self.pred_lon_label)
        pred_layout.addWidget(self.pred_distance_label)
        pred_layout.addWidget(self.pred_bearing_label)
        pred_layout.addWidget(self.pred_time_label)
        
        # Confidence bar
        conf_frame = QFrame()
        conf_layout = QVBoxLayout(conf_frame)
        conf_layout.setContentsMargins(0, 10, 0, 0)
        
        conf_header = QHBoxLayout()
        conf_header.addWidget(QLabel("Confidence:"))
        self.confidence_label = QLabel("Low")
        self.confidence_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        conf_header.addWidget(self.confidence_label)
        conf_layout.addLayout(conf_header)
        
        self.confidence_bar = QProgressBar()
        self.confidence_bar.setRange(0, 100)
        self.confidence_bar.setValue(33)
        self.confidence_bar.setTextVisible(False)
        self.confidence_bar.setStyleSheet("""
            QProgressBar {
                background-color: #2d2d2d;
                border-radius: 4px;
                height: 8px;
            }
            QProgressBar::chunk {
                background-color: #ff4444;
                border-radius: 4px;
            }
        """)
        conf_layout.addWidget(self.confidence_bar)
        
        pred_layout.addWidget(conf_frame)
        layout.addWidget(pred_group)
        
        # Settings group
        settings_group = QGroupBox("Prediction Settings")
        settings_layout = QVBoxLayout(settings_group)
        
        # Burst altitude
        burst_layout = QHBoxLayout()
        burst_layout.addWidget(QLabel("Burst Altitude (m):"))
        self.burst_alt_spin = QSpinBox()
        self.burst_alt_spin.setRange(5000, 50000)
        self.burst_alt_spin.setValue(30000)
        self.burst_alt_spin.setSingleStep(1000)
        burst_layout.addWidget(self.burst_alt_spin)
        settings_layout.addLayout(burst_layout)
        
        # Ascent rate
        ascent_layout = QHBoxLayout()
        ascent_layout.addWidget(QLabel("Ascent Rate (m/s):"))
        self.ascent_rate_spin = QDoubleSpinBox()
        self.ascent_rate_spin.setRange(1.0, 10.0)
        self.ascent_rate_spin.setValue(5.0)
        self.ascent_rate_spin.setSingleStep(0.5)
        ascent_layout.addWidget(self.ascent_rate_spin)
        settings_layout.addLayout(ascent_layout)
        
        # Descent rate
        descent_layout = QHBoxLayout()
        descent_layout.addWidget(QLabel("Descent Rate (m/s):"))
        self.descent_rate_spin = QDoubleSpinBox()
        self.descent_rate_spin.setRange(1.0, 15.0)
        self.descent_rate_spin.setValue(5.0)
        self.descent_rate_spin.setSingleStep(0.5)
        descent_layout.addWidget(self.descent_rate_spin)
        settings_layout.addLayout(descent_layout)
        
        # Apply button
        self.apply_settings_btn = QPushButton("Apply Settings")
        self.apply_settings_btn.clicked.connect(self._apply_settings)
        settings_layout.addWidget(self.apply_settings_btn)
        
        layout.addWidget(settings_group)
        
        # Map controls
        controls_group = QGroupBox("Map Controls")
        controls_layout = QHBoxLayout(controls_group)
        
        center_payload_btn = QPushButton("Center Payload")
        center_payload_btn.clicked.connect(self._center_on_payload)
        controls_layout.addWidget(center_payload_btn)
        
        center_landing_btn = QPushButton("Center Landing")
        center_landing_btn.clicked.connect(self._center_on_landing)
        controls_layout.addWidget(center_landing_btn)
        
        fit_all_btn = QPushButton("Fit All")
        fit_all_btn.clicked.connect(self._fit_all)
        controls_layout.addWidget(fit_all_btn)
        
        layout.addWidget(controls_group)
        
        layout.addStretch()
        
        scroll.setWidget(content)
        
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addWidget(scroll)
        
        return sidebar
    
    def _create_stat_row(self, label: str, value: str) -> QWidget:
        """Create a stat row widget."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 2, 0, 2)
        
        label_widget = QLabel(label)
        label_widget.setStyleSheet("color: #888;")
        
        value_widget = QLabel(value)
        value_widget.setFont(QFont("Courier New", 11))
        value_widget.setAlignment(Qt.AlignmentFlag.AlignRight)
        value_widget.setObjectName("value")
        
        layout.addWidget(label_widget)
        layout.addStretch()
        layout.addWidget(value_widget)
        
        return widget
    
    def _set_stat_value(self, row: QWidget, value: str, color: str = None):
        """Set the value of a stat row."""
        value_label = row.findChild(QLabel, "value")
        if value_label:
            value_label.setText(value)
            if color:
                value_label.setStyleSheet(f"color: {color};")
            else:
                value_label.setStyleSheet("")
    
    def _apply_settings(self):
        """Apply prediction settings."""
        self.prediction_manager.burst_altitude = self.burst_alt_spin.value()
        self.prediction_manager.ascent_rate = self.ascent_rate_spin.value()
        self.prediction_manager.descent_rate_sea_level = self.descent_rate_spin.value()
    
    def _center_on_payload(self):
        """Center map on payload."""
        if self.ground_station.latest_telemetry:
            t = self.ground_station.latest_telemetry
            self.map_widget.center_on(t.latitude, t.longitude)
    
    def _center_on_landing(self):
        """Center map on predicted landing."""
        if self.prediction_manager.current_prediction:
            p = self.prediction_manager.current_prediction
            self.map_widget.center_on(p.latitude, p.longitude)
    
    def _fit_all(self):
        """Fit all markers in view."""
        self.map_widget.fit_bounds()
    
    def update_telemetry(self, telem):
        """Update with new telemetry."""
        self._set_stat_value(self.cur_alt_label, f"{telem.altitude:.0f} m")
        
        vspeed = telem.vertical_speed
        sign = "+" if vspeed >= 0 else ""
        color = "#00ff00" if vspeed > 0 else "#ffff00" if vspeed < 0 else None
        self._set_stat_value(self.vspeed_label, f"{sign}{vspeed:.1f} m/s", color)
        
        # Update max altitude
        self._set_stat_value(
            self.max_alt_label, 
            f"{self.prediction_manager.max_altitude_seen:.0f} m"
        )
        
        # Update map
        if telem.latitude != 0 or telem.longitude != 0:
            self.map_widget.update_payload(telem.latitude, telem.longitude, telem.altitude)
            self.map_widget.add_track_point(telem.latitude, telem.longitude)
    
    def update_ground_station(self, position):
        """Update ground station position."""
        self.map_widget.update_ground_station(position.latitude, position.longitude)
    
    def update_prediction(self, prediction):
        """Update prediction display."""
        # Update phase indicator
        phase = prediction.phase.upper()
        phase_colors = {
            "PRELAUNCH": ("#2d2d2d", "#888"),
            "ASCENDING": ("rgba(0, 255, 0, 0.2)", "#00ff00"),
            "DESCENDING": ("rgba(255, 153, 0, 0.2)", "#ff9900"),
            "FLOATING": ("rgba(68, 136, 255, 0.2)", "#4488ff"),
            "LANDED": ("rgba(68, 136, 255, 0.2)", "#4488ff"),
        }
        bg_color, text_color = phase_colors.get(phase, ("#2d2d2d", "#888"))
        self.phase_label.setText(phase)
        self.phase_label.setStyleSheet(f"""
            QLabel {{
                background-color: {bg_color};
                color: {text_color};
                padding: 10px;
                border-radius: 8px;
            }}
        """)
        
        # Update prediction data
        self._set_stat_value(self.pred_lat_label, f"{prediction.latitude:.6f}°")
        self._set_stat_value(self.pred_lon_label, f"{prediction.longitude:.6f}°")
        
        if prediction.distance_to_landing >= 1000:
            self._set_stat_value(
                self.pred_distance_label, 
                f"{prediction.distance_to_landing/1000:.2f} km"
            )
        else:
            self._set_stat_value(
                self.pred_distance_label, 
                f"{prediction.distance_to_landing:.0f} m"
            )
        
        self._set_stat_value(
            self.pred_bearing_label, 
            f"{prediction.bearing_to_landing:.0f}°"
        )
        
        minutes = int(prediction.time_to_landing // 60)
        seconds = int(prediction.time_to_landing % 60)
        self._set_stat_value(self.pred_time_label, f"{minutes}m {seconds}s")
        
        # Update confidence
        confidence_values = {"low": 33, "medium": 66, "high": 100}
        confidence_colors = {"low": "#ff4444", "medium": "#ffff00", "high": "#00ff00"}
        
        conf_value = confidence_values.get(prediction.confidence, 33)
        conf_color = confidence_colors.get(prediction.confidence, "#ff4444")
        
        self.confidence_bar.setValue(conf_value)
        self.confidence_bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: #2d2d2d;
                border-radius: 4px;
                height: 8px;
            }}
            QProgressBar::chunk {{
                background-color: {conf_color};
                border-radius: 4px;
            }}
        """)
        self.confidence_label.setText(prediction.confidence.capitalize())
        self.confidence_label.setStyleSheet(f"color: {conf_color};")
        
        # Update map with prediction
        radius = {"low": 1000, "medium": 500, "high": 200}.get(prediction.confidence, 1000)
        self.map_widget.update_prediction(
            prediction.latitude, 
            prediction.longitude, 
            radius,
            prediction.confidence
        )
    
    def clear_track(self):
        """Clear the track on the map."""
        self.map_widget.clear_track()
        self.map_widget.clear_prediction()
        
        # Reset labels
        self.phase_label.setText("PRELAUNCH")
        self.phase_label.setStyleSheet("""
            QLabel {
                background-color: #2d2d2d;
                color: #888;
                padding: 10px;
                border-radius: 8px;
            }
        """)
        
        for label in [self.max_alt_label, self.cur_alt_label, self.vspeed_label,
                      self.pred_lat_label, self.pred_lon_label, self.pred_distance_label,
                      self.pred_bearing_label, self.pred_time_label]:
            self._set_stat_value(label, "--")
        
        self.confidence_bar.setValue(33)
        self.confidence_label.setText("Low")
