"""
Map widget using Leaflet in QWebEngineView.
"""

import json
from typing import Optional, List

from PyQt6.QtWidgets import QWidget, QVBoxLayout
from PyQt6.QtCore import QUrl, pyqtSlot, QObject
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel

from ..core.ground_station import GroundStationManager
from ..core.gps_manager import GPSManager
from ..core.telemetry import TelemetryPoint, GPSPosition, BearingDistance


# HTML template for the map
MAP_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RaptorHabGS Map</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
    <style>
        html, body, #map {
            height: 100%;
            width: 100%;
            margin: 0;
            padding: 0;
            background: #1a1a1a;
        }
        .payload-icon {
            background: #ff4444;
            border: 2px solid white;
            border-radius: 50%;
            width: 16px;
            height: 16px;
        }
        .gs-icon {
            background: #44ff44;
            border: 2px solid white;
            border-radius: 50%;
            width: 14px;
            height: 14px;
        }
        .leaflet-popup-content-wrapper {
            background: rgba(30, 30, 30, 0.95);
            color: white;
            border-radius: 8px;
        }
        .leaflet-popup-tip {
            background: rgba(30, 30, 30, 0.95);
        }
        .info-box {
            background: rgba(0, 0, 0, 0.7);
            color: white;
            padding: 10px 15px;
            border-radius: 8px;
            font-family: monospace;
            font-size: 12px;
            line-height: 1.5;
        }
    </style>
</head>
<body>
    <div id="map"></div>
    <script>
        // Initialize map
        var map = L.map('map', {
            center: [39.8283, -98.5795],  // Center of US
            zoom: 4,
            zoomControl: true
        });
        
        // Add tile layer (OpenStreetMap)
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap contributors',
            maxZoom: 19
        }).addTo(map);
        
        // Markers and layers
        var payloadMarker = null;
        var gsMarker = null;
        var trackLine = null;
        var bearingLine = null;
        var trackPoints = [];
        
        // Custom icons
        var payloadIcon = L.divIcon({
            className: 'payload-icon',
            iconSize: [16, 16],
            iconAnchor: [8, 8]
        });
        
        var gsIcon = L.divIcon({
            className: 'gs-icon',
            iconSize: [14, 14],
            iconAnchor: [7, 7]
        });
        
        // Info control
        var infoControl = L.control({position: 'topright'});
        infoControl.onAdd = function(map) {
            this._div = L.DomUtil.create('div', 'info-box');
            this._div.innerHTML = 'Waiting for data...';
            return this._div;
        };
        infoControl.update = function(text) {
            this._div.innerHTML = text;
        };
        infoControl.addTo(map);
        
        // Qt WebChannel connection
        var bridge = null;
        new QWebChannel(qt.webChannelTransport, function(channel) {
            bridge = channel.objects.bridge;
        });
        
        // Update payload position
        function updatePayload(lat, lon, alt, speed, heading) {
            if (lat == 0 && lon == 0) return;
            
            var pos = L.latLng(lat, lon);
            
            if (payloadMarker) {
                payloadMarker.setLatLng(pos);
            } else {
                payloadMarker = L.marker(pos, {icon: payloadIcon}).addTo(map);
            }
            
            // Update popup
            payloadMarker.bindPopup(
                '<b>Payload</b><br>' +
                'Lat: ' + lat.toFixed(6) + '°<br>' +
                'Lon: ' + lon.toFixed(6) + '°<br>' +
                'Alt: ' + alt.toFixed(0) + ' m<br>' +
                'Speed: ' + speed.toFixed(1) + ' m/s'
            );
            
            // Add to track
            trackPoints.push(pos);
            updateTrack();
            
            // Update info
            infoControl.update(
                'Alt: ' + alt.toFixed(0) + ' m<br>' +
                'Speed: ' + speed.toFixed(1) + ' m/s<br>' +
                'Heading: ' + heading.toFixed(0) + '°'
            );
        }
        
        // Update ground station position
        function updateGroundStation(lat, lon, alt) {
            if (lat == 0 && lon == 0) return;
            
            var pos = L.latLng(lat, lon);
            
            if (gsMarker) {
                gsMarker.setLatLng(pos);
            } else {
                gsMarker = L.marker(pos, {icon: gsIcon}).addTo(map);
                gsMarker.bindPopup('<b>Ground Station</b>');
            }
        }
        
        // Update bearing line
        function updateBearing(gsLat, gsLon, payloadLat, payloadLon) {
            if (bearingLine) {
                map.removeLayer(bearingLine);
            }
            
            if (gsLat && payloadLat) {
                bearingLine = L.polyline([
                    [gsLat, gsLon],
                    [payloadLat, payloadLon]
                ], {
                    color: '#ffa500',
                    weight: 2,
                    dashArray: '10, 10',
                    opacity: 0.8
                }).addTo(map);
            }
        }
        
        // Update track line
        function updateTrack() {
            if (trackLine) {
                trackLine.setLatLngs(trackPoints);
            } else {
                trackLine = L.polyline(trackPoints, {
                    color: '#4488ff',
                    weight: 3,
                    opacity: 0.8
                }).addTo(map);
            }
        }
        
        // Clear track
        function clearTrack() {
            trackPoints = [];
            if (trackLine) {
                map.removeLayer(trackLine);
                trackLine = null;
            }
        }
        
        // Center map
        function centerOn(lat, lon, zoom) {
            map.setView([lat, lon], zoom || map.getZoom());
        }
        
        // Fit to show all markers
        function fitBounds() {
            var bounds = [];
            if (payloadMarker) bounds.push(payloadMarker.getLatLng());
            if (gsMarker) bounds.push(gsMarker.getLatLng());
            if (bounds.length > 0) {
                map.fitBounds(L.latLngBounds(bounds), {padding: [50, 50]});
            }
        }
        
        // Add prediction marker
        var predictionMarker = null;
        var predictionCircle = null;
        
        function updatePrediction(lat, lon, confidence) {
            if (lat == 0 && lon == 0) {
                if (predictionMarker) {
                    map.removeLayer(predictionMarker);
                    predictionMarker = null;
                }
                if (predictionCircle) {
                    map.removeLayer(predictionCircle);
                    predictionCircle = null;
                }
                return;
            }
            
            var pos = L.latLng(lat, lon);
            
            // Radius based on confidence
            var radius = 500;
            var color = '#ff0000';
            if (confidence == 'high') {
                radius = 200;
                color = '#00ff00';
            } else if (confidence == 'medium') {
                radius = 500;
                color = '#ffff00';
            } else {
                radius = 1000;
                color = '#ff6600';
            }
            
            if (predictionMarker) {
                predictionMarker.setLatLng(pos);
            } else {
                predictionMarker = L.marker(pos, {
                    icon: L.divIcon({
                        className: '',
                        html: '<div style="background:#ff0000;width:12px;height:12px;border:2px solid white;border-radius:2px;transform:rotate(45deg);"></div>',
                        iconSize: [16, 16],
                        iconAnchor: [8, 8]
                    })
                }).addTo(map);
                predictionMarker.bindPopup('<b>Predicted Landing</b>');
            }
            
            if (predictionCircle) {
                predictionCircle.setLatLng(pos);
                predictionCircle.setRadius(radius);
                predictionCircle.setStyle({color: color, fillColor: color});
            } else {
                predictionCircle = L.circle(pos, {
                    radius: radius,
                    color: color,
                    fillColor: color,
                    fillOpacity: 0.2,
                    weight: 2
                }).addTo(map);
            }
        }
    </script>
</body>
</html>
"""


class MapBridge(QObject):
    """Bridge object for JavaScript communication."""
    
    def __init__(self, parent=None):
        super().__init__(parent)


class MapWidget(QWidget):
    """Map widget using Leaflet."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        self._setup_ui()
        
        # Track last positions for bearing line
        self._last_gs_lat = 0.0
        self._last_gs_lon = 0.0
        self._last_payload_lat = 0.0
        self._last_payload_lon = 0.0
    
    def _setup_ui(self):
        """Setup the map UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Create web view
        self.web_view = QWebEngineView()
        
        # Setup web channel for Python-JS communication
        self.channel = QWebChannel()
        self.bridge = MapBridge()
        self.channel.registerObject("bridge", self.bridge)
        self.web_view.page().setWebChannel(self.channel)
        
        # Load map HTML
        self.web_view.setHtml(MAP_HTML)
        
        layout.addWidget(self.web_view)
    
    def _run_js(self, script: str):
        """Run JavaScript in the web view."""
        self.web_view.page().runJavaScript(script)
    
    def update_payload(self, lat: float, lon: float, alt: float = 0, 
                       speed: float = 0, heading: float = 0):
        """Update payload position on map."""
        self._last_payload_lat = lat
        self._last_payload_lon = lon
        
        self._run_js(f"updatePayload({lat}, {lon}, {alt}, {speed}, {heading});")
    
    def add_track_point(self, lat: float, lon: float):
        """Add a point to the track (already handled in updatePayload)."""
        pass  # Track is updated in updatePayload JS function
    
    def update_ground_station(self, lat: float, lon: float, alt: float = 0):
        """Update ground station position on map."""
        self._last_gs_lat = lat
        self._last_gs_lon = lon
        
        self._run_js(f"updateGroundStation({lat}, {lon}, {alt});")
    
    def update_bearing_line(self):
        """Update the bearing line between GS and payload."""
        if (self._last_gs_lat != 0 or self._last_gs_lon != 0) and \
           (self._last_payload_lat != 0 or self._last_payload_lon != 0):
            self._run_js(f"updateBearing({self._last_gs_lat}, {self._last_gs_lon}, "
                        f"{self._last_payload_lat}, {self._last_payload_lon});")
    
    def update_prediction(self, lat: float, lon: float, radius: float = 500, 
                          confidence: str = "low"):
        """Update landing prediction on map."""
        self._run_js(f"updatePrediction({lat}, {lon}, '{confidence}');")
    
    def clear_prediction(self):
        """Clear the prediction marker."""
        self._run_js("updatePrediction(0, 0, 'low');")
    
    def clear_track(self):
        """Clear the track line."""
        self._run_js("clearTrack();")
    
    def center_on(self, lat: float, lon: float, zoom: int = None):
        """Center map on coordinates."""
        if zoom:
            self._run_js(f"centerOn({lat}, {lon}, {zoom});")
        else:
            self._run_js(f"centerOn({lat}, {lon});")
    
    def fit_bounds(self):
        """Fit map to show all markers."""
        self._run_js("fitBounds();")
