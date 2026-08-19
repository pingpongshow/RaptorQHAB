"""
Recovered SD card tab for the desktop UI.

The card holds every image at full quality and the complete telemetry log, not
just what fitted in the airtime budget. This reads it in place, unsealing as it
goes, and copies out what you ask for.

The readability check comes first and is stated plainly. Copying gigabytes only
to discover none of it can be opened is a bad evening, and unlike most errors
this one has no remedy: recordings sealed to a key nobody holds are gone.
"""

import os
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QCheckBox, QListWidget, QListWidgetItem, QMessageBox, QFileDialog,
    QProgressBar, QSplitter, QGroupBox,
)

from ..core.sd_import import (
    DEFAULT_KEY_PATH, CardSurvey, candidate_cards, import_files,
    load_private_key, read_image, survey_card,
)


class ImportWorker(QThread):
    """Importing hundreds of sealed files takes long enough to block the UI."""
    progress = pyqtSignal(int, int, str)
    finished_with = pyqtSignal(object)

    def __init__(self, files, output, key):
        super().__init__()
        self.files, self.output, self.key = files, output, key

    def run(self):
        result = import_files(
            self.files, self.output, private_key=self.key,
            progress=lambda i, n, name: self.progress.emit(i, n, name))
        self.finished_with.emit(result)


class CardTab(QWidget):
    def __init__(self):
        super().__init__()
        self.survey: Optional[CardSurvey] = None
        self.worker: Optional[ImportWorker] = None
        self._build()
        self.rescan()

    def _build(self):
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.card_combo = QComboBox()
        self.card_combo.setMinimumWidth(360)
        self.card_combo.setEditable(True)          # allow a hand-typed path
        bar.addWidget(self.card_combo)
        for text, slot in (("Rescan", self.rescan),
                           ("Browse...", self.browse),
                           ("Read card", self.read_card)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch()
        layout.addLayout(bar)

        self.key_label = QLabel("")
        self.key_label.setStyleSheet("color: #888;")
        layout.addWidget(self.key_label)

        self.summary = QLabel("Insert a payload card and press Read card.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Images"))
        self.image_list = QListWidget()
        self.image_list.currentItemChanged.connect(self.show_preview)
        left_layout.addWidget(self.image_list)
        split.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.preview = QLabel("Select an image")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(420, 320)
        self.preview.setStyleSheet("background:#151515; border-radius:4px;")
        right_layout.addWidget(self.preview)
        self.preview_caption = QLabel("")
        self.preview_caption.setStyleSheet("color: #888; font-size: 11px;")
        self.preview_caption.setWordWrap(True)
        right_layout.addWidget(self.preview_caption)
        split.addWidget(right)
        split.setSizes([320, 560])
        layout.addWidget(split)

        actions = QGroupBox("Import")
        row = QHBoxLayout(actions)
        self.want_images = QCheckBox("Images"); self.want_images.setChecked(True)
        self.want_telemetry = QCheckBox("Telemetry"); self.want_telemetry.setChecked(True)
        self.want_logs = QCheckBox("Logs"); self.want_logs.setChecked(True)
        for w in (self.want_images, self.want_telemetry, self.want_logs):
            row.addWidget(w)
        self.import_btn = QPushButton("Import && decrypt...")
        self.import_btn.clicked.connect(self.do_import)
        row.addWidget(self.import_btn)
        row.addStretch()
        layout.addWidget(actions)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    # -- card selection ----------------------------------------------------

    def rescan(self):
        self.card_combo.clear()
        for card in candidate_cards():
            label = f"{card['path']} — {card['detail']}"
            self.card_combo.addItem(label, card["path"])
        if not self.card_combo.count():
            self.card_combo.addItem("No payload card found", None)

        key = load_private_key()
        self.key_label.setText(
            f"Recording key loaded from {DEFAULT_KEY_PATH}" if key
            else f"No recording key at {DEFAULT_KEY_PATH} — sealed files cannot be opened")

    def browse(self):
        path = QFileDialog.getExistingDirectory(self, "Select the card or its root")
        if path:
            self.card_combo.addItem(path, path)
            self.card_combo.setCurrentIndex(self.card_combo.count() - 1)

    def _selected_path(self) -> Optional[str]:
        data = self.card_combo.currentData()
        if data:
            return data
        text = self.card_combo.currentText().split(" — ")[0].strip()
        return text or None

    # -- reading -----------------------------------------------------------

    def read_card(self):
        path = self._selected_path()
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "No card", "Choose a card path first.")
            return
        try:
            self.survey = survey_card(path)
        except Exception as exc:
            QMessageBox.critical(self, "Could not read the card", str(exc))
            return

        s = self.survey
        if s.sealed_count == 0:
            verdict = "nothing is sealed; all readable"
        elif s.readable:
            verdict = f"{s.sealed_count} sealed file(s), and the key matches"
        else:
            verdict = f"{s.sealed_count} sealed file(s) that CANNOT be opened"

        text = (f"<b>{s.callsign or 'unknown payload'}</b> — {len(s.images)} images, "
                f"{len(s.telemetry)} telemetry logs, {len(s.logs)} log(s)<br>"
                f"{verdict}")
        if s.payload_public_key:
            text += f"<br><span style='font-size:11px'>sealed to {s.payload_public_key}</span>"
        for note in s.notes:
            text += f"<br><span style='color:#e88'>{note}</span>"
        self.summary.setText(text)

        self.image_list.clear()
        for entry in s.images:
            item = QListWidgetItem(("🔒 " if entry.sealed else "") + entry.name)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.image_list.addItem(item)

    def show_preview(self, current, _previous=None):
        if current is None:
            return
        entry = current.data(Qt.ItemDataRole.UserRole)
        data = read_image(entry, load_private_key())
        if data is None:
            self.preview.setText("🔒 sealed — no key that can open this")
            self.preview_caption.setText(entry.name)
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.preview.setText("could not decode this image")
            self.preview_caption.setText(entry.name)
            return
        self.preview.setPixmap(pixmap.scaled(
            self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self.preview_caption.setText(
            f"{entry.name} — {pixmap.width()}x{pixmap.height()}, "
            f"{entry.size / 1000:.0f} kB on the card"
            + (" (decrypted for display)" if entry.sealed else ""))

    # -- import ------------------------------------------------------------

    def do_import(self):
        if not self.survey:
            QMessageBox.warning(self, "No card", "Read a card first.")
            return

        selected = []
        if self.want_images.isChecked():    selected += self.survey.images
        if self.want_telemetry.isChecked(): selected += self.survey.telemetry
        if self.want_logs.isChecked():      selected += self.survey.logs
        if not selected:
            QMessageBox.information(self, "Nothing selected", "Choose what to import.")
            return

        output = QFileDialog.getExistingDirectory(self, "Where should the files go?")
        if not output:
            return
        output = str(Path(output) / (self.survey.callsign or "payload"))

        self.progress.setVisible(True)
        self.progress.setMaximum(len(selected))
        self.import_btn.setEnabled(False)

        self.worker = ImportWorker(selected, output, load_private_key())
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_with.connect(self._on_finished)
        self.worker.start()

    def _on_progress(self, index, total, name):
        self.progress.setValue(index)
        self.status.setText(f"{index}/{total}  {name}")

    def _on_finished(self, result):
        self.progress.setVisible(False)
        self.import_btn.setEnabled(True)
        self.status.setText(
            f"{result.decrypted} decrypted, {result.copied} copied, "
            f"{result.skipped} already present, {result.failed} failed "
            f"→ {result.output_dir}")
        if result.errors:
            QMessageBox.warning(self, "Some files could not be recovered",
                                "\n".join(result.errors[:15]))
