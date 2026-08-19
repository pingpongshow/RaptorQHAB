#!/usr/bin/env python3
"""
Configuration and terminal service on the payload's USB gadget port.

Runs alongside the flight software and serves the companion app over
/dev/ttyGS0: a JSON configuration API on one channel and a real login shell on
another, multiplexed by common.linkproto.

Two rules this service exists to enforce:

  1. It binds to the USB gadget TTY and nothing else. The shell is reachable
     only by someone holding the cable. Nothing here is ever exposed over the
     radio, and that is checked at startup rather than left to convention.

  2. Configuration changes go through AirborneConfig.apply_updates, so the
     same validation, cross-field checks, and secret redaction apply whether a
     setting comes from the config file, an environment variable, or the app.

Secrets are write-only across this link: a channel PSK can be set, and its
fingerprint read back, but the value itself is never transmitted.
"""

import argparse
import errno
import fcntl
import json
import logging
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from airborne.config import STATE_ROOT, AirborneConfig
from airborne.params import PARAM_SPECS, SPECS_BY_NAME
from common.linkproto import Channel, FrameDecoder, encode_frame

logger = logging.getLogger("raptorhab.usbconsole")

PROTOCOL_VERSION = 1
DEFAULT_DEVICE = "/dev/ttyGS0"

# The gadget TTY is the only device this may bind to. A radio-facing serial
# port must never end up serving a shell.
ALLOWED_DEVICE_PREFIXES = ("/dev/ttyGS", "/dev/pts/", "/dev/ttyACM")


class ConsoleSecurityError(RuntimeError):
    """Raised when the service is pointed somewhere it must not serve."""


# --------------------------------------------------------------------------
# Shell channel
# --------------------------------------------------------------------------


class ShellSession:
    """
    A login shell on a pseudo-terminal, piped to the console channel.

    Spawned on demand rather than at startup, so a payload nobody has plugged
    into is not carrying an idle shell process for the whole flight.
    """

    def __init__(self, on_output: Callable[[bytes], None], shell: Optional[str] = None):
        self._on_output = on_output
        self._shell = shell or os.environ.get("SHELL", "/bin/bash")
        self._pid: Optional[int] = None
        self._fd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._pid is not None

    def start(self) -> None:
        if self.running:
            return

        pid, fd = pty.fork()

        if pid == 0:  # child
            os.environ["TERM"] = os.environ.get("TERM", "xterm-256color")
            os.execvp(self._shell, [self._shell, "-l"])
            os._exit(1)  # only reached if exec fails

        self._pid = pid
        self._fd = fd
        self._stop.clear()

        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        logger.info(f"Shell started (pid {pid})")

    def _pump(self) -> None:
        """Forward shell output to the link until it exits."""
        while not self._stop.is_set():
            # Take a local copy: stop() clears self._fd from another thread,
            # and reading it twice can hand os.read a None between the check
            # and the call.
            fd = self._fd
            if fd is None:
                break
            try:
                readable, _, _ = select.select([fd], [], [], 0.2)
                if not readable:
                    continue
                data = os.read(fd, 4096)
                if not data:
                    break
                self._on_output(data)
            except OSError as e:
                if e.errno in (errno.EIO, errno.EBADF):
                    break  # the shell exited and closed its side
                if e.errno != errno.EAGAIN:
                    logger.debug(f"Shell read error: {e}")
                    break

        logger.info("Shell session ended")
        self._reap()

    def write(self, data: bytes) -> None:
        if self._fd is None:
            return
        try:
            os.write(self._fd, data)
        except OSError as e:
            logger.debug(f"Shell write failed: {e}")

    def resize(self, rows: int, cols: int) -> None:
        """Tell the shell its window size, so full-screen tools render right."""
        if self._fd is None:
            return
        try:
            packed = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, packed)
        except OSError as e:
            logger.debug(f"Resize failed: {e}")

    def stop(self) -> None:
        self._stop.set()
        self._reap()

    def _reap(self) -> None:
        pid, self._pid = self._pid, None
        fd, self._fd = self._fd, None

        if pid is not None:
            try:
                os.kill(pid, signal.SIGHUP)
                # Give it a moment to exit on its own before insisting.
                for _ in range(20):
                    if os.waitpid(pid, os.WNOHANG)[0] == pid:
                        break
                    time.sleep(0.05)
                else:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass

        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Control channel
# --------------------------------------------------------------------------


class ControlHandler:
    """Handles the JSON configuration API."""

    def __init__(self, config: AirborneConfig, service_name: str = "raptorhab-airborne"):
        self.config = config
        self.service_name = service_name
        self._started_at = time.monotonic()

    def handle(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dispatch one request.

        Every reply echoes the request id so the app can match a response to
        its call, and carries ok=true/false rather than relying on the
        presence of an "error" key.
        """
        request_id = request.get("id")
        method = request.get("method", "")

        handler = getattr(self, f"_rpc_{method}", None)
        if handler is None:
            return self._error(request_id, f"unknown method: {method!r}")

        try:
            result = handler(request.get("params") or {})
            return {"id": request_id, "ok": True, "result": result}
        except Exception as e:
            logger.exception(f"Method {method!r} failed")
            return self._error(request_id, str(e))

    @staticmethod
    def _error(request_id: Any, message: str) -> Dict[str, Any]:
        return {"id": request_id, "ok": False, "error": message}

    # --- methods ----------------------------------------------------------

    def _rpc_hello(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        """Identify the payload, so the app knows what it has connected to."""
        return {
            "protocol_version": PROTOCOL_VERSION,
            "callsign": self.config.callsign,
            "payload_id": self.config.payload_id,
            "hostname": os.uname().nodename,
            "state_root": STATE_ROOT,
            "service_uptime_sec": round(time.monotonic() - self._started_at, 1),
        }

    def _rpc_get_schema(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        """
        The full parameter schema.

        The app renders its form from this, so a parameter added on the Pi
        appears in the UI with no Swift change.
        """
        return self.config.schema()

    def _rpc_get_config(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        """Current values, with secrets replaced by a fingerprint."""
        values = self.config.to_dict(redact_secrets=True)
        return {"values": values, "secrets": self._secret_fingerprints()}

    def _rpc_set_config(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply a batch of changes and persist them.

        All-or-nothing, and validated exactly as a config file would be. A
        partially applied radio configuration can be worse than no change.
        """
        updates = params.get("values") or {}
        if not isinstance(updates, dict):
            raise ValueError("params.values must be an object")

        result = self.config.apply_updates(updates)

        if result["ok"] and result["applied"]:
            result["saved"] = self.config.save()
        else:
            result["saved"] = False

        result["secrets"] = self._secret_fingerprints()
        return result

    def _rpc_reset_config(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Reset named parameters, or everything, to built-in defaults."""
        names = params.get("names")
        defaults = AirborneConfig().to_dict()

        if names is None:
            targets = defaults
        else:
            unknown = [n for n in names if n not in defaults]
            if unknown:
                raise ValueError(f"unknown parameters: {', '.join(unknown)}")
            targets = {n: defaults[n] for n in names}

        result = self.config.apply_updates(targets)
        result["saved"] = self.config.save() if result["ok"] else False
        return result

    def _rpc_get_status(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        """Live payload state, for the dashboard."""
        return {
            "service": self._service_status(),
            "system": self._system_status(),
            "storage": self._storage_status(),
        }

    def _rpc_restart_service(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        """Restart the flight software, so restart-required settings apply."""
        completed = subprocess.run(
            ["sudo", "systemctl", "restart", self.service_name],
            capture_output=True, text=True, timeout=30,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "restart failed")
        return {"restarted": self.service_name}

    def _rpc_list_images(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Recent captures, newest first."""
        limit = max(1, min(500, int(params.get("limit", 50))))
        directory = Path(self.config.image_storage_path)

        if not directory.is_dir():
            return {"images": []}

        # Sealed captures carry a .rhs suffix, so matching only *.webp made
        # every image invisible to the app the moment recording encryption
        # was switched on.
        candidates = list(directory.glob("*.webp")) + list(directory.glob("*.webp.rhs"))

        entries = []
        for path in sorted(candidates, key=lambda p: p.stat().st_mtime,
                           reverse=True)[:limit]:
            stat = path.stat()
            entries.append({
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified": int(stat.st_mtime),
                "encrypted": path.name.endswith(".rhs"),
            })
        return {"images": entries}

    def _rpc_fetch_image(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Read one image back over the cable.

        The name is resolved inside the image directory and re-checked after
        resolution, so a crafted path cannot walk out of it.
        """
        import base64

        name = params.get("name")
        if not name:
            raise ValueError("params.name is required")

        directory = Path(self.config.image_storage_path).resolve()
        target = (directory / name).resolve()

        if not str(target).startswith(str(directory) + os.sep):
            raise ValueError("name must refer to a file inside the image directory")
        if not target.is_file():
            raise ValueError(f"no such image: {name}")

        data = target.read_bytes()
        return {
            "name": target.name,
            "size_bytes": len(data),
            "data_base64": base64.b64encode(data).decode("ascii"),
        }

    def _rpc_get_logs(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Recent journal lines for the flight service."""
        lines = max(1, min(2000, int(params.get("lines", 200))))
        completed = subprocess.run(
            ["journalctl", "-u", self.service_name, "-n", str(lines),
             "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, timeout=30,
        )
        return {"lines": completed.stdout.splitlines()}

    def _rpc_generate_psk(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate a channel key on the payload.

        Returned once, so the operator can copy it into their handheld, and
        never readable again -- subsequent reads give only the fingerprint.
        """
        import base64

        from common.meshtastic.crypto import format_psk_fingerprint, generate_psk

        length = int(params.get("length", 32))
        key = generate_psk(length)
        return {
            "psk_base64": base64.b64encode(key).decode("ascii"),
            "fingerprint": format_psk_fingerprint(key),
        }

    # --- helpers ----------------------------------------------------------

    def _secret_fingerprints(self) -> Dict[str, str]:
        """
        Non-reversible identifiers for secret parameters.

        Lets the operator confirm the balloon and their handheld hold the same
        key without the key itself crossing the link.
        """
        from common.meshtastic.crypto import expand_psk, format_psk_fingerprint, parse_psk

        out: Dict[str, str] = {}
        for spec in PARAM_SPECS:
            if not spec.secret:
                continue
            raw = getattr(self.config, spec.name, "")
            try:
                key = expand_psk(parse_psk(raw))
                out[spec.name] = format_psk_fingerprint(key)
            except ValueError:
                out[spec.name] = "invalid"
        return out

    def _service_status(self) -> Dict[str, Any]:
        try:
            completed = subprocess.run(
                ["systemctl", "show", self.service_name,
                 "--property=ActiveState,SubState,ExecMainStartTimestamp,NRestarts"],
                capture_output=True, text=True, timeout=10,
            )
            fields = dict(
                line.split("=", 1)
                for line in completed.stdout.splitlines() if "=" in line
            )
        except (subprocess.SubprocessError, OSError) as e:
            return {"error": str(e)}

        return {
            "name": self.service_name,
            "active": fields.get("ActiveState", "unknown"),
            "sub": fields.get("SubState", "unknown"),
            "started": fields.get("ExecMainStartTimestamp", ""),
            "restarts": int(fields.get("NRestarts", "0") or 0),
        }

    def _system_status(self) -> Dict[str, Any]:
        from airborne.utils import get_cpu_temperature, get_memory_usage

        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])

        memory = get_memory_usage()
        return {
            "uptime_sec": round(uptime),
            "cpu_temp_c": round(get_cpu_temperature(), 1),
            "memory_percent": round(memory.get("percent", 0), 1),
            "load": os.getloadavg()[0],
        }

    def _storage_status(self) -> Dict[str, Any]:
        from airborne.utils import get_disk_usage

        usage = get_disk_usage(self.config.image_storage_path)
        directory = Path(self.config.image_storage_path)
        count = 0
        if directory.is_dir():
            count = len(list(directory.glob("*.webp"))) + \
                    len(list(directory.glob("*.webp.rhs")))

        return {
            "free_bytes": usage.get("free", 0),
            "percent_used": round(usage.get("percent", 0), 1),
            "image_count": count,
        }


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


class UsbConsoleService:
    """Serves the control and console channels on the gadget TTY."""

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        config: Optional[AirborneConfig] = None,
        allow_shell: bool = True,
        allow_any_device: bool = False,
    ):
        if not allow_any_device and not device.startswith(ALLOWED_DEVICE_PREFIXES):
            raise ConsoleSecurityError(
                f"refusing to serve on {device}: this service offers a login "
                f"shell and must bind only to the USB gadget TTY "
                f"({', '.join(ALLOWED_DEVICE_PREFIXES)}). Serving it on a "
                f"radio-facing port would put a shell on the air."
            )

        self.device = device
        self.config = config or AirborneConfig.load()
        self.allow_shell = allow_shell

        self._control = ControlHandler(self.config)
        self._decoder = FrameDecoder()
        self._shell: Optional[ShellSession] = None
        self._fd: Optional[int] = None
        self._write_lock = threading.Lock()
        self._stop = threading.Event()

    # --- link -------------------------------------------------------------

    def _open(self) -> int:
        fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

        # Raw mode. Any line discipline processing would mangle the framing
        # and, on the console channel, the terminal stream itself.
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0                      # iflag
            attrs[1] = 0                      # oflag
            attrs[3] = 0                      # lflag
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except termios.error as e:
            # A pty used for testing may not support all of this.
            logger.debug(f"Could not set raw mode on {self.device}: {e}")

        return fd

    # A control reply must not be dropped: the app is waiting on it and would
    # hang. A serial line fills its buffer routinely for a payload this size,
    # so the writer waits for drain rather than giving up. The timeout is the
    # backstop for a host that has stopped reading entirely -- at that point
    # the link is gone and the frame is worthless anyway.
    WRITE_TIMEOUT_SEC = 5.0

    def _send(self, channel: int, payload: bytes) -> None:
        fd = self._fd
        if fd is None:
            return

        frame = encode_frame(channel, payload)

        with self._write_lock:
            written = 0
            deadline = time.monotonic() + self.WRITE_TIMEOUT_SEC

            while written < len(frame):
                if time.monotonic() > deadline:
                    logger.warning(
                        f"Link write timed out after {written}/{len(frame)} bytes "
                        f"on channel {channel}; the host is not reading"
                    )
                    return
                try:
                    written += os.write(fd, frame[written:])
                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        # Wait for the buffer to drain rather than discarding.
                        _, writable, _ = select.select([], [fd], [], 0.25)
                        if not writable:
                            continue
                    else:
                        logger.debug(f"Link write failed: {e}")
                        return

    def send_json(self, channel: int, message: Dict[str, Any]) -> None:
        self._send(channel, json.dumps(message).encode("utf-8"))

    def send_event(self, event: str, **fields: Any) -> None:
        """Push an unsolicited message to the app."""
        self.send_json(Channel.EVENT, {"event": event, **fields})

    # --- dispatch ---------------------------------------------------------

    def _on_frame(self, channel: int, payload: bytes) -> None:
        if channel == Channel.CONTROL:
            self._on_control(payload)
        elif channel == Channel.CONSOLE:
            self._on_console(payload)
        else:
            logger.debug(f"Ignoring frame on unexpected channel {channel}")

    def _on_control(self, payload: bytes) -> None:
        try:
            request = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self.send_json(Channel.CONTROL, {"ok": False, "error": f"bad JSON: {e}"})
            return

        if not isinstance(request, dict):
            self.send_json(
                Channel.CONTROL, {"ok": False, "error": "request must be an object"}
            )
            return

        method = request.get("method")

        # Shell lifecycle lives here rather than in ControlHandler, because it
        # needs the link to pipe output back through.
        if method == "shell_start":
            self.send_json(Channel.CONTROL, self._start_shell(request))
            return
        if method == "shell_stop":
            self._stop_shell()
            self.send_json(Channel.CONTROL, {"id": request.get("id"), "ok": True,
                                             "result": {"running": False}})
            return
        if method == "shell_resize":
            params = request.get("params") or {}
            if self._shell:
                self._shell.resize(int(params.get("rows", 24)),
                                   int(params.get("cols", 80)))
            self.send_json(Channel.CONTROL, {"id": request.get("id"), "ok": True,
                                             "result": {}})
            return

        self.send_json(Channel.CONTROL, self._control.handle(request))

    def _on_console(self, payload: bytes) -> None:
        if self._shell and self._shell.running:
            self._shell.write(payload)

    def _start_shell(self, request: Dict[str, Any]) -> Dict[str, Any]:
        request_id = request.get("id")

        if not self.allow_shell:
            return {"id": request_id, "ok": False,
                    "error": "shell access is disabled on this payload"}

        if self._shell and self._shell.running:
            return {"id": request_id, "ok": True, "result": {"running": True}}

        self._shell = ShellSession(
            on_output=lambda data: self._send(Channel.CONSOLE, data)
        )
        try:
            self._shell.start()
        except OSError as e:
            self._shell = None
            return {"id": request_id, "ok": False, "error": f"could not start shell: {e}"}

        params = request.get("params") or {}
        self._shell.resize(int(params.get("rows", 24)), int(params.get("cols", 80)))
        return {"id": request_id, "ok": True, "result": {"running": True}}

    def _stop_shell(self) -> None:
        if self._shell:
            self._shell.stop()
            self._shell = None

    # --- lifecycle --------------------------------------------------------

    def run(self) -> None:
        """Serve until stopped. Reopens the device if the host disconnects."""
        logger.info(f"USB console serving on {self.device}")

        while not self._stop.is_set():
            try:
                self._fd = self._open()
            except OSError as e:
                logger.debug(f"Cannot open {self.device}: {e}; retrying")
                self._stop.wait(2.0)
                continue

            self._decoder.reset()
            logger.info("Link open")

            try:
                self._serve()
            except OSError as e:
                logger.info(f"Link closed: {e}")
            finally:
                self._stop_shell()
                if self._fd is not None:
                    try:
                        os.close(self._fd)
                    except OSError:
                        pass
                    self._fd = None

        logger.info("USB console stopped")

    def _serve(self) -> None:
        while not self._stop.is_set():
            readable, _, _ = select.select([self._fd], [], [], 0.5)
            if not readable:
                continue

            try:
                data = os.read(self._fd, 4096)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    continue
                raise

            if not data:
                # On a gadget TTY this means the host went away.
                return

            for channel, payload in self._decoder.feed(data):
                self._on_frame(channel, payload)

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RaptorHab USB configuration and terminal service"
    )
    parser.add_argument("--device", default=DEFAULT_DEVICE,
                        help="Gadget TTY to serve on")
    parser.add_argument("--config", default=None, help="Config file path")
    parser.add_argument("--no-shell", action="store_true",
                        help="Serve configuration only, no terminal")
    parser.add_argument("--allow-any-device", action="store_true",
                        help="Bypass the gadget-TTY check. Testing only.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        service = UsbConsoleService(
            device=args.device,
            config=AirborneConfig.load(path=args.config),
            allow_shell=not args.no_shell,
            allow_any_device=args.allow_any_device,
        )
    except ConsoleSecurityError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    signal.signal(signal.SIGTERM, lambda *_: service.stop())
    signal.signal(signal.SIGINT, lambda *_: service.stop())

    service.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
