"""
Settings panels for offline maps and audio alerts.

Kept out of settings_dialogs.py because these two own live objects -- a tile
cache with a background download, and an alert manager that makes noise -- so
they need the manager itself, not just a config dataclass to fill in.
"""

from typing import Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton,
    QDoubleSpinBox, QSpinBox, QCheckBox, QProgressBar, QMessageBox, QGroupBox,
    QSlider, QComboBox,
)
from PyQt6.QtCore import Qt

from ..core.offline_maps import OfflineMapManager
from ..core.audio_alerts import AudioAlertManager, AlertType


class OfflineMapsPanel(QWidget):
    """Download tiles before launch; check what is already cached."""

    def __init__(self, manager: OfflineMapManager,
                 default_latitude: float = 40.0,
                 default_longitude: float = -105.0):
        super().__init__()
        self.manager = manager
        layout = QVBoxLayout(self)

        note = QLabel(
            "Recovery happens where there is no signal. Download the flight "
            "area before launch.\nTiles come from OpenStreetMap's donated "
            "servers — keep requests modest, or point this at your own.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(note)

        region = QGroupBox("Region")
        form = QFormLayout(region)
        self.latitude = QDoubleSpinBox()
        self.latitude.setRange(-85.0, 85.0)
        self.latitude.setDecimals(5)
        self.latitude.setValue(default_latitude)
        self.longitude = QDoubleSpinBox()
        self.longitude.setRange(-180.0, 180.0)
        self.longitude.setDecimals(5)
        self.longitude.setValue(default_longitude)
        self.radius = QDoubleSpinBox()
        self.radius.setRange(1.0, 500.0)
        self.radius.setValue(40.0)
        self.radius.setSuffix(" km")
        self.min_zoom = QSpinBox(); self.min_zoom.setRange(1, 18); self.min_zoom.setValue(8)
        self.max_zoom = QSpinBox(); self.max_zoom.setRange(1, 18); self.max_zoom.setValue(13)
        form.addRow("Centre latitude", self.latitude)
        form.addRow("Centre longitude", self.longitude)
        form.addRow("Radius", self.radius)
        form.addRow("Minimum zoom", self.min_zoom)
        form.addRow("Maximum zoom", self.max_zoom)
        layout.addWidget(region)

        buttons = QHBoxLayout()
        for text, slot in (("Estimate", self.estimate),
                           ("Download", self.download),
                           ("Cancel", self.manager.cancel),
                           ("Clear cache", self.clear_cache)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.progress = QProgressBar()
        layout.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)
        self.refresh()

    def estimate(self):
        result = self.manager.estimate(
            self.latitude.value(), self.longitude.value(), self.radius.value(),
            self.min_zoom.value(), self.max_zoom.value())
        QMessageBox.information(
            self, "Estimate",
            f"{result['tiles']} tiles cover this region, "
            f"{result['missing']} not yet cached.\n"
            f"Roughly {result['estimated_megabytes']} MB and "
            f"{result['estimated_minutes']} minutes at the paced request rate."
            + ("\n\nThis is a large request of a donated tile service and "
               "will need confirming." if result["large"] else ""))

    def download(self):
        try:
            self.manager.download_region(
                self.latitude.value(), self.longitude.value(),
                self.radius.value(), self.min_zoom.value(), self.max_zoom.value())
        except ValueError as exc:
            answer = QMessageBox.question(
                self, "Large download",
                f"{exc}\n\nDownload anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.manager.download_region(
                self.latitude.value(), self.longitude.value(),
                self.radius.value(), self.min_zoom.value(), self.max_zoom.value(),
                acknowledge_large=True)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Busy", str(exc))

    def clear_cache(self):
        if QMessageBox.question(
                self, "Clear cache",
                "Delete every cached tile?") == QMessageBox.StandardButton.Yes:
            self.manager.cache.clear()
            self.refresh()

    def refresh(self):
        status = self.manager.status()
        cache, download = status["cache"], status["download"]
        self.progress.setMaximum(max(1, download["total"]))
        self.progress.setValue(download["completed"])
        by_zoom = ", ".join(f"z{z}: {n}" for z, n in sorted(cache["by_zoom"].items()))
        self.status.setText(
            f"Cached: {cache['tiles']} tiles, {cache['megabytes']} MB"
            + (f"\n{by_zoom}" if by_zoom else "")
            + (f"\nDownloading: {download['completed']}/{download['total']} "
               f"({download['percent']}%), {download['failed']} failed — "
               f"{download['message']}" if download["running"] else
               (f"\nLast run: {download['message']}" if download["message"] else "")))


class AudioAlertsPanel(QWidget):
    """Which events make a noise."""

    def __init__(self, manager: AudioAlertManager):
        super().__init__()
        self.manager = manager
        layout = QVBoxLayout(self)

        note = QLabel(
            "During a flight you are driving or holding an antenna, not "
            "watching the screen.\nAlerts that fire constantly get ignored, so "
            "only the ones that change what you do are on by default.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(note)

        top = QHBoxLayout()
        self.enabled = QCheckBox("Alerts enabled")
        self.enabled.setChecked(manager.config.enabled)
        self.enabled.stateChanged.connect(
            lambda: setattr(manager.config, "enabled", self.enabled.isChecked()))
        top.addWidget(self.enabled)

        self.speak = QCheckBox("Speak the message too")
        self.speak.setChecked(manager.config.speak)
        self.speak.stateChanged.connect(
            lambda: setattr(manager.config, "speak", self.speak.isChecked()))
        top.addWidget(self.speak)
        top.addStretch()
        layout.addLayout(top)

        volume_row = QHBoxLayout()
        volume_row.addWidget(QLabel("Volume"))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(manager.config.volume * 100))
        self.volume.valueChanged.connect(
            lambda v: setattr(manager.config, "volume", v / 100.0))
        volume_row.addWidget(self.volume)
        layout.addLayout(volume_row)

        alerts = QGroupBox("Alerts")
        alerts_layout = QVBoxLayout(alerts)
        self.checks = {}
        for alert in AlertType:
            row = QHBoxLayout()
            check = QCheckBox(alert.value)
            check.setChecked(manager.config.is_enabled(alert))
            check.stateChanged.connect(
                lambda _state, a=alert, c=check:
                manager.config.per_alert.__setitem__(a.name, c.isChecked()))
            self.checks[alert] = check
            row.addWidget(check)

            test = QPushButton("Test")
            test.setMaximumWidth(60)
            test.clicked.connect(
                lambda _checked, a=alert: manager.player.play(a, manager.config.volume))
            row.addWidget(test)
            row.addStretch()
            alerts_layout.addLayout(row)
        layout.addWidget(alerts)

        thresholds = QGroupBox("Thresholds")
        form = QFormLayout(thresholds)
        self.signal_lost = QSpinBox()
        self.signal_lost.setRange(10, 600)
        self.signal_lost.setSuffix(" s")
        self.signal_lost.setValue(int(manager.config.signal_lost_after_sec))
        self.signal_lost.valueChanged.connect(
            lambda v: setattr(manager.config, "signal_lost_after_sec", float(v)))
        form.addRow("Signal lost after", self.signal_lost)

        self.low_battery = QSpinBox()
        self.low_battery.setRange(2500, 4200)
        self.low_battery.setSuffix(" mV")
        self.low_battery.setValue(manager.config.low_battery_mv)
        self.low_battery.valueChanged.connect(
            lambda v: setattr(manager.config, "low_battery_mv", v))
        form.addRow("Low battery below", self.low_battery)
        layout.addWidget(thresholds)

        self.player_label = QLabel(f"Sound output: {manager.player.method}")
        self.player_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self.player_label)
        layout.addStretch()
