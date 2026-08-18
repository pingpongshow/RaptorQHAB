"""
Flight zone tracking: where the balloon is relative to the launch site, and
what that means for how it should be spending its airtime.

Four zones, each with a different transmit priority:

    LAUNCH   Inside the launch radius. Recovery crews and the ground station
             are here, the link is short and strong, so almost all airtime
             goes to images. Meshtastic beacons are occasional.

    CRUISE   Outside the launch radius, still flying. Images become expensive
             and less useful; Meshtastic becomes the thing that tells people
             where the balloon is. Most airtime is idle, which conserves
             battery for the descent.

    LANDED   On the ground and not moving. The GFSK link is probably blocked
             by terrain, and a low-rate LoRa beacon at ground level is very
             often what actually finds the payload. Images stop entirely;
             everything goes to slow Meshtastic beacons.

    UNKNOWN  No fix has ever been acquired. Treated as LAUNCH, because before
             the first fix the balloon is almost certainly still on the pad.

Transitions are deliberately sticky. A balloon drifting along the launch
radius, or bobbing at apogee, must not thrash between schedules -- each
change costs a radio reconfiguration and a discontinuity in what the ground
sees.
"""

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Optional, Tuple
from collections import deque

logger = logging.getLogger(__name__)

EARTH_RADIUS_M = 6371000.0


class Zone(str, Enum):
    """Flight zone, which selects the transmit schedule."""

    UNKNOWN = "unknown"
    LAUNCH = "launch"
    CRUISE = "cruise"
    LANDED = "landed"


@dataclass
class ZoneState:
    """Current zone and the evidence behind it."""

    zone: Zone
    changed_at: float
    distance_from_launch_m: Optional[float] = None
    altitude_agl_m: Optional[float] = None
    vertical_rate_mps: Optional[float] = None
    reason: str = ""


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


class ZoneManager:
    """Decides the flight zone from GPS position and altitude history."""

    def __init__(
        self,
        launch_latitude: float = 0.0,
        launch_longitude: float = 0.0,
        launch_altitude_m: Optional[float] = None,
        radius_m: float = 8000.0,
        hysteresis_m: float = 800.0,
        altitude_override_m: float = 3000.0,
        landed_altitude_m: float = 1000.0,
        landed_vertical_rate_mps: float = 0.5,
        landed_dwell_sec: float = 120.0,
        landed_arm_altitude_m: float = 2000.0,
        altitude_window_sec: float = 120.0,
        auto_capture_launch_point: bool = True,
    ):
        """
        Args:
            launch_latitude, launch_longitude: Launch site. Both zero means
                "capture from the first 3D fix" when auto capture is on.
            launch_altitude_m: Launch elevation MSL. None captures it with the
                launch point, so altitudes can be reported above ground level.
            radius_m: Launch zone radius.
            hysteresis_m: The balloon must be this far beyond the radius to
                leave, and this far back inside to return. Stops a balloon
                tracking the boundary from thrashing schedules.
            altitude_override_m: Above this height above launch, force CRUISE
                regardless of ground distance. A balloon at 3 km that is still
                overhead is no longer a launch-site problem.
            landed_altitude_m: Below this height above launch, LANDED becomes
                possible.
            landed_vertical_rate_mps: Below this ascent/descent rate counts as
                stationary.
            landed_dwell_sec: How long those conditions must hold.
            landed_arm_altitude_m: Landing detection stays disarmed until the
                balloon has been at least this high above the launch site.
                Without it a payload sitting on the pad during setup -- low,
                stationary, for far longer than any dwell period -- declares
                itself landed before launch and stops capturing images.
            altitude_window_sec: How much altitude history feeds the vertical
                rate estimate.
            auto_capture_launch_point: Capture the launch point from the first
                3D fix if none was configured.
        """
        self.launch_latitude = launch_latitude
        self.launch_longitude = launch_longitude
        self.launch_altitude_m = launch_altitude_m
        self.radius_m = radius_m
        self.hysteresis_m = hysteresis_m
        self.altitude_override_m = altitude_override_m
        self.landed_altitude_m = landed_altitude_m
        self.landed_vertical_rate_mps = landed_vertical_rate_mps
        self.landed_dwell_sec = landed_dwell_sec
        self.landed_arm_altitude_m = landed_arm_altitude_m
        self.altitude_window_sec = altitude_window_sec
        self.auto_capture_launch_point = auto_capture_launch_point

        self._launch_point_captured = not (
            launch_latitude == 0.0 and launch_longitude == 0.0
        )

        self._state = ZoneState(
            zone=Zone.UNKNOWN,
            changed_at=time.time(),
            reason="no fix yet",
        )

        # Altitude history for the vertical rate estimate. A short window
        # keeps it responsive; a single sample pair would be dominated by GPS
        # altitude noise, which is routinely several metres. Pruned by age as
        # well as count, so a gap in GPS coverage cannot leave a stale sample
        # skewing the fit long after it stopped being relevant.
        self._altitude_history: Deque[Tuple[float, float]] = deque(maxlen=64)
        self._landed_candidate_since: Optional[float] = None
        self._peak_altitude_m: float = 0.0
        self._peak_altitude_agl_m: float = 0.0
        self._landing_armed: bool = False

    # -- accessors ---------------------------------------------------------

    @property
    def state(self) -> ZoneState:
        return self._state

    @property
    def zone(self) -> Zone:
        return self._state.zone

    @property
    def launch_point_known(self) -> bool:
        return self._launch_point_captured

    @property
    def peak_altitude_m(self) -> float:
        return self._peak_altitude_m

    @property
    def landing_armed(self) -> bool:
        """Whether the balloon has flown high enough to arm landing detection."""
        return self._landing_armed

    # -- update ------------------------------------------------------------

    def update(
        self,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        altitude_m: Optional[float] = None,
        fix_type: int = 0,
        now: Optional[float] = None,
    ) -> ZoneState:
        """
        Feed a GPS reading and get the resulting zone.

        With no usable fix the current zone is held, matching the region
        manager's behaviour and the Q1 decision: the balloon keeps doing what
        it was doing rather than reverting to a default.
        """
        now = time.time() if now is None else now

        if fix_type < 2 or latitude is None or longitude is None:
            return self._state

        if not self._launch_point_captured:
            self._capture_launch_point(latitude, longitude, altitude_m)

        if altitude_m is not None:
            self._altitude_history.append((now, altitude_m))
            self._prune_altitude_history(now)
            self._peak_altitude_m = max(self._peak_altitude_m, altitude_m)

            agl = self._altitude_agl(altitude_m)
            if agl is not None:
                self._peak_altitude_agl_m = max(self._peak_altitude_agl_m, agl)
                if (
                    not self._landing_armed
                    and self._peak_altitude_agl_m >= self.landed_arm_altitude_m
                ):
                    self._landing_armed = True
                    logger.info(
                        f"Landing detection armed: reached "
                        f"{self._peak_altitude_agl_m:.0f} m AGL"
                    )

        distance = haversine_m(
            self.launch_latitude, self.launch_longitude, latitude, longitude
        )
        altitude_agl = self._altitude_agl(altitude_m)
        vertical_rate = self._vertical_rate()

        zone, reason = self._classify(distance, altitude_agl, vertical_rate, now)

        if zone is not self._state.zone:
            logger.info(
                f"Flight zone {self._state.zone.value} -> {zone.value}: {reason} "
                f"(distance {distance / 1000:.1f} km, "
                f"altitude {'?' if altitude_agl is None else f'{altitude_agl:.0f}'} m AGL, "
                f"rate {'?' if vertical_rate is None else f'{vertical_rate:+.1f}'} m/s)"
            )
            self._state = ZoneState(
                zone=zone,
                changed_at=now,
                distance_from_launch_m=distance,
                altitude_agl_m=altitude_agl,
                vertical_rate_mps=vertical_rate,
                reason=reason,
            )
        else:
            # Same zone, refreshed measurements.
            self._state = ZoneState(
                zone=zone,
                changed_at=self._state.changed_at,
                distance_from_launch_m=distance,
                altitude_agl_m=altitude_agl,
                vertical_rate_mps=vertical_rate,
                reason=reason,
            )

        return self._state

    # -- classification ----------------------------------------------------

    def _classify(
        self,
        distance_m: float,
        altitude_agl_m: Optional[float],
        vertical_rate_mps: Optional[float],
        now: float,
    ) -> Tuple[Zone, str]:
        """Decide the zone. Checked most-specific first."""
        if self._is_landed(altitude_agl_m, vertical_rate_mps, now):
            return Zone.LANDED, "low and stationary"

        # Altitude override: high enough that the launch radius is irrelevant.
        if (
            altitude_agl_m is not None
            and self.altitude_override_m > 0
            and altitude_agl_m >= self.altitude_override_m
        ):
            return Zone.CRUISE, f"above {self.altitude_override_m:.0f} m AGL"

        current = self._state.zone

        # Hysteresis. Leaving needs radius + margin; returning needs
        # radius - margin. A balloon sitting on the boundary keeps whichever
        # zone it already had.
        if current is Zone.CRUISE:
            if distance_m <= self.radius_m - self.hysteresis_m:
                return Zone.LAUNCH, "returned inside the launch radius"
            return Zone.CRUISE, "outside the launch radius"

        if distance_m >= self.radius_m + self.hysteresis_m:
            return Zone.CRUISE, "left the launch radius"

        return Zone.LAUNCH, "inside the launch radius"

    def _is_landed(
        self,
        altitude_agl_m: Optional[float],
        vertical_rate_mps: Optional[float],
        now: float,
    ) -> bool:
        """
        Low, not moving vertically, and has been for a while.

        The dwell requirement is what separates a landed payload from one
        floating at low altitude or briefly stalled during ascent. A false
        LANDED before apogee would stop image capture for the rest of the
        flight, so this is deliberately hard to trigger.
        """
        # Disarmed until the balloon has actually flown. A payload on the pad
        # is low and stationary for far longer than any dwell period, and
        # declaring it landed there would stop image capture before launch.
        if not self._landing_armed:
            return False

        if altitude_agl_m is None or vertical_rate_mps is None:
            self._landed_candidate_since = None
            return False

        stationary = (
            altitude_agl_m <= self.landed_altitude_m
            and abs(vertical_rate_mps) <= self.landed_vertical_rate_mps
        )

        if not stationary:
            if self._landed_candidate_since is not None:
                logger.debug("Landing candidate cleared: moving again")
            self._landed_candidate_since = None
            return False

        # Once LANDED, stay landed. A payload in a tree can register a little
        # vertical motion in the wind, and flapping back to CRUISE would cost
        # exactly the beacons the recovery crew needs.
        if self._state.zone is Zone.LANDED:
            return True

        if self._landed_candidate_since is None:
            self._landed_candidate_since = now
            logger.info(
                f"Possible landing: {altitude_agl_m:.0f} m AGL, "
                f"{vertical_rate_mps:+.2f} m/s; confirming over "
                f"{self.landed_dwell_sec:.0f}s"
            )
            return False

        return now - self._landed_candidate_since >= self.landed_dwell_sec

    # -- helpers -----------------------------------------------------------

    def _capture_launch_point(
        self, latitude: float, longitude: float, altitude_m: Optional[float]
    ) -> None:
        if not self.auto_capture_launch_point:
            return

        self.launch_latitude = latitude
        self.launch_longitude = longitude
        if self.launch_altitude_m is None:
            self.launch_altitude_m = altitude_m
        self._launch_point_captured = True

        logger.info(
            f"Launch point captured from first fix: "
            f"{latitude:.5f}, {longitude:.5f} at "
            f"{'unknown' if altitude_m is None else f'{altitude_m:.0f} m'} MSL"
        )

    def _altitude_agl(self, altitude_m: Optional[float]) -> Optional[float]:
        if altitude_m is None:
            return None
        if self.launch_altitude_m is None:
            return altitude_m
        return altitude_m - self.launch_altitude_m

    def _prune_altitude_history(self, now: float) -> None:
        """Drop samples older than the rate window."""
        cutoff = now - self.altitude_window_sec
        while len(self._altitude_history) > 2 and self._altitude_history[0][0] < cutoff:
            self._altitude_history.popleft()

    def _vertical_rate(self) -> Optional[float]:
        """
        Metres per second, from a least-squares fit over the altitude window.

        A simple first-and-last difference is dominated by GPS altitude noise,
        which is routinely several metres; a fit over the window is far
        steadier and is what makes the landed test trustworthy.
        """
        if len(self._altitude_history) < 3:
            return None

        times = [t for t, _ in self._altitude_history]
        altitudes = [a for _, a in self._altitude_history]

        span = times[-1] - times[0]
        if span < 1.0:
            return None

        n = len(times)
        mean_t = sum(times) / n
        mean_a = sum(altitudes) / n

        numerator = sum(
            (t - mean_t) * (a - mean_a) for t, a in zip(times, altitudes)
        )
        denominator = sum((t - mean_t) ** 2 for t in times)
        if denominator == 0:
            return None

        return numerator / denominator

    def set_launch_point(
        self, latitude: float, longitude: float, altitude_m: Optional[float] = None
    ) -> None:
        """Set the launch point explicitly, overriding any captured value."""
        self.launch_latitude = latitude
        self.launch_longitude = longitude
        if altitude_m is not None:
            self.launch_altitude_m = altitude_m
        self._launch_point_captured = True
        logger.info(
            f"Launch point set to {latitude:.5f}, {longitude:.5f}"
        )

    def get_status(self) -> dict:
        state = self._state
        return {
            "zone": state.zone.value,
            "reason": state.reason,
            "distance_from_launch_km": (
                round(state.distance_from_launch_m / 1000.0, 2)
                if state.distance_from_launch_m is not None
                else None
            ),
            "altitude_agl_m": (
                round(state.altitude_agl_m)
                if state.altitude_agl_m is not None
                else None
            ),
            "vertical_rate_mps": (
                round(state.vertical_rate_mps, 2)
                if state.vertical_rate_mps is not None
                else None
            ),
            "peak_altitude_m": round(self._peak_altitude_m),
            "peak_altitude_agl_m": round(self._peak_altitude_agl_m),
            "landing_armed": self._landing_armed,
            "launch_point_known": self._launch_point_captured,
            "launch_latitude": self.launch_latitude,
            "launch_longitude": self.launch_longitude,
            "radius_km": round(self.radius_m / 1000.0, 2),
        }
