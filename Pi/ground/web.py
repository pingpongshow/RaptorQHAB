"""
RaptorHab Ground Station - Web Interface
Flask-based web UI with WebSocket for real-time updates

Note: Command transmission to airborne unit is not supported.
All airborne configuration must be done via config file on the airborne unit.
"""

import logging
import json
import time
import os
from datetime import datetime
from typing import Dict, Optional, Any, TYPE_CHECKING
from threading import Thread

from flask import Flask, render_template, jsonify, request, send_file, Response
from flask_socketio import SocketIO, emit

if TYPE_CHECKING:
    from ground.receiver import PacketReceiver
    from ground.telemetry import TelemetryProcessor
    from ground.decoder import FountainDecoder
    from ground.storage import ImageStorage
    from ground.config import GroundConfig

from ground.offline_maps import OfflineMapManager, get_content_type

logger = logging.getLogger(__name__)


def create_app(
    config: 'GroundConfig',
    receiver: Optional['PacketReceiver'] = None,
    telemetry: Optional['TelemetryProcessor'] = None,
    decoder: Optional['FountainDecoder'] = None,
    storage: Optional['ImageStorage'] = None,
    ground_station: Optional[Any] = None,
) -> tuple:
    """
    Create Flask application
    
    Args:
        config: Ground station configuration
        receiver: Packet receiver instance
        telemetry: Telemetry processor instance
        decoder: Fountain decoder instance
        storage: Image storage instance
        ground_station: Main ground station instance (for GPS access)
        
    Returns:
        Tuple of (Flask app, SocketIO instance)
    """
    # Create Flask app
    template_dir = os.path.join(os.path.dirname(__file__), 'templates')
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    
    app = Flask(
        __name__,
        template_folder=template_dir,
        static_folder=static_dir
    )
    app.config['SECRET_KEY'] = 'raptorhab-ground-station'
    
    # Add CORS headers to all responses
    @app.after_request
    def add_cors_headers(response):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Accept'
        return response
    
    # Create SocketIO
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
    
    # Store references
    app.config['receiver'] = receiver
    app.config['telemetry'] = telemetry
    app.config['decoder'] = decoder
    app.config['storage'] = storage
    app.config['ground_config'] = config
    app.config['ground_station'] = ground_station
    
    # Initialize offline maps manager
    offline_maps = OfflineMapManager(config.map_offline_path)
    if config.map_offline_file:
        offline_maps.set_default(config.map_offline_file)
    app.config['offline_maps'] = offline_maps
    
    # === Routes ===
    
    @app.route('/')
    def index():
        """Main dashboard"""
        return render_template('index.html', config=config)
    
    @app.route('/map')
    def map_view():
        """Map view"""
        return render_template('map.html', config=config)
    
    @app.route('/images')
    def images_view():
        """Image gallery"""
        return render_template('images.html', config=config)
    
    # === API Endpoints ===
    
    @app.route('/api/status')
    def api_status():
        """Get system status"""
        status = {
            'time': time.time(),
            'receiver': receiver.get_stats() if receiver else {},
            'telemetry': telemetry.get_flight_stats() if telemetry else {},
            'decoder': decoder.get_status() if decoder else {},
            'storage': storage.get_storage_stats() if storage else {},
        }
        
        # Add tracking info if ground GPS is available
        if ground_station:
            tracking = ground_station.get_tracking_info()
            if tracking:
                status['tracking'] = tracking
        
        return jsonify(status)
    
    @app.route('/api/tracking')
    def api_tracking():
        """Get tracking info (distance/bearing to airborne unit)"""
        if not ground_station:
            return jsonify({'error': 'Ground station reference not available'}), 503
        
        tracking = ground_station.get_tracking_info()
        if tracking:
            return jsonify(tracking)
        return jsonify({'error': 'Tracking not available (no GPS fix or no airborne telemetry)'}), 503
    
    @app.route('/api/ground_gps')
    def api_ground_gps():
        """Get ground station GPS position"""
        if not ground_station:
            return jsonify({'error': 'Ground station reference not available'}), 503
        
        gps = ground_station.get_ground_position()
        if gps and gps.position_valid:
            return jsonify({
                'latitude': gps.latitude,
                'longitude': gps.longitude,
                'altitude': gps.altitude,
                'satellites': gps.satellites,
                'fix_type': gps.fix_type.value if hasattr(gps.fix_type, 'value') else gps.fix_type,
                'valid': True
            })
        return jsonify({'valid': False, 'error': 'No GPS fix'})
    
    @app.route('/api/telemetry/latest')
    def api_telemetry_latest():
        """Get latest telemetry"""
        if not telemetry:
            return jsonify({'error': 'Telemetry not available'}), 503
        
        point = telemetry.get_latest()
        if point:
            return jsonify(point.to_dict())
        return jsonify({})
    
    @app.route('/api/telemetry/recent')
    def api_telemetry_recent():
        """Get recent telemetry points"""
        if not telemetry:
            return jsonify({'error': 'Telemetry not available'}), 503
        
        count = request.args.get('count', 100, type=int)
        points = telemetry.buffer.get_latest(count)
        return jsonify([p.to_dict() for p in points])
    
    @app.route('/api/telemetry/track')
    def api_telemetry_track():
        """Get GPS track for mapping"""
        if not telemetry:
            return jsonify({'error': 'Telemetry not available'}), 503
        
        start = request.args.get('start', type=float)
        end = request.args.get('end', type=float)
        interval = request.args.get('interval', 1.0, type=float)
        session = request.args.get('session', 'current')  # 'current', 'all', or specific session_id
        
        track = telemetry.database.get_track(start, end, interval, session_id=session)
        return jsonify(track)
    
    @app.route('/api/telemetry/track/clear', methods=['POST'])
    def api_telemetry_track_clear():
        """Clear track for current session"""
        if not telemetry:
            return jsonify({'error': 'Telemetry not available'}), 503
        
        data = request.get_json() or {}
        session_id = data.get('session_id')  # None = current session
        
        count = telemetry.database.clear_track(session_id)
        return jsonify({'status': 'ok', 'points_deleted': count})
    
    @app.route('/api/telemetry/sessions')
    def api_telemetry_sessions():
        """Get list of telemetry sessions"""
        if not telemetry:
            return jsonify({'error': 'Telemetry not available'}), 503
        
        sessions = telemetry.database.get_sessions()
        return jsonify(sessions)
    
    @app.route('/api/images')
    def api_images():
        """Get image list"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        count = request.args.get('count', 20, type=int)
        images = storage.get_recent_images(count)
        return jsonify([
            {
                'image_id': img.image_id,
                'session_id': img.session_id,
                'filename': img.filename,
                'width': img.width,
                'height': img.height,
                'size_bytes': img.size_bytes,
                'capture_time': img.capture_time,
                'received_time': img.received_time,
            }
            for img in images
        ])
    
    @app.route('/api/images/<int:image_id>')
    def api_image(image_id: int):
        """Get image data"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        data = storage.get_image_data(image_id)
        if data:
            return Response(data, mimetype='image/webp')
        return jsonify({'error': 'Image not found'}), 404
    
    @app.route('/api/images/<int:image_id>/thumbnail')
    def api_image_thumbnail(image_id: int):
        """Get image thumbnail"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        data = storage.get_thumbnail_data(image_id)
        if data:
            return Response(data, mimetype='image/webp')
        
        # Fall back to full image
        data = storage.get_image_data(image_id)
        if data:
            return Response(data, mimetype='image/webp')
        
        return jsonify({'error': 'Image not found'}), 404
    
    @app.route('/api/images/pending')
    def api_images_pending():
        """Get pending image reconstructions"""
        if not decoder:
            return jsonify({'error': 'Decoder not available'}), 503
        
        return jsonify(decoder.get_pending_progress())
    
    # ========== Offline Map Tile API ==========
    
    @app.route('/api/tiles/<int:z>/<int:x>/<int:y>.png')
    def api_tile_png(z: int, x: int, y: int):
        """
        Serve map tiles (PNG format)
        Falls back to online tiles if offline not available
        """
        return serve_tile(z, x, y, 'png')
    
    @app.route('/api/tiles/<int:z>/<int:x>/<int:y>.<ext>')
    def api_tile(z: int, x: int, y: int, ext: str):
        """Serve map tiles with specified format"""
        return serve_tile(z, x, y, ext)
    
    @app.route('/api/tiles/<map_name>/<int:z>/<int:x>/<int:y>.<ext>')
    def api_tile_named(map_name: str, z: int, x: int, y: int, ext: str):
        """Serve map tiles from a specific offline map"""
        return serve_tile(z, x, y, ext, map_name=map_name)
    
    def serve_tile(z: int, x: int, y: int, ext: str, map_name: str = None):
        """Internal function to serve tiles"""
        offline_maps = app.config.get('offline_maps')
        ground_config = app.config.get('ground_config')
        
        # Try offline first if enabled and preferred
        if ground_config.map_offline_enabled and ground_config.map_prefer_offline:
            if offline_maps and offline_maps.has_offline_maps:
                result = offline_maps.get_tile(z, x, y, map_name)
                if result:
                    data, fmt = result
                    return Response(
                        data, 
                        mimetype=get_content_type(fmt),
                        headers={
                            'Cache-Control': 'public, max-age=86400',
                            'X-Tile-Source': 'offline'
                        }
                    )
        
        # Return 204 No Content to signal client should use online fallback
        return Response(
            status=204,
            headers={'X-Tile-Source': 'none'}
        )
    
    @app.route('/api/maps/status')
    def api_maps_status():
        """Get offline maps status"""
        offline_maps = app.config.get('offline_maps')
        ground_config = app.config.get('ground_config')
        
        status = {
            'offline_enabled': ground_config.map_offline_enabled,
            'prefer_offline': ground_config.map_prefer_offline,
            'offline_available': False,
            'maps': {}
        }
        
        if offline_maps:
            map_status = offline_maps.get_status()
            status['offline_available'] = map_status.get('available', False)
            status['maps'] = map_status.get('maps', {})
            status['maps_directory'] = map_status.get('maps_directory', '')
        
        return jsonify(status)
    
    @app.route('/api/maps/config', methods=['POST'])
    def api_maps_config():
        """Update map configuration"""
        ground_config = app.config.get('ground_config')
        data = request.get_json() or {}
        
        if 'prefer_offline' in data:
            ground_config.map_prefer_offline = bool(data['prefer_offline'])
        
        if 'offline_enabled' in data:
            ground_config.map_offline_enabled = bool(data['offline_enabled'])
        
        return jsonify({'status': 'ok'})
    
    # ========== Session/Mission API Endpoints ==========
    
    @app.route('/api/sessions')
    def api_sessions():
        """Get all sessions/missions"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        sessions = storage.get_sessions()
        return jsonify([
            {
                'session_id': s.session_id,
                'name': s.name,
                'display_name': s.display_name,
                'start_time': s.start_time,
                'end_time': s.end_time,
                'image_count': s.image_count,
                'total_size_bytes': s.total_size_bytes,
                'is_current': s.session_id == storage.session_id,
            }
            for s in sessions
        ])
    
    @app.route('/api/sessions/<session_id>')
    def api_session(session_id: str):
        """Get a specific session"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        session = storage.get_session(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        
        return jsonify({
            'session_id': session.session_id,
            'name': session.name,
            'display_name': session.display_name,
            'start_time': session.start_time,
            'end_time': session.end_time,
            'image_count': session.image_count,
            'total_size_bytes': session.total_size_bytes,
            'is_current': session.session_id == storage.session_id,
        })
    
    @app.route('/api/sessions/<session_id>/images')
    def api_session_images(session_id: str):
        """Get images for a specific session"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        count = request.args.get('count', 100, type=int)
        images = storage.get_session_images(session_id, count)
        return jsonify([
            {
                'image_id': img.image_id,
                'session_id': img.session_id,
                'filename': img.filename,
                'width': img.width,
                'height': img.height,
                'size_bytes': img.size_bytes,
                'capture_time': img.capture_time,
                'received_time': img.received_time,
            }
            for img in images
        ])
    
    @app.route('/api/sessions/<session_id>/images/<int:image_id>')
    def api_session_image(session_id: str, image_id: int):
        """Get image data for a specific session"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        data = storage.get_image_data_by_session(session_id, image_id)
        if data:
            return Response(data, mimetype='image/webp')
        return jsonify({'error': 'Image not found'}), 404
    
    @app.route('/api/sessions/<session_id>/images/<int:image_id>/thumbnail')
    def api_session_image_thumbnail(session_id: str, image_id: int):
        """Get image thumbnail for a specific session"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        data = storage.get_thumbnail_data_by_session(session_id, image_id)
        if data:
            return Response(data, mimetype='image/webp')
        return jsonify({'error': 'Image not found'}), 404
    
    @app.route('/api/sessions/<session_id>/rename', methods=['POST'])
    def api_session_rename(session_id: str):
        """Rename a session"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        data = request.get_json()
        if not data or 'name' not in data:
            return jsonify({'error': 'Name required'}), 400
        
        storage.rename_session(session_id, data['name'])
        return jsonify({'status': 'ok', 'session_id': session_id, 'name': data['name']})
    
    @app.route('/api/sessions/<session_id>/delete', methods=['POST'])
    def api_session_delete(session_id: str):
        """Delete an entire session/mission"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        # Prevent deletion of current session
        if session_id == storage.session_id:
            return jsonify({'error': 'Cannot delete current active session'}), 400
        
        if storage.delete_session(session_id):
            return jsonify({'status': 'ok', 'session_id': session_id})
        return jsonify({'error': 'Failed to delete session'}), 500
    
    @app.route('/api/sessions/<session_id>/download')
    def api_session_download(session_id: str):
        """Download session as ZIP file"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        import tempfile
        
        # Create zip in temp directory
        temp_dir = tempfile.mkdtemp()
        zip_path = storage.export_session_zip(session_id, temp_dir)
        
        if not zip_path:
            return jsonify({'error': 'Failed to create ZIP or session empty'}), 404
        
        # Get session info for filename
        session = storage.get_session(session_id)
        filename = f"{session.display_name.replace(' ', '_').replace(':', '-')}.zip" if session else f"{session_id}.zip"
        
        return send_file(
            zip_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=filename
        )
    
    @app.route('/api/sessions/<session_id>/images/delete', methods=['POST'])
    def api_session_images_delete(session_id: str):
        """Delete multiple images from a session"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        data = request.get_json()
        if not data or 'image_ids' not in data:
            return jsonify({'error': 'image_ids required'}), 400
        
        image_ids = data['image_ids']
        if not isinstance(image_ids, list):
            return jsonify({'error': 'image_ids must be a list'}), 400
        
        deleted = storage.delete_images(session_id, image_ids)
        return jsonify({
            'status': 'ok',
            'deleted': deleted,
            'requested': len(image_ids)
        })
    
    @app.route('/api/sessions/<session_id>/images/<int:image_id>/delete', methods=['POST'])
    def api_session_image_delete(session_id: str, image_id: int):
        """Delete a single image"""
        if not storage:
            return jsonify({'error': 'Storage not available'}), 503
        
        if storage.delete_image(session_id, image_id):
            return jsonify({'status': 'ok', 'image_id': image_id})
        return jsonify({'error': 'Image not found or delete failed'}), 404
    
    # === SocketIO Events ===
    
    @socketio.on('connect')
    def handle_connect():
        logger.debug("Client connected")
        # Send initial status
        emit('status', {
            'time': time.time(),
            'receiver': receiver.get_stats() if receiver else {},
            'telemetry': telemetry.get_flight_stats() if telemetry else {},
            'decoder': decoder.get_status() if decoder else {},
        })
    
    @socketio.on('disconnect')
    def handle_disconnect():
        logger.debug("Client disconnected")
    
    return app, socketio


class WebServer:
    """Web server wrapper with background thread"""
    
    def __init__(
        self,
        config: 'GroundConfig',
        receiver: Optional['PacketReceiver'] = None,
        telemetry: Optional['TelemetryProcessor'] = None,
        decoder: Optional['FountainDecoder'] = None,
        storage: Optional['ImageStorage'] = None,
        ground_station: Optional[Any] = None,
    ):
        self.config = config
        self._ground_station = ground_station
        self._app, self._socketio = create_app(
            config, receiver, telemetry, decoder, storage, ground_station
        )
        self._thread: Optional[Thread] = None
        self._running = False
        
        # Ensure templates exist
        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        if not os.path.exists(template_dir):
            create_default_templates(template_dir)
    
    def start(self):
        """Start web server in background thread"""
        if self._running:
            return
        
        self._running = True
        self._thread = Thread(
            target=self._run_server,
            name="WebServer",
            daemon=True
        )
        self._thread.start()
        logger.info(f"Web server starting on port {self.config.web_port}")
    
    def stop(self):
        """Stop web server"""
        self._running = False
        logger.info("Web server stopped")
    
    def _run_server(self):
        """Run the Flask server"""
        try:
            self._socketio.run(
                self._app,
                host='0.0.0.0',
                port=self.config.web_port,
                debug=False,
                use_reloader=False,
                log_output=False
            )
        except Exception as e:
            logger.error(f"Web server error: {e}")
    
    def emit_telemetry(self, data: dict):
        """Emit telemetry update to all clients"""
        self._socketio.emit('telemetry', data)
    
    def emit_status(self, data: dict):
        """Emit status update to all clients"""
        self._socketio.emit('status', data)
    
    def emit_alert(self, alert_type: str, message: str, data: Any = None):
        """Emit alert to all clients"""
        self._socketio.emit('alert', {
            'type': alert_type,
            'message': message,
            'data': data,
            'time': time.time()
        })
    
    def emit_image_complete(self, image_id: int, metadata: dict):
        """Emit image complete notification"""
        self._socketio.emit('image_complete', {
            'image_id': image_id,
            **metadata
        })


def create_default_templates(template_dir: str):
    """Create default HTML templates"""
    os.makedirs(template_dir, exist_ok=True)
    
    # Base template (without commands link)
    base_html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}RaptorHab Ground Station{% endblock %}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css" rel="stylesheet">
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <style>
        :root {
            --primary-color: #0d6efd;
            --success-color: #198754;
            --danger-color: #dc3545;
            --warning-color: #ffc107;
        }
        body {
            background-color: #f8f9fa;
        }
        .navbar-brand { 
            font-weight: bold; 
            font-size: 1.4rem;
        }
        .status-card { 
            margin-bottom: 1rem;
            box-shadow: 0 0.125rem 0.25rem rgba(0, 0, 0, 0.075);
        }
        .telemetry-value { 
            font-size: 1.8rem; 
            font-weight: bold;
            color: #212529;
        }
        .telemetry-label { 
            color: #6c757d; 
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .telemetry-unit {
            font-size: 0.9rem;
            color: #6c757d;
        }
        #map { height: 500px; }
        .image-thumbnail { 
            max-width: 100%; 
            cursor: pointer;
            transition: transform 0.2s;
        }
        .image-thumbnail:hover {
            transform: scale(1.02);
        }
        .alert-badge { 
            animation: pulse 1s infinite; 
        }
        @keyframes pulse { 
            0%, 100% { opacity: 1; } 
            50% { opacity: 0.5; } 
        }
        .signal-bar {
            width: 100%;
            height: 8px;
            background-color: #e9ecef;
            border-radius: 4px;
            overflow: hidden;
        }
        .signal-bar-fill {
            height: 100%;
            transition: width 0.3s ease;
        }
        .signal-excellent { background-color: #198754; }
        .signal-good { background-color: #20c997; }
        .signal-fair { background-color: #ffc107; }
        .signal-poor { background-color: #dc3545; }
        .packet-counter {
            font-family: 'Courier New', monospace;
            font-size: 1.1rem;
        }
        .status-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            display: inline-block;
            margin-right: 6px;
        }
        .status-active { background-color: #198754; }
        .status-inactive { background-color: #dc3545; }
        .status-warning { background-color: #ffc107; }
        .log-container {
            height: 400px;
            overflow-y: auto;
            font-family: 'Courier New', monospace;
            font-size: 0.85rem;
            background-color: #1e1e1e;
            color: #d4d4d4;
            padding: 1rem;
            border-radius: 0.375rem;
        }
        .log-entry {
            margin-bottom: 2px;
        }
        .log-time { color: #569cd6; }
        .log-info { color: #4ec9b0; }
        .log-warn { color: #dcdcaa; }
        .log-error { color: #f14c4c; }
        .progress-ring {
            transform: rotate(-90deg);
        }
        .card-header {
            background-color: #fff;
            border-bottom: 1px solid rgba(0,0,0,.125);
        }
    </style>
    {% block extra_head %}{% endblock %}
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-primary shadow-sm">
        <div class="container-fluid">
            <a class="navbar-brand" href="/">
                <i class="bi bi-broadcast-pin"></i> RaptorHab Ground
            </a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav">
                    <li class="nav-item">
                        <a class="nav-link {% if request.path == '/' %}active{% endif %}" href="/">
                            <i class="bi bi-speedometer2"></i> Dashboard
                        </a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link {% if request.path == '/map' %}active{% endif %}" href="/map">
                            <i class="bi bi-map"></i> Map
                        </a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link {% if request.path == '/images' %}active{% endif %}" href="/images">
                            <i class="bi bi-images"></i> Images
                        </a>
                    </li>
                </ul>
                <ul class="navbar-nav ms-auto">
                    <li class="nav-item">
                        <span class="nav-link" id="connection-status">
                            <span class="status-indicator status-inactive"></span>
                            <span id="conn-text">Connecting...</span>
                        </span>
                    </li>
                </ul>
            </div>
        </div>
    </nav>

    <div class="container-fluid mt-3">
        <div id="alert-container"></div>
        {% block content %}{% endblock %}
    </div>

    <footer class="mt-4 py-3 bg-light border-top">
        <div class="container text-center text-muted">
            <small>RaptorHab Ground Station v1.0 (Receive-Only) | 
            <span id="footer-time"></span> | 
            Uptime: <span id="footer-uptime">--</span></small>
        </div>
    </footer>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        // WebSocket connection
        const socket = io();
        let startTime = Date.now();
        
        socket.on('connect', function() {
            document.getElementById('connection-status').innerHTML = 
                '<span class="status-indicator status-active"></span>' +
                '<span id="conn-text">Connected</span>';
            console.log('WebSocket connected');
        });
        
        socket.on('disconnect', function() {
            document.getElementById('connection-status').innerHTML = 
                '<span class="status-indicator status-inactive"></span>' +
                '<span id="conn-text">Disconnected</span>';
            console.log('WebSocket disconnected');
        });
        
        socket.on('alert', function(data) {
            showAlert(data.type, data.message);
        });
        
        function showAlert(type, message) {
            const alertDiv = document.createElement('div');
            alertDiv.className = 'alert alert-warning alert-dismissible fade show';
            alertDiv.innerHTML = `
                <i class="bi bi-exclamation-triangle-fill me-2"></i>
                <strong>${type}:</strong> ${message}
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            `;
            document.getElementById('alert-container').prepend(alertDiv);
            
            // Auto-dismiss after 30 seconds
            setTimeout(() => {
                alertDiv.remove();
            }, 30000);
        }
        
        function formatTime(timestamp) {
            if (!timestamp) return '--';
            const date = new Date(timestamp * 1000);
            return date.toLocaleTimeString();
        }
        
        function formatUptime(seconds) {
            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = Math.floor(seconds % 60);
            return `${h}h ${m}m ${s}s`;
        }
        
        // Update footer time
        setInterval(function() {
            document.getElementById('footer-time').textContent = new Date().toLocaleString();
        }, 1000);
    </script>
    {% block extra_scripts %}{% endblock %}
</body>
</html>'''
    
    with open(os.path.join(template_dir, 'base.html'), 'w') as f:
        f.write(base_html)
    
    # Map template
    map_html = '''{% extends "base.html" %}
{% block title %}Map - RaptorHab Ground Station{% endblock %}
{% block extra_head %}
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
{% endblock %}

{% block content %}
<div class="card">
    <div class="card-header d-flex justify-content-between align-items-center">
        <h5><i class="bi bi-map-fill"></i> Flight Track</h5>
        <div>
            <button class="btn btn-sm btn-outline-secondary" onclick="centerOnPayload()">
                <i class="bi bi-geo-alt"></i> Center
            </button>
            <button class="btn btn-sm btn-outline-secondary" onclick="fitTrack()">
                <i class="bi bi-arrows-fullscreen"></i> Fit Track
            </button>
        </div>
    </div>
    <div class="card-body p-0">
        <div id="map" style="height: 600px;"></div>
    </div>
</div>
{% endblock %}

{% block extra_scripts %}
<script>
    const map = L.map('map').setView([40.0, -74.0], 10);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors'
    }).addTo(map);
    
    let marker = null;
    let track = L.polyline([], {color: 'red', weight: 3}).addTo(map);
    let trackPoints = [];
    
    function centerOnPayload() {
        if (marker) {
            map.setView(marker.getLatLng(), map.getZoom());
        }
    }
    
    function fitTrack() {
        if (trackPoints.length > 0) {
            map.fitBounds(track.getBounds(), {padding: [50, 50]});
        }
    }
    
    socket.on('telemetry', function(data) {
        if (data.latitude && data.longitude) {
            const pos = [data.latitude, data.longitude];
            
            if (!marker) {
                marker = L.marker(pos).addTo(map);
                map.setView(pos, 12);
            } else {
                marker.setLatLng(pos);
            }
            
            marker.bindPopup(`Alt: ${data.altitude.toFixed(0)}m<br>Speed: ${data.speed.toFixed(1)}m/s`);
            
            trackPoints.push(pos);
            track.setLatLngs(trackPoints);
        }
    });
    
    // Load existing track
    fetch('/api/telemetry/track')
        .then(r => r.json())
        .then(data => {
            trackPoints = data.map(p => [p.lat, p.lon]);
            track.setLatLngs(trackPoints);
            if (trackPoints.length > 0) {
                map.fitBounds(track.getBounds());
            }
        });
</script>
{% endblock %}'''
    
    with open(os.path.join(template_dir, 'map.html'), 'w') as f:
        f.write(map_html)
    
    # Images template
    images_html = '''{% extends "base.html" %}
{% block content %}
<div class="card">
    <div class="card-header d-flex justify-content-between align-items-center">
        <h5><i class="bi bi-images"></i> Received Images</h5>
        <button class="btn btn-primary btn-sm" onclick="loadImages()">
            <i class="bi bi-arrow-clockwise"></i> Refresh
        </button>
    </div>
    <div class="card-body">
        <div class="row" id="image-gallery"></div>
    </div>
</div>

<div class="modal fade" id="imageModal" tabindex="-1">
    <div class="modal-dialog modal-lg">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">Image Viewer</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body text-center">
                <img id="modal-image" class="img-fluid" src="">
            </div>
        </div>
    </div>
</div>
{% endblock %}

{% block extra_scripts %}
<script>
    function loadImages() {
        fetch('/api/images?count=50')
            .then(r => r.json())
            .then(images => {
                const gallery = document.getElementById('image-gallery');
                gallery.innerHTML = '';
                
                images.forEach(img => {
                    const col = document.createElement('div');
                    col.className = 'col-md-3 mb-3';
                    col.innerHTML = `
                        <div class="card">
                            <img src="/api/images/${img.image_id}/thumbnail" 
                                 class="card-img-top image-thumbnail"
                                 onclick="showImage(${img.image_id})">
                            <div class="card-body p-2">
                                <small>ID: ${img.image_id}<br>
                                ${img.width}x${img.height}<br>
                                ${(img.size_bytes/1024).toFixed(1)} KB</small>
                            </div>
                        </div>
                    `;
                    gallery.appendChild(col);
                });
            });
    }
    
    function showImage(imageId) {
        document.getElementById('modal-image').src = '/api/images/' + imageId;
        new bootstrap.Modal(document.getElementById('imageModal')).show();
    }
    
    socket.on('image_complete', function(data) {
        loadImages();
    });
    
    loadImages();
</script>
{% endblock %}'''
    
    with open(os.path.join(template_dir, 'images.html'), 'w') as f:
        f.write(images_html)
    
    logger.info(f"Created default templates in {template_dir}")


# Initialize templates on import
_template_dir = os.path.join(os.path.dirname(__file__), 'templates')
if not os.path.exists(_template_dir):
    create_default_templates(_template_dir)
