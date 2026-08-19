"""
One position from several disagreeing sources.

The balloon can be heard four ways: its own RAPTOR downlink through the ground
modem, a Meshtastic node plugged into this machine, the public Meshtastic MQTT
network, and -- when everything goes quiet -- dead reckoning from the last
known track.

The rules, in order:

  - Highest-priority source with a fresh fix wins.
  - A lower-priority source never displaces a *fresher* higher-priority one.
  - Freshness is wall-clock, so the choice is re-evaluated even when nothing
    new arrives. Otherwise the map would sit on a dead RAPTOR fix forever.
  - What the map is drawing, and how old it is, is always visible. A tracking
    display that silently switches sources is worse than one that admits it.

This mirrors the macOS app's PositionFusion so the two ground stations behave
identically.
"""

import math
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Dict, List, Optional

# Sender-supplied times are not trusted beyond this. A Meshtastic node with no
# time sync reports epoch 0, which read literally is a fix over fifty years old
# and gets discarded as stale the moment it arrives.
PLAUSIBLE_EPOCH = 1_577_836_800.0   # 2020-01-01
MAX_CLOCK_SKEW_SEC = 60.0


def reconcile_timestamp(supplied: Optional[float],
                        received: Optional[float] = None) -> float:
    """
    Reconcile a sender's clock with when we actually received the fix.

    Reception time is the honest fallback: it is the one thing we can vouch
    for. A supplied time is trusted only when it is plausible -- not before
    2020, and not meaningfully ahead of us. A future timestamp would give a
    negative age, so the fix would never go stale and would sit on the map
    claiming to be current indefinitely.
    """
    received = time.time() if received is None else received
    if supplied is None:
        return received
    if supplied < PLAUSIBLE_EPOCH:
        return received
    if supplied > received + MAX_CLOCK_SKEW_SEC:
        return received
    return supplied


class PositionSource(IntEnum):
    """Lower value means higher priority."""
    RAPTOR = 0            # our own downlink, through the ground modem
    MESHTASTIC_DIRECT = 1 # a node plugged into this machine
    MESHTASTIC_MQTT = 2   # the public mesh, via a gateway
    DEAD_RECKONING = 3    # extrapolated, not observed

    @property
    def label(self) -> str:
        return {
            PositionSource.RAPTOR: "RAPTOR downlink",
            PositionSource.MESHTASTIC_DIRECT: "Meshtastic (direct)",
            PositionSource.MESHTASTIC_MQTT: "Meshtastic (MQTT)",
            PositionSource.DEAD_RECKONING: "dead reckoning",
        }[self]

    @property
    def stale_after(self) -> float:
        """
        How long a fix from this source stays usable.

        The tolerances differ because the paths differ: our own downlink is
        continuous, so silence means something; a mesh relay is opportunistic
        and a gap is normal.
        """
        return {
            PositionSource.RAPTOR: 45.0,
            PositionSource.MESHTASTIC_DIRECT: 600.0,
            PositionSource.MESHTASTIC_MQTT: 900.0,
            PositionSource.DEAD_RECKONING: 300.0,
        }[self]


@dataclass
class PositionFix:
    source: PositionSource
    latitude: float
    longitude: float
    altitude: float
    timestamp: float
    satellites: Optional[int] = None
    rssi: Optional[int] = None
    snr: Optional[float] = None
    detail: Optional[str] = None

    @property
    def age(self) -> float:
        return time.time() - self.timestamp

    @property
    def is_stale(self) -> bool:
        return self.age > self.source.stale_after

    @property
    def age_description(self) -> str:
        seconds = int(self.age)
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        return f"{seconds // 3600}h ago"

    def as_dict(self) -> dict:
        return {
            "source": self.source.name.lower(),
            "source_label": self.source.label,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude": self.altitude,
            "timestamp": self.timestamp,
            "age_sec": round(self.age, 1),
            "age": self.age_description,
            "stale": self.is_stale,
            "satellites": self.satellites,
            "rssi": self.rssi,
            "snr": self.snr,
            "detail": self.detail,
        }


def _valid(latitude: float, longitude: float) -> bool:
    """
    Reject coordinates that cannot be a balloon.

    (0, 0) is the classic one: an unset GPS reports it, and plotting it drags
    the map into the Atlantic.
    """
    if latitude is None or longitude is None:
        return False
    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        return False
    return not (abs(latitude) < 1e-6 and abs(longitude) < 1e-6)


class PositionFusion:
    """Thread-safe: fixes arrive from serial readers, MQTT and the UI."""

    def __init__(self, history_limit: int = 5000):
        self._lock = threading.Lock()
        self._latest: Dict[PositionSource, PositionFix] = {}
        self._history: List[PositionFix] = []
        self._history_limit = history_limit
        self.extrapolation_enabled = True
        self.on_update: Optional[Callable[[Optional[PositionFix]], None]] = None

    # -- input -------------------------------------------------------------

    def submit(self, fix: PositionFix) -> Optional[PositionFix]:
        if not _valid(fix.latitude, fix.longitude):
            return self.best()

        with self._lock:
            self._latest[fix.source] = fix
            if fix.source is not PositionSource.DEAD_RECKONING:
                self._history.append(fix)
                if len(self._history) > self._history_limit:
                    del self._history[:len(self._history) - self._history_limit]

        best = self.best()
        if self.on_update:
            self.on_update(best)
        return best

    def submit_raptor(self, latitude: float, longitude: float, altitude: float,
                      satellites: Optional[int] = None, rssi: Optional[int] = None,
                      snr: Optional[float] = None,
                      timestamp: Optional[float] = None) -> Optional[PositionFix]:
        return self.submit(PositionFix(
            source=PositionSource.RAPTOR, latitude=latitude, longitude=longitude,
            altitude=altitude, timestamp=reconcile_timestamp(timestamp),
            satellites=satellites, rssi=rssi, snr=snr, detail="ground modem"))

    def submit_meshtastic(self, latitude: float, longitude: float, altitude: float,
                          detail: str = "", timestamp: Optional[float] = None,
                          satellites: Optional[int] = None,
                          rssi: Optional[int] = None,
                          snr: Optional[float] = None,
                          via_mqtt: bool = False) -> Optional[PositionFix]:
        source = (PositionSource.MESHTASTIC_MQTT if via_mqtt
                  else PositionSource.MESHTASTIC_DIRECT)
        return self.submit(PositionFix(
            source=source, latitude=latitude, longitude=longitude,
            altitude=altitude, timestamp=reconcile_timestamp(timestamp),
            satellites=satellites, rssi=rssi, snr=snr, detail=detail or None))

    # -- output ------------------------------------------------------------

    def best(self) -> Optional[PositionFix]:
        """The fix the map should draw."""
        with self._lock:
            for source in sorted(self._latest, key=lambda s: int(s)):
                fix = self._latest[source]
                if not fix.is_stale:
                    return fix

        extrapolated = self._extrapolate()
        if extrapolated is not None:
            return extrapolated

        # Everything is stale. Show the freshest thing we have rather than
        # nothing: a known-old position is still where to start looking.
        with self._lock:
            if not self._latest:
                return None
            return max(self._latest.values(), key=lambda f: f.timestamp)

    def _extrapolate(self) -> Optional[PositionFix]:
        """
        Carry the last track forward when every source has gone quiet.

        Labelled as dead reckoning, always. A guess presented as an observation
        is how a recovery team ends up searching the wrong field.
        """
        if not self.extrapolation_enabled:
            return None

        with self._lock:
            recent = [f for f in self._history[-10:]
                      if f.source is not PositionSource.DEAD_RECKONING]
        if len(recent) < 2:
            return None

        last, previous = recent[-1], recent[-2]
        span = last.timestamp - previous.timestamp
        if span <= 0:
            return None

        elapsed = time.time() - last.timestamp
        if elapsed > PositionSource.DEAD_RECKONING.stale_after:
            return None

        lat_rate = (last.latitude - previous.latitude) / span
        lon_rate = (last.longitude - previous.longitude) / span
        alt_rate = (last.altitude - previous.altitude) / span

        return PositionFix(
            source=PositionSource.DEAD_RECKONING,
            latitude=last.latitude + lat_rate * elapsed,
            longitude=last.longitude + lon_rate * elapsed,
            altitude=max(0.0, last.altitude + alt_rate * elapsed),
            timestamp=time.time(),
            detail=f"extrapolated from {last.source.label}, "
                   f"{int(elapsed)}s of drift",
        )

    def latest_by_source(self) -> Dict[str, dict]:
        with self._lock:
            return {s.name.lower(): f.as_dict() for s, f in self._latest.items()}

    def track(self, thin_to: int = 1000) -> List[dict]:
        """The observed track, thinned for drawing."""
        with self._lock:
            history = list(self._history)
        if len(history) <= thin_to:
            step = 1
        else:
            step = len(history) // thin_to + 1
        return [{"latitude": f.latitude, "longitude": f.longitude,
                 "altitude": f.altitude, "source": f.source.name.lower(),
                 "timestamp": f.timestamp}
                for f in history[::step]]

    def status(self) -> dict:
        best = self.best()
        return {
            "best": best.as_dict() if best else None,
            "sources": self.latest_by_source(),
            "history_points": len(self._history),
            "extrapolation_enabled": self.extrapolation_enabled,
        }
