"""
Settings dialogs for configuration.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QFormLayout, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox,
    QComboBox, QPushButton, QGroupBox, QLabel, QDialogButtonBox
)
from PyQt6.QtCore import Qt

from ..core.config import (
    AppConfig, ModemConfig, SondeHubConfig, GPSConfig, 
    PredictionConfig, get_config, save_config
)


class SettingsDialog(QDialog):
    """Main settings dialog with tabs."""
    
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        
        self.config = config
        
        self.setWindowTitle("Settings")
        self.setMinimumSize(500, 400)
        
        self._setup_ui()
        self._load_settings()
    
    def _setup_ui(self):
        """Setup the dialog UI."""
        layout = QVBoxLayout(self)
        
        # Tab widget
        tabs = QTabWidget()
        
        # Modem tab
        tabs.addTab(self._create_modem_tab(), "Radio Modem")
        
        # SondeHub tab
        tabs.addTab(self._create_sondehub_tab(), "SondeHub")
        
        # GPS tab
        tabs.addTab(self._create_gps_tab(), "GPS")
        
        # Prediction tab
        tabs.addTab(self._create_prediction_tab(), "Prediction")
        
        # Missions tab
        tabs.addTab(self._create_missions_tab(), "Missions")
        
        layout.addWidget(tabs)
        
        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | 
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
    
    def _create_modem_tab(self) -> QWidget:
        """Create modem settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # FSK Settings
        fsk_group = QGroupBox("FSK Mode (High-Speed)")
        fsk_layout = QFormLayout(fsk_group)
        
        self.freq_spin = QDoubleSpinBox()
        self.freq_spin.setRange(400, 1000)
        self.freq_spin.setDecimals(3)
        self.freq_spin.setSuffix(" MHz")
        fsk_layout.addRow("Frequency:", self.freq_spin)
        
        self.bitrate_spin = QDoubleSpinBox()
        self.bitrate_spin.setRange(1, 300)
        self.bitrate_spin.setDecimals(1)
        self.bitrate_spin.setSuffix(" kbps")
        fsk_layout.addRow("Bit Rate:", self.bitrate_spin)
        
        self.deviation_spin = QDoubleSpinBox()
        self.deviation_spin.setRange(5, 100)
        self.deviation_spin.setDecimals(1)
        self.deviation_spin.setSuffix(" kHz")
        fsk_layout.addRow("Deviation:", self.deviation_spin)
        
        self.bandwidth_spin = QDoubleSpinBox()
        self.bandwidth_spin.setRange(50, 500)
        self.bandwidth_spin.setDecimals(1)
        self.bandwidth_spin.setSuffix(" kHz")
        fsk_layout.addRow("Bandwidth:", self.bandwidth_spin)
        
        layout.addWidget(fsk_group)
        
        # Connection settings
        conn_group = QGroupBox("Connection")
        conn_layout = QFormLayout(conn_group)
        
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["9600", "19200", "38400", "57600", "115200", "230400"])
        conn_layout.addRow("Baud Rate:", self.baud_combo)
        
        layout.addWidget(conn_group)
        layout.addStretch()
        
        return widget
    
    def _create_sondehub_tab(self) -> QWidget:
        """Create SondeHub settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # Enable checkbox
        self.sondehub_enabled = QCheckBox("Enable SondeHub uploads")
        layout.addWidget(self.sondehub_enabled)
        
        # Credentials
        creds_group = QGroupBox("Credentials")
        creds_layout = QFormLayout(creds_group)
        
        self.uploader_callsign = QLineEdit()
        self.uploader_callsign.setPlaceholderText("Your callsign (e.g., N0CALL)")
        creds_layout.addRow("Uploader Callsign:", self.uploader_callsign)
        
        self.payload_callsign = QLineEdit()
        self.payload_callsign.setPlaceholderText("Payload callsign")
        creds_layout.addRow("Payload Callsign:", self.payload_callsign)
        
        self.uploader_antenna = QLineEdit()
        self.uploader_antenna.setPlaceholderText("e.g., Yagi, Ground Plane")
        creds_layout.addRow("Antenna:", self.uploader_antenna)
        
        layout.addWidget(creds_group)
        
        # Options
        options_group = QGroupBox("Upload Options")
        options_layout = QVBoxLayout(options_group)
        
        self.upload_telemetry = QCheckBox("Upload telemetry")
        options_layout.addWidget(self.upload_telemetry)
        
        self.upload_position = QCheckBox("Upload station position")
        options_layout.addWidget(self.upload_position)
        
        layout.addWidget(options_group)
        layout.addStretch()
        
        return widget
    
    def _create_gps_tab(self) -> QWidget:
        """Create GPS settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        self.gps_enabled = QCheckBox("Enable GPS for ground station position")
        layout.addWidget(self.gps_enabled)
        
        gps_group = QGroupBox("GPS Settings")
        gps_layout = QFormLayout(gps_group)
        
        self.gps_baud = QComboBox()
        self.gps_baud.addItems(["4800", "9600", "19200", "38400", "57600", "115200"])
        gps_layout.addRow("Baud Rate:", self.gps_baud)
        
        layout.addWidget(gps_group)
        layout.addStretch()
        
        return widget
    
    def _create_prediction_tab(self) -> QWidget:
        """Create prediction settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        self.prediction_enabled = QCheckBox("Enable landing prediction")
        layout.addWidget(self.prediction_enabled)
        
        pred_group = QGroupBox("Prediction Parameters")
        pred_layout = QFormLayout(pred_group)
        
        self.burst_alt = QSpinBox()
        self.burst_alt.setRange(10000, 50000)
        self.burst_alt.setSuffix(" m")
        pred_layout.addRow("Burst Altitude:", self.burst_alt)
        
        self.ascent_rate = QDoubleSpinBox()
        self.ascent_rate.setRange(1, 10)
        self.ascent_rate.setDecimals(1)
        self.ascent_rate.setSuffix(" m/s")
        pred_layout.addRow("Ascent Rate:", self.ascent_rate)
        
        self.descent_rate = QDoubleSpinBox()
        self.descent_rate.setRange(1, 15)
        self.descent_rate.setDecimals(1)
        self.descent_rate.setSuffix(" m/s")
        pred_layout.addRow("Descent Rate:", self.descent_rate)
        
        layout.addWidget(pred_group)
        
        # Wind settings
        wind_group = QGroupBox("Wind")
        wind_layout = QFormLayout(wind_group)
        
        self.auto_wind = QCheckBox("Auto-calculate from drift")
        wind_layout.addRow(self.auto_wind)
        
        self.wind_speed = QDoubleSpinBox()
        self.wind_speed.setRange(0, 50)
        self.wind_speed.setDecimals(1)
        self.wind_speed.setSuffix(" m/s")
        wind_layout.addRow("Wind Speed:", self.wind_speed)
        
        self.wind_dir = QSpinBox()
        self.wind_dir.setRange(0, 359)
        self.wind_dir.setSuffix("°")
        wind_layout.addRow("Wind Direction:", self.wind_dir)
        
        layout.addWidget(wind_group)
        layout.addStretch()
        
        return widget
    
    def _create_missions_tab(self) -> QWidget:
        """Create missions settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # Auto-record settings
        record_group = QGroupBox("Auto-Recording")
        record_layout = QVBoxLayout(record_group)
        
        self.auto_record = QCheckBox("Auto-record on first telemetry")
        record_layout.addWidget(self.auto_record)
        
        info_label = QLabel(
            "When enabled, mission recording will automatically start\n"
            "when the first valid telemetry is received above 50m altitude."
        )
        info_label.setStyleSheet("color: #888; font-size: 11px;")
        record_layout.addWidget(info_label)
        
        layout.addWidget(record_group)
        layout.addStretch()
        
        return widget
    
    def _load_settings(self):
        """Load settings into UI."""
        # Modem
        self.freq_spin.setValue(self.config.modem.frequency_mhz)
        self.bitrate_spin.setValue(self.config.modem.bitrate_kbps)
        self.deviation_spin.setValue(self.config.modem.deviation_khz)
        self.bandwidth_spin.setValue(self.config.modem.bandwidth_khz)
        
        idx = self.baud_combo.findText(str(self.config.serial_baud))
        if idx >= 0:
            self.baud_combo.setCurrentIndex(idx)
        
        # SondeHub
        self.sondehub_enabled.setChecked(self.config.sondehub.enabled)
        self.uploader_callsign.setText(self.config.sondehub.uploader_callsign)
        self.payload_callsign.setText(self.config.sondehub.payload_callsign)
        self.uploader_antenna.setText(self.config.sondehub.uploader_antenna)
        self.upload_telemetry.setChecked(self.config.sondehub.upload_telemetry)
        self.upload_position.setChecked(self.config.sondehub.upload_position)
        
        # GPS
        self.gps_enabled.setChecked(self.config.gps.enabled)
        idx = self.gps_baud.findText(str(self.config.gps.baud_rate))
        if idx >= 0:
            self.gps_baud.setCurrentIndex(idx)
        
        # Prediction
        self.prediction_enabled.setChecked(self.config.prediction.enabled)
        self.burst_alt.setValue(int(self.config.prediction.burst_altitude))
        self.ascent_rate.setValue(self.config.prediction.ascent_rate)
        self.descent_rate.setValue(self.config.prediction.descent_rate)
        self.auto_wind.setChecked(self.config.prediction.use_auto_wind)
        self.wind_speed.setValue(self.config.prediction.wind_speed)
        self.wind_dir.setValue(int(self.config.prediction.wind_direction))
        
        # Missions
        self.auto_record.setChecked(self.config.auto_record)
    
    def _save_and_accept(self):
        """Save settings and close dialog."""
        # Modem
        self.config.modem.frequency_mhz = self.freq_spin.value()
        self.config.modem.bitrate_kbps = self.bitrate_spin.value()
        self.config.modem.deviation_khz = self.deviation_spin.value()
        self.config.modem.bandwidth_khz = self.bandwidth_spin.value()
        self.config.serial_baud = int(self.baud_combo.currentText())
        
        # SondeHub
        self.config.sondehub.enabled = self.sondehub_enabled.isChecked()
        self.config.sondehub.uploader_callsign = self.uploader_callsign.text()
        self.config.sondehub.payload_callsign = self.payload_callsign.text()
        self.config.sondehub.uploader_antenna = self.uploader_antenna.text()
        self.config.sondehub.upload_telemetry = self.upload_telemetry.isChecked()
        self.config.sondehub.upload_position = self.upload_position.isChecked()
        
        # GPS
        self.config.gps.enabled = self.gps_enabled.isChecked()
        self.config.gps.baud_rate = int(self.gps_baud.currentText())
        
        # Prediction
        self.config.prediction.enabled = self.prediction_enabled.isChecked()
        self.config.prediction.burst_altitude = float(self.burst_alt.value())
        self.config.prediction.ascent_rate = self.ascent_rate.value()
        self.config.prediction.descent_rate = self.descent_rate.value()
        self.config.prediction.use_auto_wind = self.auto_wind.isChecked()
        self.config.prediction.wind_speed = self.wind_speed.value()
        self.config.prediction.wind_direction = float(self.wind_dir.value())
        
        # Missions
        self.config.auto_record = self.auto_record.isChecked()
        
        # Save to file
        self.config.save()
        
        self.accept()
