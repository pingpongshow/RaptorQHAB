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
from ..core.payload_link import PayloadLink, discover_payload_ports
from ..core.meshtastic_manager import (
    MeshtasticManager, ChannelConfig, discover_meshtastic_ports)
from ..core.meshtastic_mqtt import MeshtasticMQTTClient
from ..core.position_fusion import PositionFusion, PositionSource
from ..core.meshtastic import channel_hash as meshtastic_channel_hash
from ..core.offline_maps import OfflineMapManager
from ..core.audio_alerts import AudioAlertManager, AlertType
from ..core.sd_import import (
    candidate_cards, survey_card, import_files, load_private_key, read_image,
    read_telemetry, DEFAULT_KEY_PATH)

logger = logging.getLogger(__name__)

# Reduce Flask/Werkzeug logging noise
log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)


class WebServer:
    """Flask-based web server for remote GUI access."""
    
    def __init__(self, host: str = "127.0.0.1", port: int = 5000):
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

        # USB link to the balloon: configuration and terminal.
        # Deliberately separate from the RF ground station -- the
        # payload accepts settings over USB only, never over radio.
        self.payload = PayloadLink()

        # Second receive path: a stock Meshtastic node on USB, plus the public
        # MQTT network. Both feed the same fusion, which decides what the map
        # actually draws.
        self.mesh = MeshtasticManager()
        self.mqtt = MeshtasticMQTTClient()
        self.fusion = PositionFusion()
        self.packet_log = []
        self.max_packet_log = 500
        self._wire_position_sources()

        # Offline tiles and audible alerts. Both matter most in the field,
        # where nobody is looking at this browser tab.
        self.offline_maps = OfflineMapManager(get_data_directory() / "tiles.mbtiles")
        self.audio_alerts = AudioAlertManager()

        # A recovered card holds every image at full quality, not just the
        # handful that fit in the airtime budget.
        self.card_survey = None
        
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
    




        # ---- recovered SD card ---------------------------------------------

        @self.app.route('/api/card/scan')
        def card_scan():
            return jsonify({"cards": candidate_cards(),
                            "key_path": str(DEFAULT_KEY_PATH),
                            "have_key": load_private_key() is not None})

        @self.app.route('/api/card/survey', methods=['POST'])
        def card_survey_route():
            data = request.get_json(silent=True) or {}
            root = data.get("path")
            if not root:
                return jsonify({"ok": False, "error": "no path given"}), 400
            key = data.get("key_path")
            try:
                survey = survey_card(root, key_path=key or None)
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500
            self.card_survey = survey
            result = survey.as_dict()
            result["files"] = {
                "images": [f.as_dict() for f in survey.images[:2000]],
                "telemetry": [f.as_dict() for f in survey.telemetry],
                "logs": [f.as_dict() for f in survey.logs],
            }
            return jsonify({"ok": True, "survey": result})

        @self.app.route('/api/card/thumb')
        def card_thumb():
            """One image, unsealed on the fly, without importing anything."""
            name = request.args.get("name")
            if not self.card_survey or not name:
                return Response(status=404)
            entry = next((f for f in self.card_survey.images if f.name == name), None)
            if entry is None:
                return Response(status=404)
            data = read_image(entry, load_private_key())
            if data is None:
                return Response(status=409)     # sealed and we hold no key
            return Response(data, mimetype="image/webp",
                            headers={"Cache-Control": "max-age=300"})

        @self.app.route('/api/card/telemetry')
        def card_telemetry():
            name = request.args.get("name")
            if not self.card_survey or not name:
                return jsonify({"ok": False, "error": "no card surveyed"}), 404
            entry = next((f for f in self.card_survey.telemetry if f.name == name), None)
            if entry is None:
                return jsonify({"ok": False, "error": "not found"}), 404
            rows = read_telemetry(entry, load_private_key())
            return jsonify({"ok": True, "rows": rows[:2000], "count": len(rows)})

        @self.app.route('/api/card/import', methods=['POST'])
        def card_import():
            if not self.card_survey:
                return jsonify({"ok": False, "error": "survey a card first"}), 400
            data = request.get_json(silent=True) or {}
            kinds = set(data.get("kinds") or ["images", "telemetry", "logs"])
            # Confined to the data directory. This used to take any path the
            # caller asked for, which -- with the server bound to every
            # interface, as it was by default -- let anyone on the network
            # write decrypted flight data anywhere the process could reach.
            recovered_root = (get_data_directory() / "recovered").resolve()
            requested = data.get("output")
            if requested:
                candidate = (recovered_root / str(requested).lstrip("/")).resolve()
                if not candidate.is_relative_to(recovered_root):
                    return jsonify({
                        "ok": False,
                        "error": "output must stay inside the recovered folder",
                    }), 400
                output = str(candidate)
            else:
                output = str(recovered_root / (self.card_survey.callsign or "payload"))

            selected = []
            if "images" in kinds:    selected += self.card_survey.images
            if "telemetry" in kinds: selected += self.card_survey.telemetry
            if "logs" in kinds:      selected += self.card_survey.logs
            if not selected:
                return jsonify({"ok": False, "error": "nothing selected"}), 400

            result = import_files(selected, output,
                                  private_key=load_private_key(),
                                  overwrite=bool(data.get("overwrite")))
            return jsonify({"ok": True, "result": result.as_dict()})

        # ---- offline maps and audio alerts ---------------------------------

        @self.app.route('/api/maps/status')
        def maps_status():
            return jsonify(self.offline_maps.status())

        @self.app.route('/api/maps/estimate', methods=['POST'])
        def maps_estimate():
            d = request.get_json(silent=True) or {}
            try:
                return jsonify({"ok": True, "estimate": self.offline_maps.estimate(
                    float(d.get("latitude", 0)), float(d.get("longitude", 0)),
                    float(d.get("radius_km", 25)), int(d.get("min_zoom", 8)),
                    int(d.get("max_zoom", 13)))})
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400

        @self.app.route('/api/maps/download', methods=['POST'])
        def maps_download():
            d = request.get_json(silent=True) or {}
            try:
                estimate = self.offline_maps.download_region(
                    float(d.get("latitude", 0)), float(d.get("longitude", 0)),
                    float(d.get("radius_km", 25)), int(d.get("min_zoom", 8)),
                    int(d.get("max_zoom", 13)),
                    acknowledge_large=bool(d.get("acknowledge_large")))
            except (ValueError, RuntimeError) as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            return jsonify({"ok": True, "estimate": estimate})

        @self.app.route('/api/maps/cancel', methods=['POST'])
        def maps_cancel():
            self.offline_maps.cancel()
            return jsonify({"ok": True})

        @self.app.route('/api/maps/clear', methods=['POST'])
        def maps_clear():
            self.offline_maps.cache.clear()
            return jsonify({"ok": True})

        @self.app.route('/api/maps/tile/<int:z>/<int:x>/<int:y>.png')
        def maps_tile(z, x, y):
            """
            Serve a tile, cache first.

            Pointing the map at this rather than straight at OpenStreetMap is
            what makes the browser work with no internet at all: in the field
            the cache is the only source, and a miss simply draws nothing.
            """
            data = self.offline_maps.get_tile(z, x, y)
            if data is None:
                return Response(status=404)
            return Response(data, mimetype="image/png",
                            headers={"Cache-Control": "max-age=86400"})

        @self.app.route('/api/alerts/status')
        def alerts_status():
            return jsonify(self.audio_alerts.status())

        @self.app.route('/api/alerts/config', methods=['POST'])
        def alerts_config():
            d = request.get_json(silent=True) or {}
            config = self.audio_alerts.config
            if "enabled" in d:
                config.enabled = bool(d["enabled"])
            if "volume" in d:
                config.volume = max(0.0, min(1.0, float(d["volume"])))
            if "speak" in d:
                config.speak = bool(d["speak"])
            if "signal_lost_after_sec" in d:
                config.signal_lost_after_sec = float(d["signal_lost_after_sec"])
            if "low_battery_mv" in d:
                config.low_battery_mv = int(d["low_battery_mv"])
            for name, enabled in (d.get("per_alert") or {}).items():
                config.per_alert[name] = bool(enabled)
            return jsonify({"ok": True, "status": self.audio_alerts.status()})

        @self.app.route('/api/alerts/test', methods=['POST'])
        def alerts_test():
            name = (request.get_json(silent=True) or {}).get("alert", "BURST")
            try:
                alert = AlertType[name]
            except KeyError:
                return jsonify({"ok": False, "error": f"unknown alert {name}"}), 400
            self.audio_alerts.player.play(alert, self.audio_alerts.config.volume)
            return jsonify({"ok": True})

        # ---- Meshtastic node, MQTT, fusion, packet log ---------------------

        @self.app.route('/api/mesh/ports')
        def mesh_ports():
            return jsonify({"ports": discover_meshtastic_ports(),
                            "connected": self.mesh.connected,
                            "port": self.mesh.port})

        @self.app.route('/api/mesh/connect', methods=['POST'])
        def mesh_connect():
            data = request.get_json(silent=True) or {}
            device = data.get("port")
            if not device:
                found = discover_meshtastic_ports()
                if not found:
                    return jsonify({"ok": False, "error": "no Meshtastic node found"}), 404
                device = found[0]["device"]
            try:
                self.mesh.connect(device)
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500
            return jsonify({"ok": True, "status": self.mesh.status()})

        @self.app.route('/api/mesh/disconnect', methods=['POST'])
        def mesh_disconnect():
            self.mesh.disconnect()
            return jsonify({"ok": True})

        @self.app.route('/api/mesh/status')
        def mesh_status():
            return jsonify({
                "mesh": self.mesh.status(),
                "mqtt": self.mqtt.status(),
                "nodes": [
                    {"id": n.node_id, "name": n.display_name,
                     "short": n.short_name, "last_heard": n.last_heard,
                     "snr": n.snr, "rssi": n.rssi,
                     "battery": n.battery_percent,
                     "latitude": n.latitude, "longitude": n.longitude,
                     "is_balloon": n.node_id == self.mesh.balloon_node_id}
                    for n in sorted(self.mesh.nodes.values(),
                                    key=lambda x: x.last_heard, reverse=True)
                ],
                "messages": [
                    {"timestamp": m.timestamp, "sender_name": m.sender_name,
                     "text": m.text, "outgoing": m.outgoing,
                     "rssi": m.rssi, "snr": m.snr}
                    for m in self.mesh.messages[-100:]
                ],
            })

        @self.app.route('/api/mesh/channels', methods=['GET', 'POST'])
        def mesh_channels():
            if request.method == 'GET':
                return jsonify({"channels": [
                    {"name": c.name, "hash": c.hash} for c in self.mesh.channels]})
            data = request.get_json(silent=True) or {}
            import base64
            channels = []
            for entry in data.get("channels", []):
                try:
                    key = base64.b64decode(entry.get("key", ""))
                except Exception:
                    return jsonify({"ok": False,
                                    "error": f"channel {entry.get('name')}: key is not base64"}), 400
                name = entry.get("name") or "LongFast"
                channels.append(ChannelConfig(
                    name=name, key=key, hash=meshtastic_channel_hash(name, key)))
            self.mesh.channels = channels
            return jsonify({"ok": True, "channels": [
                {"name": c.name, "hash": c.hash} for c in channels]})

        @self.app.route('/api/mesh/balloon', methods=['POST'])
        def mesh_balloon():
            data = request.get_json(silent=True) or {}
            node = data.get("node_id")
            if isinstance(node, str):
                node = int(node.lstrip("!"), 16)
            self.mesh.balloon_node_id = node
            self.mqtt.balloon_node_id = node
            return jsonify({"ok": True, "balloon_node_id": node})

        @self.app.route('/api/mesh/send', methods=['POST'])
        def mesh_send():
            data = request.get_json(silent=True) or {}
            text = (data.get("text") or "").strip()
            if not text:
                return jsonify({"ok": False, "error": "no text"}), 400
            if not self.mesh.channels:
                return jsonify({"ok": False, "error": "no channels configured"}), 400

            name = data.get("channel")
            channel = next((c for c in self.mesh.channels if c.name == name),
                           self.mesh.channels[0])
            try:
                if data.get("as_command"):
                    message = self.mesh.send_command_to_balloon(text, channel)
                elif data.get("to_balloon"):
                    if self.mesh.balloon_node_id is None:
                        return jsonify({"ok": False,
                                        "error": "the balloon's node id is not known yet"}), 400
                    message = self.mesh.send_text(
                        text, channel, destination=self.mesh.balloon_node_id)
                else:
                    message = self.mesh.send_text(text, channel)
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            return jsonify({"ok": True, "sent": message.text})

        @self.app.route('/api/mqtt/connect', methods=['POST'])
        def mqtt_connect():
            try:
                self.mqtt.connect()
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500
            return jsonify({"ok": True, "status": self.mqtt.status()})

        @self.app.route('/api/mqtt/disconnect', methods=['POST'])
        def mqtt_disconnect():
            self.mqtt.disconnect()
            return jsonify({"ok": True})

        @self.app.route('/api/position/sources')
        def position_sources():
            return jsonify(self.fusion.status())

        @self.app.route('/api/position/track')
        def position_track():
            return jsonify({"track": self.fusion.track()})

        @self.app.route('/api/position/extrapolation', methods=['POST'])
        def position_extrapolation():
            data = request.get_json(silent=True) or {}
            self.fusion.extrapolation_enabled = bool(data.get("enabled", True))
            return jsonify({"ok": True,
                            "enabled": self.fusion.extrapolation_enabled})

        @self.app.route('/api/packets')
        def packets():
            return jsonify({"packets": self.packet_log[-200:]})

        @self.app.route('/api/packets/clear', methods=['POST'])
        def packets_clear():
            self.packet_log.clear()
            return jsonify({"ok": True})

        # ---- payload USB link: configuration and console -------------------

        @self.app.route('/api/payload/ports')
        def payload_ports():
            return jsonify({"ports": [
                {"device": p.device, "description": p.description,
                 "confident": p.confident} for p in discover_payload_ports()
            ], "connected": self.payload.connected, "port": self.payload.port})

        @self.app.route('/api/payload/connect', methods=['POST'])
        def payload_connect():
            data = request.get_json(silent=True) or {}
            device = data.get("port")
            if not device:
                found = discover_payload_ports()
                if not found:
                    return jsonify({"ok": False, "error": "no payload port found"}), 404
                device = found[0].device
            try:
                identity = self.payload.connect(device)
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500
            self.payload.on_console = self._emit_console
            return jsonify({"ok": True, "port": device, "identity": identity})

        @self.app.route('/api/payload/disconnect', methods=['POST'])
        def payload_disconnect():
            self.payload.disconnect()
            return jsonify({"ok": True})

        @self.app.route('/api/payload/schema')
        def payload_schema():
            try:
                return jsonify({"ok": True, "schema": self.payload.get_schema()})
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500

        @self.app.route('/api/payload/config', methods=['GET', 'POST'])
        def payload_config():
            try:
                if request.method == 'GET':
                    return jsonify({"ok": True, "config": self.payload.get_config()})
                values = (request.get_json(silent=True) or {}).get("values", {})
                if not values:
                    return jsonify({"ok": False, "error": "no values supplied"}), 400
                return jsonify({"ok": True, "result": self.payload.set_config(values)})
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500

        @self.app.route('/api/payload/status')
        def payload_status():
            try:
                return jsonify({"ok": True, "status": self.payload.get_status()})
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500

        @self.app.route('/api/payload/logs')
        def payload_logs():
            try:
                lines = int(request.args.get("lines", 100))
                return jsonify({"ok": True, "logs": self.payload.get_logs(lines)})
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500

        @self.app.route('/api/payload/restart', methods=['POST'])
        def payload_restart():
            try:
                return jsonify({"ok": True, "result": self.payload.restart_service()})
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500

        @self.app.route('/api/payload/images')
        def payload_images():
            try:
                limit = int(request.args.get("limit", 50))
                return jsonify({"ok": True, "result": self.payload.list_images(limit)})
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500

        @self.app.route('/api/payload/psk', methods=['POST'])
        def payload_psk():
            try:
                bits = int((request.get_json(silent=True) or {}).get("bits", 256))
                return jsonify({"ok": True, "result": self.payload.generate_psk(bits)})
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500


    def _wire_position_sources(self):
        """Point every receive path at the one fusion."""
        def mesh_position(node_id, position):
            self.fusion.submit_meshtastic(
                position["latitude"], position["longitude"], position["altitude"],
                detail=position.get("name") or "Meshtastic node",
                timestamp=position.get("timestamp"),
                satellites=position.get("satellites"),
                rssi=position.get("rssi"), snr=position.get("snr"))
            self.socketio.emit('position_update', self.fusion.status())

        def mesh_message(message):
            self.socketio.emit('mesh_message', {
                "timestamp": message.timestamp, "sender": message.sender,
                "sender_name": message.sender_name, "text": message.text,
                "outgoing": message.outgoing, "rssi": message.rssi,
                "snr": message.snr,
            })

        def mqtt_position(position):
            self.fusion.submit_meshtastic(
                position["latitude"], position["longitude"], position["altitude"],
                detail=position.get("detail") or "MQTT gateway",
                timestamp=position.get("timestamp"),
                satellites=position.get("satellites"),
                rssi=position.get("rssi"), snr=position.get("snr"),
                via_mqtt=True)
            self.socketio.emit('position_update', self.fusion.status())

        self.mesh.on_position = mesh_position
        self.mesh.on_message = mesh_message
        self.mqtt.on_position = mqtt_position

    def _record_packet(self, entry):
        self.packet_log.append(entry)
        if len(self.packet_log) > self.max_packet_log:
            del self.packet_log[:len(self.packet_log) - self.max_packet_log]
        self.socketio.emit('packet', entry)

    def _emit_console(self, data: bytes):
        """Forward payload terminal output to every connected browser."""
        self.socketio.emit('console_output',
                           {"data": data.decode("utf-8", "replace")})

    def _setup_socketio_events(self):
        """Setup SocketIO event handlers."""
        
        @self.socketio.on('connect')
        def handle_connect():
            logger.debug(f"[WebServer] Client connected")
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
        

        @self.socketio.on('console_start')
        def on_console_start(data=None):
            data = data or {}
            try:
                self.payload.shell_start(int(data.get("rows", 24)),
                                         int(data.get("cols", 100)))
                emit('console_status', {"running": True})
            except Exception as exc:
                emit('console_status', {"running": False, "error": str(exc)})

        @self.socketio.on('console_input')
        def on_console_input(data):
            try:
                self.payload.console_write((data or {}).get("data", "").encode())
            except Exception as exc:
                emit('console_status', {"running": False, "error": str(exc)})

        @self.socketio.on('console_stop')
        def on_console_stop(data=None):
            try:
                self.payload.shell_stop()
            except Exception:
                pass
            emit('console_status', {"running": False})

        @self.socketio.on('disconnect')
        def handle_disconnect():
            logger.debug(f"[WebServer] Client disconnected")
    
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
        logger.debug(f"\n{'='*60}")
        logger.debug(f"  RaptorHabGS Web Server")
        logger.debug(f"  Running on http://{self.host}:{self.port}")
        logger.debug(f"{'='*60}\n")

        # Binding beyond loopback puts thirty state-changing endpoints on the
        # network with no authentication in front of them: stopping the
        # receiver, rewriting the radio configuration, deleting recorded
        # flights. Useful on purpose -- a phone on the same network is a good
        # second screen at a launch site -- but it should never happen by
        # accident, which it did when this defaulted to 0.0.0.0.
        if not self._is_loopback(self.host):
            logger.debug("  !! This is reachable from the whole network and has no")
            logger.debug("  !! authentication. Anyone who can open the page can stop")
            logger.debug("  !! the ground station, change its configuration and")
            logger.debug("  !! delete recorded flights. Use --host 127.0.0.1 unless")
            logger.debug("  !! you are on a network you trust.\n")
        self.socketio.run(
            self.app, 
            host=self.host, 
            port=self.port, 
            debug=debug,
            allow_unsafe_werkzeug=True
        )
    
    @staticmethod
    def _is_loopback(host: str) -> bool:
        import ipaddress
        if host in ("localhost", ""):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def shutdown(self):
        """Shutdown the server and managers."""
        logger.debug("[WebServer] Shutting down...")
        
        # Stop mission if recording
        if self.mission_manager.is_recording:
            self.mission_manager.stop_recording(save=True)
        
        if self.ground_station.is_receiving:
            self.ground_station.stop_receiving()
        if self.gps_manager.is_connected:
            self.gps_manager.disconnect()
