"""
Web server for RaptorHabGS remote access.
Provides a web-based GUI with real-time updates via WebSocket.
"""

import json
import threading
import logging
import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, render_template, jsonify, request, send_from_directory, send_file, Response
from flask_socketio import SocketIO, emit

# Use web-compatible managers that don't require PyQt6
from ..core.web_managers import WebGroundStationManager, WebGPSManager
from ..core.sondehub import SondeHubManager
from ..core.prediction import LandingPredictionManager
from ..core.mission_manager import MissionManager
from ..core.config import get_config, save_config, get_data_directory
from ..core.telemetry import TelemetryPoint

# Reduce Flask/Werkzeug logging noise
log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)


class WebServer:
    """Flask-based web server for remote GUI access."""
    
    def __init__(self, host: str = "0.0.0.0", port: int = 5000):
        self.host = host
        self.port = port
        
        # Flask app
        template_dir = Path(__file__).parent / "templates"
        static_dir = Path(__file__).parent / "static"
        
        self.app = Flask(
            __name__,
            template_folder=str(template_dir),
            static_folder=str(static_dir)
        )
        self.app.config['SECRET_KEY'] = 'raptorhabgs-secret-key'
        
        # SocketIO for real-time updates
        self.socketio = SocketIO(
            self.app, 
            cors_allowed_origins="*", 
            async_mode='threading',
            logger=False,
            engineio_logger=False
        )
        
        # Core managers (using web-compatible versions)
        self.ground_station = WebGroundStationManager()
        self.gps_manager = WebGPSManager()
        self.sondehub = SondeHubManager()
        self.prediction_manager = LandingPredictionManager()
        self.mission_manager = MissionManager()
        self.config = get_config()
        
        # Apply config
        self.mission_manager.auto_record_enabled = self.config.auto_record
        self.sondehub.set_config(self.config.sondehub)
        
        # Setup routes and events
        self._setup_routes()
        self._setup_socketio_events()
        self._connect_callbacks()
    
    def _setup_routes(self):
        """Setup Flask routes."""
        
        @self.app.route('/')
        def index():
            return render_template('index.html')
        
        # ==================== Status ====================
        @self.app.route('/api/status')
        def get_status():
            telem = self.ground_station.latest_telemetry
            gps = self.gps_manager.current_position
            bearing = self.gps_manager.current_bearing
            pred = self.prediction_manager.current_prediction
            
            return jsonify({
                'is_receiving': self.ground_station.is_receiving,
                'is_configured': self.ground_station.is_configured,
                'gps_connected': self.gps_manager.is_connected,
                'is_recording': self.mission_manager.is_recording,
                'packets_received': self.ground_station.statistics.packets_valid,
                'rssi': self.ground_station.current_rssi,
                'snr': self.ground_station.current_snr,
                'latest_telemetry': telem.to_dict() if telem else None,
                'gps_position': {
                    'latitude': gps.latitude,
                    'longitude': gps.longitude,
                    'altitude': gps.altitude,
                    'satellites': gps.satellites,
                } if gps and gps.is_valid else None,
                'bearing': {
                    'bearing': bearing.bearing,
                    'distance': bearing.distance,
                    'elevation': bearing.elevation,
                    'cardinal': bearing.cardinal_direction,
                } if bearing else None,
                'prediction': {
                    'latitude': pred.latitude,
                    'longitude': pred.longitude,
                    'time_to_landing': pred.time_to_landing,
                    'distance_to_landing': pred.distance_to_landing,
                    'bearing_to_landing': pred.bearing_to_landing,
                    'confidence': pred.confidence,
                    'phase': pred.phase,
                } if pred else None,
            })
        
        # ==================== Ports ====================
        @self.app.route('/api/ports')
        def get_ports():
            return jsonify(self.ground_station.get_available_ports())
        
        # ==================== Radio Control ====================
        @self.app.route('/api/start', methods=['POST'])
        def start_receiving():
            data = request.get_json() or {}
            port = data.get('port') or self.config.serial_port
            
            if not port:
                return jsonify({'error': 'No port specified'}), 400
            
            self.config.serial_port = port
            save_config()
            
            if self.ground_station.start_receiving(port):
                return jsonify({'status': 'started', 'port': port})
            return jsonify({'error': 'Failed to start'}), 500
        
        @self.app.route('/api/stop', methods=['POST'])
        def stop_receiving():
            self.ground_station.stop_receiving()
            return jsonify({'status': 'stopped'})
        
        # ==================== GPS ====================
        @self.app.route('/api/gps/connect', methods=['POST'])
        def connect_gps():
            data = request.get_json() or {}
            port = data.get('port')
            baud = data.get('baud', 9600)
            
            if not port:
                return jsonify({'error': 'No port specified'}), 400
            
            if self.gps_manager.connect(port, baud):
                self.config.gps.port = port
                save_config()
                return jsonify({'status': 'connected', 'port': port})
            return jsonify({'error': 'Failed to connect'}), 500
        
        @self.app.route('/api/gps/disconnect', methods=['POST'])
        def disconnect_gps():
            self.gps_manager.disconnect()
            return jsonify({'status': 'disconnected'})
        
        # ==================== Telemetry ====================
        @self.app.route('/api/telemetry/history')
        def get_telemetry_history():
            limit = request.args.get('limit', 500, type=int)
            history = self.ground_station.telemetry_history[-limit:]
            return jsonify([{
                'lat': t.latitude,
                'lon': t.longitude,
                'alt': t.altitude,
                'timestamp': t.timestamp.isoformat(),
            } for t in history])
        
        @self.app.route('/api/clear_track', methods=['POST'])
        def clear_track():
            self.ground_station.clear_history()
            self.prediction_manager.reset()
            return jsonify({'status': 'cleared'})
        
        # ==================== Config ====================
        @self.app.route('/api/config', methods=['GET'])
        def get_config_api():
            return jsonify({
                'serial_port': self.config.serial_port,
                'serial_baud': self.config.serial_baud,
                'auto_record': self.config.auto_record,
                'modem': {
                    'frequency_mhz': self.config.modem.frequency_mhz,
                    'bitrate_kbps': self.config.modem.bitrate_kbps,
                    'deviation_khz': self.config.modem.deviation_khz,
                    'bandwidth_khz': self.config.modem.bandwidth_khz,
                    'preamble_bits': self.config.modem.preamble_bits,
                },
                'sondehub': {
                    'enabled': self.config.sondehub.enabled,
                    'uploader_callsign': self.config.sondehub.uploader_callsign,
                    'payload_callsign': self.config.sondehub.payload_callsign,
                    'uploader_antenna': self.config.sondehub.uploader_antenna,
                },
            })
        
        @self.app.route('/api/config', methods=['POST'])
        def set_config_api():
            data = request.get_json()
            
            if 'serial_port' in data:
                self.config.serial_port = data['serial_port']
            if 'serial_baud' in data:
                self.config.serial_baud = data['serial_baud']
            if 'auto_record' in data:
                self.config.auto_record = data['auto_record']
                self.mission_manager.auto_record_enabled = data['auto_record']
            
            if 'modem' in data:
                m = data['modem']
                if 'frequency_mhz' in m:
                    self.config.modem.frequency_mhz = m['frequency_mhz']
                if 'bitrate_kbps' in m:
                    self.config.modem.bitrate_kbps = m['bitrate_kbps']
                if 'bandwidth_khz' in m:
                    self.config.modem.bandwidth_khz = m['bandwidth_khz']
            
            if 'sondehub' in data:
                sh = data['sondehub']
                if 'enabled' in sh:
                    self.config.sondehub.enabled = sh['enabled']
                if 'uploader_callsign' in sh:
                    self.config.sondehub.uploader_callsign = sh['uploader_callsign']
                if 'payload_callsign' in sh:
                    self.config.sondehub.payload_callsign = sh['payload_callsign']
                if 'uploader_antenna' in sh:
                    self.config.sondehub.uploader_antenna = sh['uploader_antenna']
            
            self.sondehub.set_config(self.config.sondehub)
            save_config()
            return jsonify({'status': 'saved'})
        
        # ==================== Prediction ====================
        @self.app.route('/api/prediction/settings', methods=['POST'])
        def set_prediction_settings():
            data = request.get_json()
            
            if 'burst_altitude' in data:
                self.prediction_manager.burst_altitude = data['burst_altitude']
            if 'descent_rate' in data:
                self.prediction_manager.descent_rate_sea_level = data['descent_rate']
            if 'ascent_rate' in data:
                self.prediction_manager.ascent_rate = data['ascent_rate']
            
            return jsonify({'status': 'updated'})
        
        # ==================== Images ====================
        @self.app.route('/api/images')
        def get_images():
            images_dir = get_data_directory() / "images"
            images = []
            if images_dir.exists():
                for f in sorted(images_dir.glob("*.webp"), key=lambda x: x.stat().st_mtime, reverse=True)[:100]:
                    images.append({
                        'filename': f.name,
                        'path': f'/api/images/{f.name}',
                        'timestamp': f.stat().st_mtime,
                    })
            return jsonify(images)
        
        @self.app.route('/api/images/<filename>')
        def serve_image(filename):
            images_dir = get_data_directory() / "images"
            return send_from_directory(str(images_dir), filename)
        
        # ==================== Missions ====================
        @self.app.route('/api/missions')
        def get_missions():
            missions = MissionManager.list_missions()
            return jsonify([m.to_dict() for m in missions])
        
        @self.app.route('/api/missions/<mission_id>')
        def get_mission(mission_id):
            data = MissionManager.load_mission(mission_id)
            if data:
                return jsonify(data)
            return jsonify({'error': 'Mission not found'}), 404
        
        @self.app.route('/api/missions/<mission_id>', methods=['DELETE'])
        def delete_mission(mission_id):
            if MissionManager.delete_mission(mission_id):
                return jsonify({'status': 'deleted'})
            return jsonify({'error': 'Failed to delete'}), 500
        
        @self.app.route('/api/missions/<mission_id>/export')
        def export_mission(mission_id):
            data = MissionManager.load_mission(mission_id)
            if not data:
                return jsonify({'error': 'Mission not found'}), 404
            
            # Generate CSV
            output = io.StringIO()
            writer = csv.writer(output)
            
            writer.writerow([
                'timestamp', 'latitude', 'longitude', 'altitude_m',
                'speed_ms', 'heading', 'vertical_speed_ms', 'satellites',
                'battery_mv', 'cpu_temp_c', 'rssi', 'snr'
            ])
            
            for t in data.get('telemetry', []):
                writer.writerow([
                    t.get('timestamp', ''),
                    t.get('latitude', 0),
                    t.get('longitude', 0),
                    t.get('altitude', 0),
                    t.get('speed', 0),
                    t.get('heading', 0),
                    t.get('vertical_speed', 0),
                    t.get('satellites', 0),
                    t.get('battery_mv', 0),
                    t.get('cpu_temp', 0),
                    t.get('rx_rssi', 0),
                    t.get('rx_snr', 0),
                ])
            
            output.seek(0)
            return Response(
                output.getvalue(),
                mimetype='text/csv',
                headers={'Content-Disposition': f'attachment; filename={data.get("name", "mission")}.csv'}
            )
        
        @self.app.route('/api/missions/start', methods=['POST'])
        def start_mission():
            data = request.get_json() or {}
            name = data.get('name', '')
            
            if self.mission_manager.start_recording(name):
                self._emit_recording_status()
                return jsonify({'status': 'started'})
            return jsonify({'error': 'Failed to start recording'}), 500
        
        @self.app.route('/api/missions/stop', methods=['POST'])
        def stop_mission():
            folder = self.mission_manager.stop_recording(save=True)
            self._emit_recording_status()
            return jsonify({'status': 'stopped', 'folder': folder})
    
    def _setup_socketio_events(self):
        """Setup SocketIO event handlers."""
        
        @self.socketio.on('connect')
        def handle_connect():
            print(f"[WebServer] Client connected")
            emit('status', {
                'is_receiving': self.ground_station.is_receiving,
                'packets': self.ground_station.statistics.packets_valid,
                'rssi': self.ground_station.current_rssi,
                'snr': self.ground_station.current_snr,
            })
            
            if self.ground_station.latest_telemetry:
                emit('telemetry', self.ground_station.latest_telemetry.to_dict())
            
            if self.gps_manager.current_position and self.gps_manager.current_position.is_valid:
                pos = self.gps_manager.current_position
                emit('gps_position', {
                    'latitude': pos.latitude,
                    'longitude': pos.longitude,
                    'altitude': pos.altitude,
                    'satellites': pos.satellites,
                })
            
            self._emit_recording_status()
        
        @self.socketio.on('disconnect')
        def handle_disconnect():
            print(f"[WebServer] Client disconnected")
    
    def _emit_recording_status(self):
        """Emit current recording status."""
        self.socketio.emit('recording_status', {
            'is_recording': self.mission_manager.is_recording,
            'telemetry_count': len(self.mission_manager.recorded_telemetry),
        })
    
    def _connect_callbacks(self):
        """Connect manager callbacks to SocketIO broadcasts."""
        
        def on_telemetry(telem: TelemetryPoint):
            data = telem.to_dict()
            data['rx_rssi'] = self.ground_station.current_rssi
            data['rx_snr'] = self.ground_station.current_snr
            self.socketio.emit('telemetry', data)
            
            # Update prediction
            pred = self.prediction_manager.update(telem)
            if pred:
                self.socketio.emit('prediction', {
                    'latitude': pred.latitude,
                    'longitude': pred.longitude,
                    'time_to_landing': pred.time_to_landing,
                    'distance_to_landing': pred.distance_to_landing,
                    'bearing_to_landing': pred.bearing_to_landing,
                    'confidence': pred.confidence,
                    'phase': pred.phase,
                })
            
            # Record to mission
            self.mission_manager.record_telemetry(telem)
            
            # Emit recording status periodically
            if len(self.mission_manager.recorded_telemetry) % 10 == 0:
                self._emit_recording_status()
            
            # SondeHub upload
            if self.sondehub.config.enabled:
                if self.gps_manager.current_position:
                    pos = self.gps_manager.current_position
                    self.sondehub.set_ground_station_position(
                        pos.latitude, pos.longitude, pos.altitude
                    )
                self.sondehub.upload_telemetry(
                    telem,
                    self.ground_station.current_rssi,
                    self.ground_station.current_snr
                )
        
        # Set callback on ground station
        self.ground_station.on_telemetry_received = on_telemetry
        
        def on_status(is_receiving: bool, message: str):
            self.socketio.emit('status', {
                'is_receiving': is_receiving,
                'message': message,
                'rssi': self.ground_station.current_rssi,
                'snr': self.ground_station.current_snr,
                'packets': self.ground_station.statistics.packets_valid,
            })
        
        self.ground_station.on_status_changed = on_status
        
        def on_gps(position):
            if position.is_valid:
                self.socketio.emit('gps_position', {
                    'latitude': position.latitude,
                    'longitude': position.longitude,
                    'altitude': position.altitude,
                    'satellites': position.satellites,
                })
                
                # Update bearing if we have telemetry
                if self.ground_station.latest_telemetry:
                    self.gps_manager.update_bearing(
                        self.ground_station.latest_telemetry.latitude,
                        self.ground_station.latest_telemetry.longitude,
                        self.ground_station.latest_telemetry.altitude
                    )
        
        self.gps_manager.on_position_updated = on_gps
        
        def on_bearing(bearing):
            self.socketio.emit('bearing', {
                'bearing': bearing.bearing,
                'distance': bearing.distance,
                'elevation': bearing.elevation,
                'cardinal': bearing.cardinal_direction,
            })
        
        self.gps_manager.on_bearing_updated = on_bearing
        
        def on_image(path: str, image_id: int):
            filename = Path(path).name
            self.socketio.emit('image_decoded', {
                'image_id': image_id,
                'path': f'/api/images/{filename}',
                'filename': filename,
            })
            # Record to mission
            self.mission_manager.record_image(path, image_id)
        
        self.ground_station.on_image_decoded = on_image
        
        def on_image_progress(image_id: int, progress: float):
            self.socketio.emit('image_progress', {
                'image_id': image_id,
                'progress': progress,
            })
        
        self.ground_station.on_image_progress = on_image_progress
        
        def on_error(message: str):
            self.socketio.emit('error', {'message': message})
        
        self.ground_station.on_error = on_error
    
    def run(self, debug: bool = False):
        """Run the web server (blocking)."""
        print(f"\n{'='*60}")
        print(f"  RaptorHabGS Web Server")
        print(f"  Running on http://{self.host}:{self.port}")
        print(f"{'='*60}\n")
        self.socketio.run(
            self.app, 
            host=self.host, 
            port=self.port, 
            debug=debug,
            allow_unsafe_werkzeug=True
        )
    
    def shutdown(self):
        """Shutdown the server and managers."""
        print("[WebServer] Shutting down...")
        
        # Stop mission if recording
        if self.mission_manager.is_recording:
            self.mission_manager.stop_recording(save=True)
        
        if self.ground_station.is_receiving:
            self.ground_station.stop_receiving()
        if self.gps_manager.is_connected:
            self.gps_manager.disconnect()
