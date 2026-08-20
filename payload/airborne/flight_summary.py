"""Running record of the whole flight, for the summary packet.

Telemetry answers "where is it now". This answers "what has this flight
been" -- apogee and when, how fast it rose and fell, how far it went, how
cold it got. One summary packet received late in the flight, or relayed off
the mesh by a stranger, carries the story even when every other packet was
missed. That is the situation recovery actually is.

Everything here is derived from readings the payload already takes, so the
cost is a few floats and one packet every few minutes.
"""
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

EARTH_RADIUS_M = 6371000.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


@dataclass
class FlightSummary:
    """Accumulates flight-scale facts from ordinary readings."""

    max_altitude_m: float = 0.0
    max_altitude_time: int = 0
    max_ascent_rate_mps: float = 0.0
    max_descent_rate_mps: float = 0.0
    distance_travelled_m: float = 0.0
    min_cpu_temp_c: Optional[float] = None
    max_cpu_temp_c: Optional[float] = None
    started_at: Optional[float] = None

    _last_lat: Optional[float] = None
    _last_lon: Optional[float] = None

    # Ignore jumps larger than this between consecutive fixes. A GPS
    # glitch that teleports the payload a hundred kilometres would
    # otherwise be added to the odometer permanently -- the one number
    # here that cannot correct itself later.
    max_step_m: float = 20000.0

    def note_position(self, latitude: float, longitude: float,
                      altitude_m: Optional[float], vertical_rate_mps: Optional[float],
                      now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        if self.started_at is None:
            self.started_at = now

        if altitude_m is not None and altitude_m > self.max_altitude_m:
            self.max_altitude_m = altitude_m
            self.max_altitude_time = int(now)

        if vertical_rate_mps is not None:
            if vertical_rate_mps > self.max_ascent_rate_mps:
                self.max_ascent_rate_mps = vertical_rate_mps
            if -vertical_rate_mps > self.max_descent_rate_mps:
                self.max_descent_rate_mps = -vertical_rate_mps

        if self._last_lat is not None:
            step = _haversine_m(self._last_lat, self._last_lon,
                                latitude, longitude)
            if step <= self.max_step_m:
                self.distance_travelled_m += step
            else:
                logger.debug(
                    f"Ignoring a {step / 1000:.0f} km jump in the odometer")
        self._last_lat, self._last_lon = latitude, longitude

    def note_temperature(self, cpu_temp_c: Optional[float]) -> None:
        if cpu_temp_c is None:
            return
        if self.min_cpu_temp_c is None or cpu_temp_c < self.min_cpu_temp_c:
            self.min_cpu_temp_c = cpu_temp_c
        if self.max_cpu_temp_c is None or cpu_temp_c > self.max_cpu_temp_c:
            self.max_cpu_temp_c = cpu_temp_c

    def flight_time_sec(self, now: Optional[float] = None) -> int:
        if self.started_at is None:
            return 0
        return int((time.time() if now is None else now) - self.started_at)

    def as_payload(self, packets_sent: int, images_captured: int,
                   zone_index: int, now: Optional[float] = None):
        """Build the wire payload."""
        from common.protocol import FlightSummaryPayload
        return FlightSummaryPayload(
            max_altitude_m=self.max_altitude_m,
            max_altitude_time=self.max_altitude_time,
            max_ascent_rate_mps=self.max_ascent_rate_mps,
            max_descent_rate_mps=self.max_descent_rate_mps,
            distance_travelled_m=self.distance_travelled_m,
            min_cpu_temp_c=self.min_cpu_temp_c or 0.0,
            max_cpu_temp_c=self.max_cpu_temp_c or 0.0,
            packets_sent=packets_sent,
            images_captured=images_captured,
            flight_time_sec=self.flight_time_sec(now),
            zone=zone_index,
        )
