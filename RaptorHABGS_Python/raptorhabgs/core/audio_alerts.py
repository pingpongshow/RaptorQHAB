"""
Audible alerts for things you must not miss while looking elsewhere.

During a flight the operator is driving, holding an antenna, or watching the
sky -- not the screen. Burst, landing and loss of signal are the moments that
change what you do next, so they get a sound.

Which alerts default to on is a deliberate choice: the ones that demand action
(burst, landing, signal lost, low battery) are enabled, and the ones that
merely confirm things are working (every telemetry packet, every image) are
not. An alert that fires constantly is one the operator learns to ignore, which
costs them the alert that mattered.

Sound playback is deliberately best-effort and never fatal. This runs on a Mac
in the field and on a Raspberry Pi in a car; if neither has a usable audio
path, the alert still reaches the log and the UI. A ground station must not
fall over because it could not make a noise.
"""

import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class AlertType(Enum):
    TELEMETRY_RECEIVED = "Telemetry received"
    BURST = "Burst detected"
    LANDING = "Landing detected"
    SIGNAL_LOST = "Signal lost"
    SIGNAL_RESTORED = "Signal restored"
    ALTITUDE_MILESTONE = "Altitude milestone"
    LOW_BATTERY = "Low battery"
    IMAGE_RECEIVED = "Image received"

    @property
    def default_enabled(self) -> bool:
        return self in {AlertType.BURST, AlertType.LANDING,
                        AlertType.SIGNAL_LOST, AlertType.LOW_BATTERY}

    @property
    def macos_sound(self) -> str:
        return {
            AlertType.TELEMETRY_RECEIVED: "Tink",
            AlertType.BURST: "Sosumi",
            AlertType.LANDING: "Glass",
            AlertType.SIGNAL_LOST: "Basso",
            AlertType.SIGNAL_RESTORED: "Ping",
            AlertType.ALTITUDE_MILESTONE: "Pop",
            AlertType.LOW_BATTERY: "Funk",
            AlertType.IMAGE_RECEIVED: "Submarine",
        }[self]

    @property
    def urgent(self) -> bool:
        """Whether this warrants repeating rather than a single chime."""
        return self in {AlertType.BURST, AlertType.LANDING,
                        AlertType.SIGNAL_LOST}


@dataclass
class AlertEvent:
    timestamp: float
    alert: AlertType
    message: str


@dataclass
class AudioAlertConfig:
    enabled: bool = True
    volume: float = 0.7
    speak: bool = False                 # spoken messages as well as a tone
    signal_lost_after_sec: float = 60.0
    low_battery_mv: int = 3500
    altitude_milestones_m: List[float] = field(
        default_factory=lambda: [1000, 5000, 10000, 15000, 20000, 25000, 30000])
    per_alert: Dict[str, bool] = field(default_factory=dict)

    def is_enabled(self, alert: AlertType) -> bool:
        return self.per_alert.get(alert.name, alert.default_enabled)


class SoundPlayer:
    """
    Whatever this machine can actually make a noise with.

    Resolved once at startup so a missing player is reported clearly instead of
    failing silently on every alert.
    """

    def __init__(self):
        self.system = platform.system()
        self.method: str = "none"
        self._resolve()

    def _resolve(self) -> None:
        if self.system == "Darwin" and shutil.which("afplay"):
            self.method = "afplay"
        elif shutil.which("paplay"):
            self.method = "paplay"
        elif shutil.which("aplay"):
            self.method = "aplay"
        else:
            self.method = "bell"
        logger.info(f"Audio alerts using: {self.method}")

    @property
    def available(self) -> bool:
        return self.method != "none"

    def play(self, alert: AlertType, volume: float = 0.7) -> bool:
        try:
            if self.method == "afplay":
                path = f"/System/Library/Sounds/{alert.macos_sound}.aiff"
                if not os.path.exists(path):
                    return self._bell()
                subprocess.Popen(
                    ["afplay", "-v", f"{max(0.0, min(1.0, volume)):.2f}", path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True

            if self.method in ("paplay", "aplay"):
                # A Pi has no stock sound set, so synthesise a short tone
                # rather than depending on files that may not exist.
                return self._tone(alert, volume)

            return self._bell()
        except Exception as exc:
            logger.debug(f"alert playback failed: {exc}")
            return self._bell()

    def _tone(self, alert: AlertType, volume: float) -> bool:
        """A short sine burst; pitch distinguishes urgent from routine."""
        import array
        import math
        import tempfile
        import wave

        frequency = 880.0 if alert.urgent else 440.0
        seconds = 0.35 if alert.urgent else 0.18
        rate = 22050
        amplitude = int(32767 * max(0.0, min(1.0, volume)))

        samples = array.array("h")
        for i in range(int(rate * seconds)):
            # Taper the ends so it does not click.
            envelope = min(1.0, i / 200.0,
                           (int(rate * seconds) - i) / 200.0)
            samples.append(int(amplitude * envelope *
                               math.sin(2 * math.pi * frequency * i / rate)))

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            with wave.open(handle, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(samples.tobytes())
            path = handle.name

        subprocess.Popen([self.method, path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Clean up shortly after playback rather than blocking on it.
        threading.Timer(5.0, lambda: os.path.exists(path) and os.unlink(path)).start()
        return True

    def _bell(self) -> bool:
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
            return True
        except Exception:
            return False

    def speak(self, message: str) -> None:
        try:
            if self.system == "Darwin" and shutil.which("say"):
                subprocess.Popen(["say", message],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif shutil.which("espeak-ng"):
                subprocess.Popen(["espeak-ng", message],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif shutil.which("espeak"):
                subprocess.Popen(["espeak", message],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


class AudioAlertManager:
    """Decides when to make a noise, and makes it."""

    # A repeated alert within this window is suppressed, so a balloon
    # oscillating around a threshold cannot produce a stream of beeps.
    REARM_SEC = 20.0

    def __init__(self, config: Optional[AudioAlertConfig] = None):
        self.config = config or AudioAlertConfig()
        self.player = SoundPlayer()
        self.history: List[AlertEvent] = []
        self.max_history = 200
        self.on_alert: Optional[Callable[[AlertEvent], None]] = None

        self._last_played: Dict[AlertType, float] = {}
        self._last_telemetry: Optional[float] = None
        self._signal_lost = False
        self._reached_milestones: set = set()
        self._peak_altitude = 0.0
        self._burst_announced = False
        self._lock = threading.Lock()

    # -- firing ------------------------------------------------------------

    def play(self, alert: AlertType, message: str = "") -> bool:
        if not self.config.enabled or not self.config.is_enabled(alert):
            return False

        now = time.time()
        with self._lock:
            if now - self._last_played.get(alert, 0.0) < self.REARM_SEC:
                return False
            self._last_played[alert] = now

        event = AlertEvent(timestamp=now, alert=alert,
                           message=message or alert.value)
        self.history.append(event)
        if len(self.history) > self.max_history:
            del self.history[:len(self.history) - self.max_history]

        self.player.play(alert, self.config.volume)
        if self.config.speak:
            self.player.speak(event.message)
        if self.on_alert:
            self.on_alert(event)
        logger.info(f"Alert: {event.message}")
        return True

    # -- observation -------------------------------------------------------

    def on_telemetry(self, altitude_m: Optional[float] = None,
                     battery_mv: Optional[int] = None,
                     vertical_rate_mps: Optional[float] = None) -> None:
        """Feed each telemetry point; alerts fall out of the changes."""
        # Monotonic, and paired with check_signal(): this measures silence, not
        # a point in history. On a Pi ground station -- no RTC -- a wall-clock
        # step would either fire a spurious "signal lost" or suppress a real
        # one.
        now = time.monotonic()
        if self._signal_lost:
            self._signal_lost = False
            self.play(AlertType.SIGNAL_RESTORED, "Signal restored")
        self._last_telemetry = now
        self.play(AlertType.TELEMETRY_RECEIVED)

        if altitude_m is not None:
            self._check_altitude(altitude_m, vertical_rate_mps)

        # A battery reading of zero means "no monitor fitted", not "flat".
        # Alerting on it would cry wolf for the whole flight.
        if battery_mv:
            if battery_mv < self.config.low_battery_mv:
                self.play(AlertType.LOW_BATTERY,
                          f"Low battery: {battery_mv} millivolts")

    def _check_altitude(self, altitude_m: float,
                        vertical_rate_mps: Optional[float]) -> None:
        self._peak_altitude = max(self._peak_altitude, altitude_m)

        for milestone in sorted(self.config.altitude_milestones_m):
            if altitude_m >= milestone and milestone not in self._reached_milestones:
                self._reached_milestones.add(milestone)
                label = (f"{int(milestone / 1000)} kilometers"
                         if milestone >= 1000 else f"{int(milestone)} meters")
                self.play(AlertType.ALTITUDE_MILESTONE,
                          f"Altitude milestone: {label}")

        # Burst: descending hard from a height that could only be a flight.
        if (not self._burst_announced
                and vertical_rate_mps is not None
                and vertical_rate_mps < -5.0
                and self._peak_altitude > 3000.0
                and altitude_m < self._peak_altitude - 200.0):
            self._burst_announced = True
            self.play(AlertType.BURST,
                      f"Burst at {int(self._peak_altitude)} meters")

    def on_landing(self) -> None:
        self.play(AlertType.LANDING, "Landing detected")

    def on_image(self, image_id: Optional[int] = None) -> None:
        self.play(AlertType.IMAGE_RECEIVED,
                  f"Image {image_id} received" if image_id else "Image received")

    def check_signal(self) -> None:
        """Call periodically; silence is only detectable by watching the clock."""
        if self._last_telemetry is None or self._signal_lost:
            return
        if time.monotonic() - self._last_telemetry > self.config.signal_lost_after_sec:
            self._signal_lost = True
            self.play(AlertType.SIGNAL_LOST,
                      f"Signal lost for {int(self.config.signal_lost_after_sec)} seconds")

    def reset_flight(self) -> None:
        """Clear per-flight state so a second flight alerts properly."""
        self._reached_milestones.clear()
        self._peak_altitude = 0.0
        self._burst_announced = False
        self._signal_lost = False
        self._last_telemetry = None

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "volume": self.config.volume,
            "speak": self.config.speak,
            "player": self.player.method,
            "signal_lost": self._signal_lost,
            "peak_altitude_m": self._peak_altitude,
            "milestones_reached": sorted(self._reached_milestones),
            "alerts": {a.name: {"label": a.value,
                                "enabled": self.config.is_enabled(a)}
                       for a in AlertType},
            "history": [{"timestamp": e.timestamp, "alert": e.alert.name,
                         "message": e.message} for e in self.history[-50:]],
        }
