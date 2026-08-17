#!/usr/bin/env python3
"""
RaptorHabGS - High Altitude Balloon Ground Station
Cross-platform Python port of the macOS application
"""

import sys
from pathlib import Path

# CRITICAL: Set attribute and import QtWebEngineWidgets BEFORE creating QApplication
from PyQt6.QtCore import Qt, QCoreApplication

# Set attribute before any QApplication is created
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

# Now import QtWebEngineWidgets
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPalette, QColor


def main():
    """Application entry point."""
    # Set application attributes
    QCoreApplication.setOrganizationName("RaptorHab")
    QCoreApplication.setApplicationName("RaptorHabGS")
    QCoreApplication.setApplicationVersion("1.0.0")
    
    # Create application
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    # Set dark palette for modern look
    set_dark_palette(app)
    
    # Create data directories
    from raptorhabgs.core.config import get_data_directory
    data_dir = get_data_directory()
    for subdir in ["images", "missions", "telemetry", "logs"]:
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)
    
    # Create and show main window
    from raptorhabgs.ui.main_window import MainWindow
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())


def set_dark_palette(app):
    """Set a dark color palette for the application."""
    palette = QPalette()
    
    # Base colors
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 35))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(25, 25, 25))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(35, 35, 35))
    
    # Disabled colors
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(127, 127, 127))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(127, 127, 127))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(127, 127, 127))
    
    app.setPalette(palette)


if __name__ == "__main__":
    main()
