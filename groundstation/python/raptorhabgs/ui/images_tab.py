"""
Images Tab - Gallery of received images with progress display.
"""

import os
from pathlib import Path
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGroupBox, QLabel,
    QPushButton, QScrollArea, QGridLayout, QFrame, QProgressBar,
    QSplitter, QFileDialog, QMessageBox
)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QPixmap, QFont, QImage

from ..core.config import get_data_directory


class ImageThumbnail(QFrame):
    """Clickable image thumbnail."""
    
    def __init__(self, path: str, image_id: int, on_click):
        super().__init__()
        
        self.path = path
        self.image_id = image_id
        self.on_click = on_click
        self.selected = False
        
        self.setFixedSize(120, 90)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_style()
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setScaledContents(False)
        
        # Load and scale image
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                116, 86,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self.image_label.setPixmap(scaled)
        else:
            self.image_label.setText("?")
        
        layout.addWidget(self.image_label)
    
    def _update_style(self):
        if self.selected:
            self.setStyleSheet("""
                QFrame {
                    background-color: #2d2d2d;
                    border: 2px solid #4488ff;
                    border-radius: 4px;
                }
            """)
        else:
            self.setStyleSheet("""
                QFrame {
                    background-color: #2d2d2d;
                    border: 2px solid transparent;
                    border-radius: 4px;
                }
                QFrame:hover {
                    border: 2px solid #4488ff;
                }
            """)
    
    def set_selected(self, selected: bool):
        self.selected = selected
        self._update_style()
    
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.on_click(self)


class ImagesTab(QWidget):
    """Images tab with gallery and viewer."""
    
    def __init__(self, ground_station):
        super().__init__()
        
        self.ground_station = ground_station
        self.thumbnails = []
        self.selected_thumbnail = None
        self.current_image_path = None
        
        self._setup_ui()
        self._load_existing_images()
    
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Left sidebar
        sidebar = self._create_sidebar()
        layout.addWidget(sidebar)
        
        # Main area with viewer and thumbnails
        main_area = QWidget()
        main_layout = QVBoxLayout(main_area)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Image viewer
        viewer_frame = QFrame()
        viewer_frame.setStyleSheet("background-color: #1a1a1a;")
        viewer_layout = QVBoxLayout(viewer_frame)
        
        self.viewer_label = QLabel("No image selected")
        self.viewer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.viewer_label.setStyleSheet("color: #888;")
        self.viewer_label.setMinimumHeight(400)
        viewer_layout.addWidget(self.viewer_label)
        
        main_layout.addWidget(viewer_frame, 1)
        
        # Thumbnail strip
        thumb_frame = QFrame()
        thumb_frame.setMaximumHeight(150)
        thumb_frame.setStyleSheet("background-color: #252525;")
        thumb_layout = QVBoxLayout(thumb_frame)
        thumb_layout.setContentsMargins(10, 10, 10, 10)
        
        # Scroll area for thumbnails
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        
        self.thumb_container = QWidget()
        self.thumb_layout = QHBoxLayout(self.thumb_container)
        self.thumb_layout.setContentsMargins(0, 0, 0, 0)
        self.thumb_layout.setSpacing(10)
        self.thumb_layout.addStretch()
        
        scroll.setWidget(self.thumb_container)
        thumb_layout.addWidget(scroll)
        
        main_layout.addWidget(thumb_frame)
        
        layout.addWidget(main_area, 1)
    
    def _create_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setMaximumWidth(250)
        sidebar.setMinimumWidth(200)
        sidebar.setStyleSheet("background-color: #252525;")
        
        layout = QVBoxLayout(sidebar)
        layout.setSpacing(15)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # Progress group
        progress_group = QGroupBox("Image Reception")
        progress_layout = QVBoxLayout(progress_group)
        
        self.progress_label = QLabel("No image in progress")
        self.progress_label.setStyleSheet("color: #888;")
        progress_layout.addWidget(self.progress_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        progress_layout.addWidget(self.progress_bar)
        
        self.image_id_label = QLabel("")
        self.image_id_label.setStyleSheet("color: #888; font-size: 11px;")
        progress_layout.addWidget(self.image_id_label)
        
        layout.addWidget(progress_group)
        
        # Stats group
        stats_group = QGroupBox("Statistics")
        stats_layout = QVBoxLayout(stats_group)
        
        self.total_images_label = QLabel("Total Images: 0")
        stats_layout.addWidget(self.total_images_label)
        
        self.session_images_label = QLabel("This Session: 0")
        stats_layout.addWidget(self.session_images_label)
        
        layout.addWidget(stats_group)
        
        # Actions group
        actions_group = QGroupBox("Actions")
        actions_layout = QVBoxLayout(actions_group)
        
        refresh_btn = QPushButton("↻ Refresh Gallery")
        refresh_btn.clicked.connect(self._load_existing_images)
        actions_layout.addWidget(refresh_btn)
        
        save_btn = QPushButton("💾 Save Selected")
        save_btn.clicked.connect(self._save_selected)
        actions_layout.addWidget(save_btn)
        
        open_folder_btn = QPushButton("📁 Open Folder")
        open_folder_btn.clicked.connect(self._open_folder)
        actions_layout.addWidget(open_folder_btn)
        
        layout.addWidget(actions_group)
        
        # Selected image info
        info_group = QGroupBox("Selected Image")
        info_layout = QVBoxLayout(info_group)
        
        self.selected_name_label = QLabel("--")
        self.selected_name_label.setWordWrap(True)
        info_layout.addWidget(self.selected_name_label)
        
        self.selected_size_label = QLabel("--")
        self.selected_size_label.setStyleSheet("color: #888;")
        info_layout.addWidget(self.selected_size_label)
        
        self.selected_date_label = QLabel("--")
        self.selected_date_label.setStyleSheet("color: #888;")
        info_layout.addWidget(self.selected_date_label)
        
        layout.addWidget(info_group)
        
        layout.addStretch()
        
        return sidebar
    
    def _load_existing_images(self):
        """Load existing images from the images directory."""
        images_dir = get_data_directory() / "images"
        
        # Clear existing thumbnails
        for thumb in self.thumbnails:
            thumb.deleteLater()
        self.thumbnails.clear()
        
        if not images_dir.exists():
            images_dir.mkdir(parents=True, exist_ok=True)
            return
        
        # Load images sorted by modification time (newest first)
        image_files = sorted(
            images_dir.glob("*.webp"),
            key=lambda x: x.stat().st_mtime,
            reverse=True
        )
        
        for i, img_path in enumerate(image_files[:100]):  # Limit to 100 images
            # Extract image ID from filename if possible
            try:
                image_id = int(img_path.stem.split('_')[-1])
            except:
                image_id = i
            
            self._add_thumbnail(str(img_path), image_id, insert_at_start=False)
        
        self.total_images_label.setText(f"Total Images: {len(self.thumbnails)}")
        
        # Select first image if available
        if self.thumbnails:
            self._on_thumbnail_click(self.thumbnails[0])
    
    def _add_thumbnail(self, path: str, image_id: int, insert_at_start: bool = True):
        """Add a thumbnail to the gallery."""
        thumb = ImageThumbnail(path, image_id, self._on_thumbnail_click)
        self.thumbnails.append(thumb)
        
        if insert_at_start:
            self.thumb_layout.insertWidget(0, thumb)
        else:
            # Insert before the stretch
            self.thumb_layout.insertWidget(self.thumb_layout.count() - 1, thumb)
    
    def _on_thumbnail_click(self, thumbnail: ImageThumbnail):
        """Handle thumbnail click."""
        # Deselect previous
        if self.selected_thumbnail:
            self.selected_thumbnail.set_selected(False)
        
        # Select new
        thumbnail.set_selected(True)
        self.selected_thumbnail = thumbnail
        self.current_image_path = thumbnail.path
        
        # Show full image
        pixmap = QPixmap(thumbnail.path)
        if not pixmap.isNull():
            # Scale to fit viewer
            scaled = pixmap.scaled(
                self.viewer_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self.viewer_label.setPixmap(scaled)
        
        # Update info
        path = Path(thumbnail.path)
        self.selected_name_label.setText(path.name)
        
        try:
            size = path.stat().st_size
            if size > 1024 * 1024:
                self.selected_size_label.setText(f"Size: {size / 1024 / 1024:.2f} MB")
            else:
                self.selected_size_label.setText(f"Size: {size / 1024:.1f} KB")
            
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            self.selected_date_label.setText(f"Date: {mtime.strftime('%Y-%m-%d %H:%M')}")
        except:
            self.selected_size_label.setText("--")
            self.selected_date_label.setText("--")
    
    def _save_selected(self):
        """Save selected image to a chosen location."""
        if not self.current_image_path:
            QMessageBox.information(self, "No Image", "Please select an image first.")
            return
        
        source = Path(self.current_image_path)
        
        dest_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Image",
            str(Path.home() / source.name),
            "WebP Images (*.webp);;All Files (*)"
        )
        
        if dest_path:
            import shutil
            shutil.copy2(self.current_image_path, dest_path)
            QMessageBox.information(self, "Saved", f"Image saved to:\n{dest_path}")
    
    def _open_folder(self):
        """Open the images folder in file manager."""
        images_dir = get_data_directory() / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        
        import subprocess
        import sys
        
        if sys.platform == "win32":
            os.startfile(str(images_dir))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(images_dir)])
        else:
            subprocess.run(["xdg-open", str(images_dir)])
    
    def add_image(self, path: str, image_id: int):
        """Add a newly decoded image."""
        self._add_thumbnail(path, image_id, insert_at_start=True)
        
        # Update stats
        self.total_images_label.setText(f"Total Images: {len(self.thumbnails)}")
        
        # Select the new image
        if self.thumbnails:
            self._on_thumbnail_click(self.thumbnails[0])
        
        # Hide progress
        self.progress_bar.setVisible(False)
        self.progress_label.setText("Image decoded!")
        self.image_id_label.setText("")
    
    def update_progress(self, image_id: int, progress: float):
        """Update image reception progress."""
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(int(progress))
        self.progress_label.setText(f"Receiving: {progress:.0f}%")
        self.image_id_label.setText(f"Image ID: {image_id}")
    
    def resizeEvent(self, event):
        """Handle resize to update image scaling."""
        super().resizeEvent(event)
        
        if self.current_image_path and self.selected_thumbnail:
            pixmap = QPixmap(self.current_image_path)
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    self.viewer_label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
                self.viewer_label.setPixmap(scaled)
