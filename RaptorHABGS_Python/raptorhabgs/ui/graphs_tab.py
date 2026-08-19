"""
Flight graphs and a raw packet log for the desktop UI.

Both are drawn with QPainter rather than a charting library. The ground station
deliberately carries no plotting dependency: these are line graphs over a few
thousand points, and a dependency that has to be installed on a Raspberry Pi in
a field is a dependency that will not be there when it is needed.
"""

import time
from typing import Callable, List, Optional, Sequence

from PyQt6.QtCore import Qt, QTimer, QPointF
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QComboBox, QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea,
)


class Sparkline(QWidget):
    """One labelled line graph."""

    def __init__(self, title: str, unit: str, colour: str):
        super().__init__()
        self.title = title
        self.unit = unit
        self.colour = QColor(colour)
        self.values: List[float] = []
        self.setMinimumHeight(140)

    def set_values(self, values: Sequence[float]) -> None:
        self.values = [v for v in values if v is not None]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#1a1a1a"))

        padding = 34
        width = self.width() - padding - 8
        height = self.height() - padding - 8

        painter.setPen(QPen(QColor("#888")))
        painter.setFont(QFont("", 10))

        if len(self.values) < 2:
            painter.drawText(padding, 24, f"{self.title} — not enough data yet")
            return

        lowest, highest = min(self.values), max(self.values)
        span = (highest - lowest) or 1.0

        painter.drawText(padding, 18,
                         f"{self.title}   min {lowest:.1f} {self.unit}   "
                         f"max {highest:.1f} {self.unit}   "
                         f"latest {self.values[-1]:.1f} {self.unit}")

        # Axes
        painter.setPen(QPen(QColor("#333")))
        painter.drawLine(padding, self.height() - padding,
                         self.width() - 8, self.height() - padding)
        painter.drawLine(padding, 24, padding, self.height() - padding)

        painter.setPen(QPen(self.colour, 1.6))
        points = []
        for index, value in enumerate(self.values):
            x = padding + (index / (len(self.values) - 1)) * width
            y = (self.height() - padding) - ((value - lowest) / span) * height
            points.append(QPointF(x, y))
        for a, b in zip(points, points[1:]):
            painter.drawLine(a, b)


class GraphsTab(QWidget):
    """Altitude, vertical speed, signal and temperature over the flight."""

    def __init__(self, history_provider: Callable[[], List[object]]):
        super().__init__()
        self.history_provider = history_provider
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(refresh)
        self.auto = QCheckBox("Auto-refresh")
        self.auto.setChecked(True)
        bar.addWidget(self.auto)
        self.info = QLabel("")
        self.info.setStyleSheet("color: #888;")
        bar.addWidget(self.info)
        bar.addStretch()
        layout.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        host_layout = QVBoxLayout(host)

        self.altitude = Sparkline("Altitude", "m", "#4da3ff")
        self.vertical = Sparkline("Vertical speed", "m/s", "#7fd67f")
        self.rssi = Sparkline("RSSI", "dBm", "#ffb454")
        self.temperature = Sparkline("CPU temperature", "°C", "#ff7f7f")
        for graph in (self.altitude, self.vertical, self.rssi, self.temperature):
            host_layout.addWidget(graph)
        scroll.setWidget(host)
        layout.addWidget(scroll)

        self._timer = QTimer(self)
        self._timer.timeout.connect(
            lambda: self.refresh() if self.auto.isChecked() else None)
        self._timer.start(5000)

    def refresh(self):
        points = list(self.history_provider() or [])
        self.info.setText(f"{len(points)} telemetry points")
        if not points:
            for graph in (self.altitude, self.vertical, self.rssi, self.temperature):
                graph.set_values([])
            return

        def field(point, *names):
            for name in names:
                value = getattr(point, name, None)
                if value is None and isinstance(point, dict):
                    value = point.get(name)
                if value is not None:
                    return value
            return None

        altitudes = [field(p, "altitude", "altitude_m") for p in points]
        self.altitude.set_values([a for a in altitudes if a is not None])

        # Vertical speed is derived: the payload does not send it, and
        # computing it here keeps the graph honest about where it came from.
        rates = []
        for index in range(1, len(points)):
            previous, current = points[index - 1], points[index]
            t0 = field(previous, "timestamp")
            t1 = field(current, "timestamp")
            a0, a1 = altitudes[index - 1], altitudes[index]
            if None in (t0, t1, a0, a1):
                continue
            t0 = t0.timestamp() if hasattr(t0, "timestamp") else float(t0)
            t1 = t1.timestamp() if hasattr(t1, "timestamp") else float(t1)
            span = t1 - t0
            if span > 0:
                rates.append((a1 - a0) / span)
        self.vertical.set_values(rates)

        self.rssi.set_values([r for r in (field(p, "rssi") for p in points)
                              if r is not None])
        self.temperature.set_values(
            [t for t in (field(p, "cpu_temp", "cpuTemp", "cpu_temperature")
                         for p in points) if t is not None])


class PacketsTab(QWidget):
    """Every packet the ground station decoded, newest last."""

    COLUMNS = ["Time", "Type", "Seq", "RSSI", "SNR", "Bytes", "Detail"]

    def __init__(self, max_rows: int = 1000):
        super().__init__()
        self.max_rows = max_rows
        self._filter: Optional[str] = None

        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        bar.addWidget(self.follow)

        self.type_filter = QComboBox()
        self.type_filter.addItem("All types", None)
        for name in ("TELEMETRY", "IMAGE_META", "IMAGE_DATA", "TEXT_MESSAGE"):
            self.type_filter.addItem(name, name)
        self.type_filter.currentIndexChanged.connect(self._apply_filter)
        bar.addWidget(self.type_filter)

        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        bar.addWidget(clear)

        self.count_label = QLabel("0 packets")
        self.count_label.setStyleSheet("color: #888;")
        bar.addWidget(self.count_label)
        bar.addStretch()
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(
            len(self.COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.setFont(QFont("Menlo", 10))
        layout.addWidget(self.table)

        self._rows: List[dict] = []

    def add_packet(self, packet: dict) -> None:
        self._rows.append(packet)
        if len(self._rows) > self.max_rows:
            del self._rows[:len(self._rows) - self.max_rows]
        self._render()

    def clear(self) -> None:
        self._rows.clear()
        self._render()

    def _apply_filter(self) -> None:
        self._filter = self.type_filter.currentData()
        self._render()

    def _render(self) -> None:
        rows = [r for r in self._rows
                if not self._filter or r.get("type") == self._filter]
        self.table.setRowCount(len(rows))
        for index, packet in enumerate(rows):
            stamp = packet.get("timestamp") or time.time()
            values = [
                time.strftime("%H:%M:%S", time.localtime(stamp)),
                str(packet.get("type", "?")),
                str(packet.get("sequence", "")),
                "" if packet.get("rssi") is None else str(packet["rssi"]),
                "" if packet.get("snr") is None else f"{packet['snr']:.1f}",
                str(packet.get("length", "")),
                str(packet.get("detail", "")),
            ]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(value))

        self.count_label.setText(
            f"{len(rows)} packets" +
            (f" ({len(self._rows)} total)" if len(rows) != len(self._rows) else ""))
        if self.follow.isChecked():
            self.table.scrollToBottom()
