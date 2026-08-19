"""UI components for RaptorHabGS.

Components are imported on-demand to avoid QtWebEngine import order issues.
"""

# Don't auto-import - let main.py control import order
__all__ = [
    'MainWindow',
    'TrackingTab', 
    'PredictionTab',
    'ImagesTab',
    'MissionsTab',
    'MapWidget',
    'SettingsDialog',
]
