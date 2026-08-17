"""
Tracks which Meshtastic region the balloon is currently over.

A balloon at altitude drifts across borders, and each jurisdiction puts
Meshtastic on a different band. Transmitting on the wrong one means either
nobody local can hear you or you are outside what that country allows, so this
decides the active region and the transmit power ceiling that goes with it.

Behaviour, in priority order:

  1. Auto-switching is opt-in. With it off, the configured home region is used
     unconditionally and none of the rest applies.
  2. A region change requires a 3D fix. A 2D fix has no altitude and, more to
     the point, tends to be the fix you get when the receiver is confused.
  3. A change must persist for a dwell period and the position must be well
     inside the new region, not skimming its edge. Together these stop a
     balloon tracking a border from flapping between bands.
  4. If the position is in no known region, Meshtastic transmission STOPS.
     Guessing is the one thing that must not happen. Images and the RAPTOR
     downlink are unaffected.
  5. On GPS loss the last determined region is held (per Q1).
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from common.meshtastic.regions import (
    Region,
    distance_to_region_edge_km,
    get_region,
    region_for_position,
)

logger = logging.getLogger(__name__)


class RegionSource(str, Enum):
    """Why the active region is what it is."""

    CONFIGURED = "configured"      # auto-switching disabled, using home region
    HOME_DEFAULT = "home_default"  # auto on, but no fix yet
    GPS = "gps"                    # determined from position
    HELD = "held"                  # GPS lost, holding the last determination
    NONE = "none"                  # unknown territory, do not transmit


@dataclass
class RegionState:
    """The current region decision and how it was reached."""

    region: Optional[Region]
    source: RegionSource
    changed_at: float
    may_transmit: bool

    @property
    def code(self) -> str:
        return self.region.code if self.region else "NONE"


class RegionManager:
    """Decides the active Meshtastic region from GPS position."""

    def __init__(
        self,
        home_region_code: str = "US",
        auto_switch: bool = True,
        dwell_sec: float = 120.0,
        edge_margin_km: float = 25.0,
        require_3d_fix: bool = True,
    ):
        """
        Args:
            home_region_code: Region used when auto-switching is off, and
                before the first fix.
            auto_switch: Whether to follow the balloon's position.
            dwell_sec: How long a new region must be observed before adopting
                it.
            edge_margin_km: How far inside a new region the balloon must be
                before the change counts. Zero disables the margin test.
            require_3d_fix: Only act on a 3D fix.
        """
        self.auto_switch = auto_switch
        self.dwell_sec = dwell_sec
        self.edge_margin_km = edge_margin_km
        self.require_3d_fix = require_3d_fix

        self._home_region = get_region(home_region_code)
        if self._home_region is None:
            logger.error(
                f"Unknown home region {home_region_code!r}; falling back to US"
            )
            self._home_region = get_region("US")

        now = time.time()
        if auto_switch:
            self._state = RegionState(
                region=self._home_region,
                source=RegionSource.HOME_DEFAULT,
                changed_at=now,
                may_transmit=True,
            )
        else:
            self._state = RegionState(
                region=self._home_region,
                source=RegionSource.CONFIGURED,
                changed_at=now,
                may_transmit=True,
            )

        # Candidate region awaiting the dwell period.
        self._candidate: Optional[Region] = None
        self._candidate_since: float = 0.0
        self._last_gps_time: float = 0.0

    @property
    def state(self) -> RegionState:
        return self._state

    @property
    def region(self) -> Optional[Region]:
        return self._state.region

    @property
    def may_transmit(self) -> bool:
        """Whether Meshtastic transmission is permitted right now."""
        return self._state.may_transmit and self._state.region is not None

    def set_home_region(self, code: str) -> bool:
        """Change the home region. Returns False for an unknown code."""
        region = get_region(code)
        if region is None:
            logger.error(f"Unknown region code {code!r}")
            return False

        self._home_region = region
        if not self.auto_switch:
            self._adopt(region, RegionSource.CONFIGURED)
        return True

    def update(
        self,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        fix_type: int = 0,
        now: Optional[float] = None,
    ) -> RegionState:
        """
        Feed a GPS reading and get the resulting region decision.

        Args:
            latitude, longitude: Position in degrees, or None if no fix.
            fix_type: 0 none, 1 2D, 2 3D.
            now: Injectable clock, for tests.

        Returns:
            The current RegionState.
        """
        now = time.time() if now is None else now

        if not self.auto_switch:
            return self._state

        have_fix = (
            latitude is not None
            and longitude is not None
            and fix_type >= (2 if self.require_3d_fix else 1)
        )

        if not have_fix:
            self._handle_no_fix(now)
            return self._state

        self._last_gps_time = now

        observed = region_for_position(latitude, longitude)

        if observed is None:
            self._handle_unknown_territory(now)
            return self._state

        # Back inside known territory after being outside it.
        if self._state.region is None:
            self._candidate = None
            self._adopt(observed, RegionSource.GPS, now)
            return self._state

        if observed.code == self._state.region.code:
            # Confirms the current region; drop any pending candidate.
            if self._candidate is not None:
                logger.debug(
                    f"Region candidate {self._candidate.code} abandoned; back "
                    f"inside {observed.code}"
                )
                self._candidate = None
            if self._state.source is not RegionSource.GPS:
                self._adopt(observed, RegionSource.GPS, now)
            return self._state

        self._consider_change(observed, latitude, longitude, now)
        return self._state

    # -- internals ---------------------------------------------------------

    def _consider_change(
        self, observed: Region, latitude: float, longitude: float, now: float
    ) -> None:
        """Apply the edge-margin and dwell tests before adopting a new region."""
        if self.edge_margin_km > 0:
            margin = distance_to_region_edge_km(latitude, longitude)
            if margin is not None and margin < self.edge_margin_km:
                logger.debug(
                    f"Observed {observed.code} but only {margin:.0f} km inside "
                    f"its boundary (need {self.edge_margin_km:.0f} km); holding "
                    f"{self._state.code}"
                )
                return

        if self._candidate is None or self._candidate.code != observed.code:
            self._candidate = observed
            self._candidate_since = now
            logger.info(
                f"Region change candidate: {self._state.code} -> {observed.code}, "
                f"confirming over {self.dwell_sec:.0f}s"
            )
            return

        if now - self._candidate_since >= self.dwell_sec:
            self._adopt(observed, RegionSource.GPS, now)
            self._candidate = None

    def _handle_no_fix(self, now: float) -> None:
        """
        GPS lost: hold the last determined region (Q1).

        The candidate is discarded, because a half-confirmed change should not
        survive a gap in position data.
        """
        if self._candidate is not None:
            logger.debug("GPS lost; discarding pending region change")
            self._candidate = None

        if self._state.source is RegionSource.GPS:
            self._state = RegionState(
                region=self._state.region,
                source=RegionSource.HELD,
                changed_at=self._state.changed_at,
                may_transmit=self._state.may_transmit,
            )
            logger.info(f"GPS lost; holding region {self._state.code}")

    def _handle_unknown_territory(self, now: float) -> None:
        """
        Position is in no known region: stop transmitting Meshtastic.

        Over open ocean or a country with no entry in the table, there is no
        correct frequency, and picking one anyway risks transmitting outside
        what is permitted. Silence is the right answer. The image downlink and
        RAPTOR telemetry are untouched.
        """
        if self._state.source is RegionSource.NONE:
            return

        self._candidate = None
        logger.warning(
            f"Position is outside every known Meshtastic region (was "
            f"{self._state.code}); suspending Meshtastic transmission"
        )
        self._state = RegionState(
            region=None,
            source=RegionSource.NONE,
            changed_at=now,
            may_transmit=False,
        )

    def _adopt(
        self, region: Region, source: RegionSource, now: Optional[float] = None
    ) -> None:
        now = time.time() if now is None else now
        previous = self._state.code

        self._state = RegionState(
            region=region,
            source=source,
            changed_at=now,
            may_transmit=True,
        )

        if previous != region.code:
            logger.info(
                f"Meshtastic region {previous} -> {region.code} "
                f"({region.description}, {region.freq_start_mhz}-"
                f"{region.freq_end_mhz} MHz, max {region.power_limit_dbm} dBm)"
            )

    def get_status(self) -> dict:
        """Region status for logs, telemetry, and the configuration UI."""
        region = self._state.region
        return {
            "region": self._state.code,
            "source": self._state.source.value,
            "may_transmit": self.may_transmit,
            "auto_switch": self.auto_switch,
            "home_region": self._home_region.code if self._home_region else None,
            "power_limit_dbm": region.power_limit_dbm if region else None,
            "duty_cycle_percent": region.duty_cycle_percent if region else None,
            "pending_candidate": self._candidate.code if self._candidate else None,
        }
