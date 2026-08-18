"""
The public Meshtastic MQTT network, as a last-resort position source.

When the balloon is beyond every radio you own, somebody else's node may still
hear it and gateway the packet to an MQTT broker. That is a real recovery path
for a balloon that drifts a few hundred miles, and it costs nothing but an
internet connection.

MQTT 3.1.1 is implemented directly rather than pulled in as a dependency: the
subset needed to connect, subscribe and receive QoS 0 publishes is small, and
the project's ground stations deliberately run on the standard library.

Positions from here are the lowest-priority source. A gateway can be minutes
behind, and the packet has been relayed by strangers -- useful for pointing a
search in the right direction, not for steering a chase car.
"""

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_HOST = "mqtt.meshtastic.org"
DEFAULT_PORT = 1883
DEFAULT_USER = "meshdev"
DEFAULT_PASSWORD = "large4cats"
DEFAULT_TOPIC = "msh/+/+/json/#"


@dataclass
class MQTTConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    username: str = DEFAULT_USER
    password: str = DEFAULT_PASSWORD
    topic: str = DEFAULT_TOPIC
    client_id: str = "raptorhabgs"


def _encode_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


def _encode_string(text: str) -> bytes:
    raw = text.encode()
    return len(raw).to_bytes(2, "big") + raw


class MeshtasticMQTTClient:
    """Subscribes to the JSON feed and forwards balloon positions."""

    def __init__(self, config: Optional[MQTTConfig] = None):
        self.config = config or MQTTConfig()
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._buffer = bytearray()

        self.balloon_node_id: Optional[int] = None
        self.state = "disconnected"
        self.messages_received = 0
        self.positions_forwarded = 0
        self.last_message_at: Optional[float] = None
        self.last_error: Optional[str] = None

        self.on_position: Optional[Callable[[dict], None]] = None
        self.on_state: Optional[Callable[[str], None]] = None

    # -- connection --------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    def connect(self) -> None:
        self.disconnect()
        self._set_state("connecting")
        try:
            self._socket = socket.create_connection(
                (self.config.host, self.config.port), timeout=10)
            self._socket.settimeout(1.0)
        except Exception as exc:
            self.last_error = str(exc)
            self._set_state("failed")
            raise

        self._send_connect()
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True,
                                        name="meshtastic-mqtt")
        self._thread.start()

    def disconnect(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive() and \
                threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
        self._socket = None
        self._buffer.clear()
        self._set_state("disconnected")

    def _set_state(self, state: str) -> None:
        self.state = state
        if self.on_state:
            self.on_state(state)

    def _send(self, data: bytes) -> None:
        if self._socket:
            self._socket.sendall(data)

    def _send_connect(self) -> None:
        payload = (_encode_string("MQTT") + bytes([0x04]) +
                   bytes([0xC2]) +               # username + password flags
                   (60).to_bytes(2, "big") +     # keepalive
                   _encode_string(f"{self.config.client_id}-{int(time.time()) % 100000}") +
                   _encode_string(self.config.username) +
                   _encode_string(self.config.password))
        self._send(bytes([0x10]) + _encode_length(len(payload)) + payload)

    def _send_subscribe(self) -> None:
        payload = ((1).to_bytes(2, "big") +
                   _encode_string(self.config.topic) + bytes([0x00]))
        self._send(bytes([0x82]) + _encode_length(len(payload)) + payload)

    def _send_ping(self) -> None:
        self._send(bytes([0xC0, 0x00]))

    # -- receive -----------------------------------------------------------

    def _read_loop(self) -> None:
        last_ping = time.time()
        while self._running and self._socket:
            try:
                chunk = self._socket.recv(8192)
                if not chunk:
                    raise ConnectionError("broker closed the connection")
                self._buffer.extend(chunk)
            except socket.timeout:
                pass
            except Exception as exc:
                self.last_error = str(exc)
                self._set_state("disconnected")
                self._running = False
                return

            while True:
                packet = self._next_packet()
                if packet is None:
                    break
                self._handle_packet(*packet)

            if time.time() - last_ping > 30:
                last_ping = time.time()
                try:
                    self._send_ping()
                except Exception:
                    pass

    def _next_packet(self):
        """Decode one packet, or None if the buffer holds an incomplete one."""
        if len(self._buffer) < 2:
            return None
        packet_type = self._buffer[0]

        length = 0
        multiplier = 1
        index = 1
        while True:
            if index >= len(self._buffer):
                return None
            if index > 4:
                # Malformed remaining-length; drop a byte and resynchronise
                # rather than spinning on it forever.
                del self._buffer[:1]
                return None
            byte = self._buffer[index]
            length += (byte & 0x7F) * multiplier
            multiplier *= 128
            index += 1
            if not byte & 0x80:
                break

        total = index + length
        if len(self._buffer) < total:
            return None

        body = bytes(self._buffer[index:total])
        del self._buffer[:total]
        return packet_type, body

    def _handle_packet(self, packet_type: int, body: bytes) -> None:
        kind = packet_type & 0xF0
        if kind == 0x20:                       # CONNACK
            if len(body) >= 2 and body[1] == 0:
                self._set_state("connected")
                self._send_subscribe()
            else:
                code = body[1] if len(body) >= 2 else -1
                self.last_error = f"broker refused the connection (code {code})"
                self._set_state("failed")
        elif kind == 0x30:                     # PUBLISH
            self._handle_publish(body)

    def _handle_publish(self, payload: bytes) -> None:
        if len(payload) < 2:
            return
        topic_length = (payload[0] << 8) | payload[1]
        if len(payload) < 2 + topic_length:
            return
        message = payload[2 + topic_length:]

        try:
            envelope = json.loads(message.decode("utf-8", "replace"))
        except Exception:
            return

        self.messages_received += 1
        self.last_message_at = time.time()

        if envelope.get("type") != "position":
            return
        body = envelope.get("payload")
        if not isinstance(body, dict):
            return

        sender = envelope.get("from")
        if self.balloon_node_id is not None and sender != self.balloon_node_id:
            return

        latitude_i = body.get("latitude_i")
        longitude_i = body.get("longitude_i")
        if latitude_i is None or longitude_i is None:
            return

        self.positions_forwarded += 1
        if self.on_position:
            self.on_position({
                "latitude": latitude_i / 1e7,
                "longitude": longitude_i / 1e7,
                "altitude": float(body.get("altitude") or 0.0),
                "satellites": body.get("sats_in_view"),
                "rssi": envelope.get("rssi"),
                "snr": envelope.get("snr"),
                "timestamp": envelope.get("timestamp"),
                "detail": f"via {envelope.get('sender') or 'MQTT gateway'}",
            })

    def status(self) -> dict:
        return {
            "state": self.state,
            "host": self.config.host,
            "topic": self.config.topic,
            "messages_received": self.messages_received,
            "positions_forwarded": self.positions_forwarded,
            "last_message_at": self.last_message_at,
            "balloon_node_id": self.balloon_node_id,
            "last_error": self.last_error,
        }
