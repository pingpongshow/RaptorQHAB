"""
Meshtastic and position-source tabs for the desktop UI.

The same capability as the web UI: a stock Meshtastic node as a second
receiver for the balloon, the public MQTT network as a third, and a view of
which source the map is actually drawing.
"""

import base64
import time
from typing import Optional

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QCheckBox, QMessageBox, QTableWidget, QTableWidgetItem,
    QPlainTextEdit, QGroupBox, QHeaderView,
)

from ..core.meshtastic_manager import (
    MeshtasticManager, ChannelConfig, discover_meshtastic_ports)
from ..core.meshtastic_mqtt import MeshtasticMQTTClient
from ..core.meshtastic import channel_hash
from ..core.position_fusion import PositionFusion


class MeshBridge(QObject):
    """Carries callbacks from reader threads onto the Qt thread."""
    message = pyqtSignal(object)
    position = pyqtSignal()


class MeshtasticTab(QWidget):
    def __init__(self, mesh: MeshtasticManager, mqtt: MeshtasticMQTTClient,
                 fusion: PositionFusion):
        super().__init__()
        self.mesh = mesh
        self.mqtt = mqtt
        self.fusion = fusion
        self.bridge = MeshBridge()
        self.bridge.message.connect(lambda _: self.refresh())
        self._build()

        self.mesh.on_message = lambda m: self.bridge.message.emit(m)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(5000)

    def _build(self):
        layout = QVBoxLayout(self)

        conn = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(300)
        conn.addWidget(self.port_combo)
        for text, slot in (("Refresh", self.refresh_ports),
                           ("Connect node", self.toggle_node),
                           ("Connect MQTT", self.toggle_mqtt)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            conn.addWidget(b)
            if text == "Connect node":
                self.node_btn = b
            if text == "Connect MQTT":
                self.mqtt_btn = b
        conn.addStretch()
        layout.addLayout(conn)

        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("color: #888;")
        layout.addWidget(self.status_label)

        hint = QLabel("Any radio running stock Meshtastic firmware works — the "
                      "balloon transmits standard LongFast packets.")
        hint.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(hint)

        setup = QGroupBox("Balloon and channels")
        setup_layout = QVBoxLayout(setup)

        row = QHBoxLayout()
        self.balloon_edit = QLineEdit()
        self.balloon_edit.setPlaceholderText("Balloon node id, e.g. !efcab5ac")
        row.addWidget(self.balloon_edit)
        b = QPushButton("Set balloon node")
        b.clicked.connect(self.set_balloon)
        row.addWidget(b)
        setup_layout.addLayout(row)

        row2 = QHBoxLayout()
        self.chan_name = QLineEdit("LongFast")
        self.chan_name.setMaximumWidth(160)
        self.chan_key = QLineEdit()
        self.chan_key.setPlaceholderText("Channel key (base64)")
        row2.addWidget(self.chan_name)
        row2.addWidget(self.chan_key)
        b = QPushButton("Add channel")
        b.clicked.connect(self.add_channel)
        row2.addWidget(b)
        setup_layout.addLayout(row2)
        layout.addWidget(setup)

        self.nodes_table = QTableWidget(0, 5)
        self.nodes_table.setHorizontalHeaderLabels(
            ["Node", "SNR", "RSSI", "Battery", "Last heard"])
        self.nodes_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.nodes_table)

        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.messages.setFont(QFont("Menlo", 11))
        self.messages.setMaximumBlockCount(500)
        layout.addWidget(self.messages)

        send = QHBoxLayout()
        self.draft = QLineEdit()
        self.draft.setPlaceholderText("Message, or !command")
        self.draft.returnPressed.connect(self.send)
        send.addWidget(self.draft)
        self.channel_pick = QComboBox()
        send.addWidget(self.channel_pick)
        self.to_balloon = QCheckBox("to balloon")
        self.as_command = QCheckBox("as command")
        self.as_command.setToolTip(
            "Uplink command: private channel, addressed to the balloon, must start with !")
        send.addWidget(self.to_balloon)
        send.addWidget(self.as_command)
        b = QPushButton("Send")
        b.clicked.connect(self.send)
        send.addWidget(b)
        layout.addLayout(send)

        self.refresh_ports()

    # -- actions -----------------------------------------------------------

    def refresh_ports(self):
        self.port_combo.clear()
        for p in discover_meshtastic_ports():
            self.port_combo.addItem(f"{p['device']} — {p['description']}", p["device"])
        if not self.port_combo.count():
            self.port_combo.addItem("No Meshtastic node found", None)

    def toggle_node(self):
        if self.mesh.connected:
            self.mesh.disconnect()
            self.node_btn.setText("Connect node")
            self.refresh()
            return
        device = self.port_combo.currentData()
        if not device:
            QMessageBox.warning(self, "No node", "No Meshtastic node was found.")
            return
        try:
            self.mesh.connect(device)
        except Exception as exc:
            QMessageBox.critical(self, "Connect failed", str(exc))
            return
        self.node_btn.setText("Disconnect node")
        self.refresh()

    def toggle_mqtt(self):
        try:
            if self.mqtt.connected:
                self.mqtt.disconnect()
                self.mqtt_btn.setText("Connect MQTT")
            else:
                self.mqtt.connect()
                self.mqtt_btn.setText("Disconnect MQTT")
        except Exception as exc:
            QMessageBox.critical(self, "MQTT failed", str(exc))

    def set_balloon(self):
        text = self.balloon_edit.text().strip()
        if not text:
            return
        try:
            node_id = int(text.lstrip("!"), 16)
        except ValueError:
            QMessageBox.warning(self, "Bad node id",
                                "Expected a hex node id such as !efcab5ac")
            return
        self.mesh.balloon_node_id = node_id
        self.mqtt.balloon_node_id = node_id
        self.refresh()

    def add_channel(self):
        name = self.chan_name.text().strip()
        key_text = self.chan_key.text().strip()
        if not name or not key_text:
            QMessageBox.warning(self, "Channel",
                                "Both a name and a base64 key are required.")
            return
        try:
            key = base64.b64decode(key_text)
        except Exception:
            QMessageBox.warning(self, "Channel", "The key is not valid base64.")
            return
        self.mesh.channels.append(
            ChannelConfig(name=name, key=key, hash=channel_hash(name, key)))
        self.chan_key.clear()
        self._sync_channel_picker()

    def _sync_channel_picker(self):
        self.channel_pick.clear()
        for channel in self.mesh.channels:
            self.channel_pick.addItem(channel.name, channel.name)

    def send(self):
        text = self.draft.text().strip()
        if not text:
            return
        if not self.mesh.channels:
            QMessageBox.warning(self, "No channel", "Add a channel first.")
            return
        name = self.channel_pick.currentData()
        channel = next((c for c in self.mesh.channels if c.name == name),
                       self.mesh.channels[0])
        try:
            if self.as_command.isChecked():
                self.mesh.send_command_to_balloon(text, channel)
            elif self.to_balloon.isChecked():
                if self.mesh.balloon_node_id is None:
                    raise ValueError("the balloon's node id is not known yet")
                self.mesh.send_text(text, channel,
                                    destination=self.mesh.balloon_node_id)
            else:
                self.mesh.send_text(text, channel)
        except Exception as exc:
            QMessageBox.warning(self, "Send failed", str(exc))
            return
        self.draft.clear()
        self.refresh()

    # -- display -----------------------------------------------------------

    def refresh(self):
        mesh = self.mesh.status()
        mqtt = self.mqtt.status()
        self.status_label.setText(
            f"node {'connected on ' + str(mesh['port']) if mesh['connected'] else 'disconnected'} · "
            f"{mesh['packets_received']} packets · {mesh['decrypt_failures']} undecodable · "
            f"MQTT {mqtt['state']} ({mqtt['positions_forwarded']} positions)")

        nodes = sorted(self.mesh.nodes.values(),
                       key=lambda n: n.last_heard, reverse=True)
        self.nodes_table.setRowCount(len(nodes))
        for row, node in enumerate(nodes):
            name = node.display_name
            if node.node_id == self.mesh.balloon_node_id:
                name = "🎈 " + name
            values = [
                name,
                "" if node.snr is None else f"{node.snr:.1f}",
                "" if node.rssi is None else str(node.rssi),
                "" if node.battery_percent is None else f"{node.battery_percent}%",
                time.strftime("%H:%M:%S", time.localtime(node.last_heard))
                if node.last_heard else "",
            ]
            for column, value in enumerate(values):
                self.nodes_table.setItem(row, column, QTableWidgetItem(value))

        self.messages.setPlainText("\n".join(
            f"{time.strftime('%H:%M:%S', time.localtime(m.timestamp))} "
            f"{'→' if m.outgoing else '←'} {m.sender_name}: {m.text}"
            for m in self.mesh.messages[-200:]))
        self.messages.verticalScrollBar().setValue(
            self.messages.verticalScrollBar().maximum())


class PositionSourcesTab(QWidget):
    """Which source the map is drawing, and how old it is."""

    def __init__(self, fusion: PositionFusion):
        super().__init__()
        self.fusion = fusion
        layout = QVBoxLayout(self)

        self.extrapolate = QCheckBox(
            "Extrapolate when every source has gone quiet "
            "(always labelled as dead reckoning)")
        self.extrapolate.setChecked(True)
        self.extrapolate.stateChanged.connect(
            lambda: setattr(self.fusion, "extrapolation_enabled",
                            self.extrapolate.isChecked()))
        layout.addWidget(self.extrapolate)

        best_box = QGroupBox("Currently drawing")
        best_layout = QVBoxLayout(best_box)
        self.best_label = QLabel("No position yet.")
        self.best_label.setWordWrap(True)
        best_layout.addWidget(self.best_label)
        layout.addWidget(best_box)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Source", "Latitude", "Longitude", "Altitude", "Age"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(2000)

    def refresh(self):
        status = self.fusion.status()
        best = status["best"]
        if best:
            self.best_label.setText(
                f"{best['source_label']} — {best['latitude']:.5f}, "
                f"{best['longitude']:.5f} at {round(best['altitude'])} m · "
                f"{best['age']}{' · STALE' if best['stale'] else ''}"
                + (f"\n{best['detail']}" if best.get("detail") else ""))
        else:
            self.best_label.setText("No position yet.")

        rows = list(status["sources"].values())
        self.table.setRowCount(len(rows))
        for row, fix in enumerate(rows):
            values = [
                fix["source_label"],
                f"{fix['latitude']:.5f}",
                f"{fix['longitude']:.5f}",
                f"{round(fix['altitude'])} m",
                fix["age"] + (" (stale)" if fix["stale"] else ""),
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
