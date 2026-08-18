"""
Payload configuration and console tabs for the desktop UI.

The same two capabilities the web UI exposes, for people running the Qt app.
Both talk to the balloon over USB; neither touches the radio link, because the
payload accepts configuration over USB only.

The configuration form is generated from the schema the payload sends. Nothing
here knows what a parameter is called or what range it accepts -- that is the
payload's business, and asking it means the form cannot drift out of step with
the firmware it is configuring.
"""

import re
from typing import Any, Dict, Optional

from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QCheckBox, QSpinBox, QDoubleSpinBox, QScrollArea,
    QGroupBox, QMessageBox, QPlainTextEdit, QSizePolicy,
)

from ..core.payload_link import PayloadLink, discover_payload_ports

# A PTY's control sequences are noise in a plain text widget.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "")


class ConsoleBridge(QObject):
    """
    Carries payload output from the link's reader thread onto the Qt thread.

    Qt widgets may only be touched from the GUI thread; the link delivers
    console bytes from its own reader. A signal is the safe crossing.
    """
    output = pyqtSignal(str)


class PayloadConfigTab(QWidget):
    """Schema-driven configuration form for the airborne payload."""

    def __init__(self, link: PayloadLink):
        super().__init__()
        self.link = link
        self.schema: Optional[dict] = None
        self.values: Dict[str, Any] = {}
        self.editors: Dict[str, QWidget] = {}
        self._build()

    # -- layout ------------------------------------------------------------

    def _build(self):
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(320)
        bar.addWidget(self.port_combo)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        bar.addWidget(self.refresh_btn)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connect)
        bar.addWidget(self.connect_btn)

        self.identity_label = QLabel("Not connected")
        bar.addWidget(self.identity_label)
        bar.addStretch()
        layout.addLayout(bar)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #888;")
        layout.addWidget(self.status_label)

        actions = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter parameters...")
        self.filter_edit.textChanged.connect(self.render)
        actions.addWidget(self.filter_edit)

        self.advanced_check = QCheckBox("Show advanced")
        self.advanced_check.stateChanged.connect(self.render)
        actions.addWidget(self.advanced_check)

        for text, slot in (("Apply changes", self.apply_changes),
                           ("Reload", self.load_config),
                           ("Restart payload service", self.restart_service)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            actions.addWidget(b)
        actions.addStretch()
        layout.addLayout(actions)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.form_host = QWidget()
        self.form_layout = QVBoxLayout(self.form_host)
        self.form_layout.addWidget(QLabel("Connect to the payload over USB to load its configuration."))
        self.scroll.setWidget(self.form_host)
        layout.addWidget(self.scroll)

        self.refresh_ports()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self.load_status)
        self._status_timer.start(10000)

    # -- connection --------------------------------------------------------

    def refresh_ports(self):
        self.port_combo.clear()
        for p in discover_payload_ports():
            label = p.label if p.confident else f"{p.label} (unidentified)"
            self.port_combo.addItem(label, p.device)
        if not self.port_combo.count():
            self.port_combo.addItem("No payload found", None)

    def toggle_connect(self):
        if self.link.connected:
            self.link.disconnect()
            self.connect_btn.setText("Connect")
            self.identity_label.setText("Not connected")
            self.status_label.setText("")
            return

        device = self.port_combo.currentData()
        if not device:
            QMessageBox.warning(self, "No payload", "No payload port was found.")
            return
        try:
            identity = self.link.connect(device)
        except Exception as exc:
            QMessageBox.critical(self, "Connect failed", str(exc))
            return

        self.connect_btn.setText("Disconnect")
        self.identity_label.setText(
            f"{identity.get('callsign')} (payload {identity.get('payload_id')}) "
            f"on {identity.get('hostname')}")
        self.load_schema()
        self.load_config()
        self.load_status()

    # -- data --------------------------------------------------------------

    def load_status(self):
        if not self.link.connected:
            return
        try:
            st = self.link.get_status()
        except Exception:
            return
        self.status_label.setText(
            f"service {st['service']['active']}/{st['service']['sub']} · "
            f"uptime {round(st['system']['uptime_sec'] / 60)} min · "
            f"CPU {st['system']['cpu_temp_c']:.1f}°C · "
            f"{st['storage']['image_count']} images · "
            f"{st['storage']['free_bytes'] / 1e9:.1f} GB free")

    def load_schema(self):
        try:
            self.schema = self.link.get_schema()
        except Exception as exc:
            QMessageBox.critical(self, "Schema failed", str(exc))

    def load_config(self):
        if not self.link.connected:
            return
        try:
            cfg = self.link.get_config()
        except Exception as exc:
            QMessageBox.critical(self, "Config failed", str(exc))
            return
        self.values = cfg.get("values", {})
        self.render()

    # -- form --------------------------------------------------------------

    def render(self):
        while self.form_layout.count():
            item = self.form_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.editors.clear()

        if not self.schema:
            self.form_layout.addWidget(QLabel("Not connected."))
            return

        needle = self.filter_edit.text().lower().strip()
        advanced = self.advanced_check.isChecked()

        by_category: Dict[str, list] = {}
        for p in self.schema["parameters"]:
            if p.get("advanced") and not advanced:
                continue
            if needle and needle not in p["name"].lower() and \
                    needle not in (p.get("description") or "").lower():
                continue
            by_category.setdefault(p["category"], []).append(p)

        for category in self.schema["categories"]:
            params = by_category.get(category)
            if not params:
                continue
            box = QGroupBox(category)
            form = QFormLayout(box)
            for p in params:
                editor = self._editor_for(p)
                self.editors[p["name"]] = editor
                label = QLabel(p["name"] + (f"  ({p['unit']})" if p.get("unit") else ""))
                label.setToolTip(p.get("description") or "")
                editor.setToolTip(p.get("description") or "")
                form.addRow(label, editor)
            self.form_layout.addWidget(box)

        self.form_layout.addStretch()

    def _editor_for(self, p: dict) -> QWidget:
        name = p["name"]
        value = self.values.get(name)
        kind = p.get("kind")

        if p.get("secret"):
            w = QLineEdit()
            w.setEchoMode(QLineEdit.EchoMode.Password)
            w.setPlaceholderText("unchanged")
            return w

        if kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(value))
            return w

        if kind == "enum":
            w = QComboBox()
            for choice in p.get("choices", []):
                w.addItem(str(choice), choice)
            idx = w.findText(str(value))
            if idx >= 0:
                w.setCurrentIndex(idx)
            return w

        if kind == "int":
            w = QSpinBox()
            # Qt spin boxes are 32-bit; the payload's bounds can exceed that.
            lo = p.get("minimum")
            hi = p.get("maximum")
            w.setRange(int(lo) if lo is not None else -2_147_483_648,
                       int(hi) if hi is not None else 2_147_483_647)
            if isinstance(value, int):
                w.setValue(value)
            return w

        if kind == "float":
            w = QDoubleSpinBox()
            w.setDecimals(4)
            lo = p.get("minimum")
            hi = p.get("maximum")
            w.setRange(float(lo) if lo is not None else -1e9,
                       float(hi) if hi is not None else 1e9)
            if isinstance(value, (int, float)):
                w.setValue(float(value))
            return w

        w = QLineEdit()
        if kind == "resolution" and isinstance(value, (list, tuple)):
            w.setText(f"{value[0]}x{value[1]}")
        elif value is not None:
            w.setText(str(value))
        return w

    def _current_value(self, p: dict) -> Any:
        editor = self.editors[p["name"]]
        kind = p.get("kind")
        if p.get("secret"):
            return editor.text() or None
        if kind == "bool":
            return editor.isChecked()
        if kind == "enum":
            return editor.currentData()
        if kind == "int":
            return editor.value()
        if kind == "float":
            return editor.value()
        if kind == "resolution":
            m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", editor.text())
            return [int(m.group(1)), int(m.group(2))] if m else editor.text()
        return editor.text()

    def apply_changes(self):
        if not self.link.connected or not self.schema:
            return
        changed: Dict[str, Any] = {}
        for p in self.schema["parameters"]:
            if p["name"] not in self.editors:
                continue
            new = self._current_value(p)
            # A blank secret means "leave it alone". Sending an empty string
            # would clear a key that the operator never intended to touch.
            if p.get("secret"):
                if new:
                    changed[p["name"]] = new
                continue
            if new != self.values.get(p["name"]):
                changed[p["name"]] = new

        if not changed:
            QMessageBox.information(self, "No changes", "Nothing to apply.")
            return

        try:
            result = self.link.set_config(changed)
        except Exception as exc:
            QMessageBox.critical(self, "Apply failed", str(exc))
            return

        lines = [f"Applied {len(result.get('applied', []))} parameter(s)."]
        if result.get("rejected"):
            lines.append("Rejected:")
            lines += [f"  {k}: {v}" for k, v in result["rejected"].items()]
        if result.get("restart_required"):
            lines.append("Restart required for: " + ", ".join(result["restart_required"]))
        QMessageBox.information(self, "Configuration", "\n".join(lines))
        self.load_config()

    def restart_service(self):
        if not self.link.connected:
            return
        if QMessageBox.question(
                self, "Restart payload",
                "Restart the payload service? The downlink stops briefly."
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.link.restart_service()
        except Exception as exc:
            QMessageBox.critical(self, "Restart failed", str(exc))
            return
        QTimer.singleShot(3000, self.load_status)


class PayloadConsoleTab(QWidget):
    """A terminal on the payload."""

    def __init__(self, link: PayloadLink):
        super().__init__()
        self.link = link
        self.bridge = ConsoleBridge()
        self.bridge.output.connect(self._append)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        for text, slot in (("Start shell", self.start),
                           ("Stop", self.stop),
                           ("Clear", self.clear)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #888;")
        bar.addWidget(self.status_label)
        bar.addStretch()
        layout.addLayout(bar)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Menlo", 11))
        # A long flight produces a lot of output; cap the scrollback rather
        # than letting the widget grow without bound.
        self.output.setMaximumBlockCount(5000)
        layout.addWidget(self.output)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a command and press Enter")
        self.input.setFont(QFont("Menlo", 11))
        self.input.returnPressed.connect(self.send)
        layout.addWidget(self.input)

    def start(self):
        if not self.link.connected:
            QMessageBox.warning(self, "Not connected",
                                "Connect to the payload on the Config tab first.")
            return
        self.link.on_console = lambda data: self.bridge.output.emit(
            data.decode("utf-8", "replace"))
        try:
            self.link.shell_start(30, 120)
        except Exception as exc:
            QMessageBox.critical(self, "Shell failed", str(exc))
            return
        self.status_label.setText("shell running")

    def stop(self):
        try:
            self.link.shell_stop()
        except Exception:
            pass
        self.status_label.setText("shell stopped")

    def clear(self):
        self.output.clear()

    def send(self):
        if not self.link.connected:
            return
        try:
            self.link.console_write((self.input.text() + "\n").encode())
        except Exception as exc:
            self.status_label.setText(f"error: {exc}")
            return
        self.input.clear()

    def _append(self, text: str):
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)
        self.output.insertPlainText(strip_ansi(text))
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)
