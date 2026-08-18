"""
USB link to the airborne payload.

The macOS companion app can configure the balloon and open a terminal on it
over the Pi's USB port. This is the same capability for the Python ground
station, so a laptop in the field -- or the Pi running the ground station
itself -- can do it without a Mac.

Configuration is USB-only by design. The payload will not accept settings over
the radio, so there is no path here that touches the RF link.

The transport is the payload's own frame format (see linkproto.py): one serial
line carrying a JSON RPC channel and a raw terminal channel side by side. The
payload is the authority on what is configurable -- this asks it for a schema
rather than hardcoding a parameter list, so the two cannot drift apart.
"""

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import serial
import serial.tools.list_ports

from .linkproto import Channel, FrameDecoder, encode_frame

# The payload's USB gadget identifies itself with these. Matching on them
# rather than on a substring of the device name avoids grabbing a Meshtastic
# node or the Heltec modem, which sit on the same bus.
PAYLOAD_VID = 0x1D6B
PAYLOAD_PID = 0x0104
PAYLOAD_PRODUCT_HINT = "raptorhab"

DEFAULT_BAUD = 115200
RPC_TIMEOUT_SEC = 10.0


@dataclass
class PayloadPort:
    """A serial port that looks like a payload."""
    device: str
    description: str
    confident: bool          # matched VID/PID, not just a name

    @property
    def label(self) -> str:
        return f"{self.device} ({self.description})"


def discover_payload_ports() -> List[PayloadPort]:
    """
    Find candidate payload ports, most likely first.

    Confident matches come from the USB gadget's VID/PID. Name matches are
    offered as a fallback because a payload reached over a plain USB-serial
    adapter, or a port renamed by the OS, is still worth showing.
    """
    confident: List[PayloadPort] = []
    possible: List[PayloadPort] = []

    for port in serial.tools.list_ports.comports():
        desc = port.description or ""
        product = (getattr(port, "product", None) or "")
        if port.vid == PAYLOAD_VID and port.pid == PAYLOAD_PID:
            confident.append(PayloadPort(port.device, desc or product, True))
        elif PAYLOAD_PRODUCT_HINT in (product + desc).lower():
            confident.append(PayloadPort(port.device, desc or product, True))
        elif "usbmodem" in port.device or "ttyACM" in port.device:
            possible.append(PayloadPort(port.device, desc or "unidentified", False))

    return confident + possible


class PayloadLink:
    """
    A connection to the payload's USB console.

    Thread-safe for RPC: a single reader thread demultiplexes the channels and
    hands control replies back to whichever caller is waiting. Console output
    is delivered by callback because it is unsolicited.
    """

    def __init__(self):
        self._serial: Optional[serial.Serial] = None
        self._decoder = FrameDecoder()
        self._reader: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()          # serialises RPC calls
        self._write_lock = threading.Lock()    # serialises port writes

        self._next_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._replies = threading.Condition()

        self.on_console: Optional[Callable[[bytes], None]] = None
        self.on_event: Optional[Callable[[dict], None]] = None
        self.on_disconnect: Optional[Callable[[str], None]] = None

        self.port: Optional[str] = None
        self.identity: Dict[str, Any] = {}

    # -- connection --------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def connect(self, port: str, baud: int = DEFAULT_BAUD) -> Dict[str, Any]:
        """
        Open the link and identify the payload.

        Returns the payload's hello response, which carries its callsign and
        firmware version -- proof that the thing on the other end is a payload
        and not, say, a Meshtastic node on a neighbouring port.
        """
        self.disconnect()

        self._serial = serial.Serial(port, baud, timeout=0.2)
        self.port = port
        self._decoder = FrameDecoder()
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="payload-link")
        self._reader.start()

        # Give the gadget a moment; connecting mid-stream is normal and the
        # decoder resynchronises, but the first RPC should not race the open.
        time.sleep(0.3)
        try:
            self.identity = self.rpc("hello")
        except Exception:
            self.disconnect()
            raise
        return self.identity

    def disconnect(self) -> None:
        self._running = False
        if self._reader and self._reader.is_alive() and \
                threading.current_thread() is not self._reader:
            self._reader.join(timeout=1.0)
        self._reader = None
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        self.port = None

    # -- transport ---------------------------------------------------------

    def _read_loop(self) -> None:
        while self._running and self._serial:
            try:
                data = self._serial.read(4096)
            except Exception as exc:
                self._running = False
                if self.on_disconnect:
                    self.on_disconnect(str(exc))
                return
            if not data:
                continue
            for channel, payload in self._decoder.feed(data):
                if channel == Channel.CONTROL:
                    self._handle_control(payload)
                elif channel == Channel.CONSOLE:
                    if self.on_console:
                        self.on_console(payload)
                elif channel == Channel.EVENT:
                    if self.on_event:
                        try:
                            self.on_event(json.loads(payload.decode()))
                        except Exception:
                            pass

    def _handle_control(self, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode())
        except Exception:
            return
        with self._replies:
            self._pending[message.get("id", -1)] = message
            self._replies.notify_all()

    def _write(self, channel: int, payload: bytes) -> None:
        if not self.connected:
            raise ConnectionError("payload link is not connected")
        with self._write_lock:
            self._serial.write(encode_frame(channel, payload))
            self._serial.flush()

    # -- RPC ---------------------------------------------------------------

    def rpc(self, method: str, params: Optional[dict] = None,
            timeout: float = RPC_TIMEOUT_SEC) -> Any:
        """
        Call a payload method and wait for its reply.

        Raises on payload-reported errors rather than returning them, so a
        caller cannot mistake a refusal for a result.
        """
        with self._lock:
            request_id = self._next_id
            self._next_id += 1

            self._write(Channel.CONTROL, json.dumps({
                "id": request_id,
                "method": method,
                "params": params or {},
            }).encode())

            deadline = time.time() + timeout
            with self._replies:
                while request_id not in self._pending:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise TimeoutError(f"payload did not answer '{method}'")
                    self._replies.wait(remaining)
                message = self._pending.pop(request_id)

        if not message.get("ok", False):
            raise RuntimeError(message.get("error", "unknown payload error"))
        return message.get("result", {})

    # -- convenience -------------------------------------------------------

    def get_schema(self) -> dict:
        """The payload's own description of every parameter it accepts."""
        return self.rpc("get_schema", timeout=20.0)

    def get_config(self) -> dict:
        return self.rpc("get_config", timeout=20.0)

    def set_config(self, values: Dict[str, Any]) -> dict:
        return self.rpc("set_config", {"values": values}, timeout=20.0)

    def reset_config(self, confirm: bool = True) -> dict:
        return self.rpc("reset_config", {"confirm": confirm})

    def get_status(self) -> dict:
        return self.rpc("get_status")

    def restart_service(self) -> dict:
        return self.rpc("restart_service", timeout=20.0)

    def get_logs(self, lines: int = 100) -> dict:
        return self.rpc("get_logs", {"lines": lines}, timeout=20.0)

    def list_images(self, limit: int = 50) -> dict:
        return self.rpc("list_images", {"limit": limit}, timeout=20.0)

    def fetch_image(self, name: str) -> dict:
        return self.rpc("fetch_image", {"name": name}, timeout=60.0)

    def generate_psk(self, bits: int = 256) -> dict:
        return self.rpc("generate_psk", {"bits": bits})

    # -- console -----------------------------------------------------------

    def shell_start(self, rows: int = 24, cols: int = 100) -> dict:
        return self.rpc("shell_start", {"rows": rows, "cols": cols})

    def shell_stop(self) -> dict:
        return self.rpc("shell_stop")

    def shell_resize(self, rows: int, cols: int) -> dict:
        return self.rpc("shell_resize", {"rows": rows, "cols": cols})

    def console_write(self, data: bytes) -> None:
        """Send keystrokes to the payload's shell."""
        self._write(Channel.CONSOLE, data)
