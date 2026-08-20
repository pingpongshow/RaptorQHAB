"""Recover a receiver that has stopped producing fixes.

GPS modules wedge. They come up, work, and then sit there emitting sentences
with no fix -- or emit nothing at all -- until something resets them. Nothing
in the flight software can fix that by asking politely, and a payload with a
wedged receiver is a payload nobody can find.

This board has no GPIO control over the receiver's power, so the escalation
is what can actually be done over the wire, cheapest first:

  1. Re-apply the configuration. Covers a receiver that lost its settings.
  2. Hot restart (PMTK101) -- keeps almanac and ephemeris, fixes in seconds.
  3. Cold start (PMTK103) -- discards everything, minutes to re-acquire, and
     the last resort short of hardware.

Each step gets time to work before the next is tried, because a cold start
issued impatiently costs more fix time than the fault it was meant to cure.
The escalation resets the moment a fix returns.
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GPSWatchdogStats:
    reconfigures: int = 0
    hot_restarts: int = 0
    cold_starts: int = 0
    recoveries: int = 0


class GPSWatchdog:
    """Escalating recovery for a receiver that has stopped fixing."""

    def __init__(
        self,
        gps,
        no_fix_timeout_sec: float = 300.0,
        escalation_interval_sec: float = 180.0,
        enabled: bool = True,
    ):
        """
        Args:
            gps: the GPSReader to act on
            no_fix_timeout_sec: how long without a fix before acting. Long by
                design -- a balloon under canopy or in heavy cloud can lose
                its fix for minutes without anything being wrong, and
                restarting the receiver then makes the outage worse.
            escalation_interval_sec: how long each step gets to work.
        """
        self.gps = gps
        self.no_fix_timeout_sec = no_fix_timeout_sec
        self.escalation_interval_sec = escalation_interval_sec
        self.enabled = enabled
        self.stats = GPSWatchdogStats()

        self._last_fix_at: Optional[float] = None
        self._last_action_at: Optional[float] = None
        self._stage = 0

    @property
    def stage(self) -> int:
        """0 = healthy, 1..3 = the recovery step last taken."""
        return self._stage

    def update(self, has_fix: bool, now: Optional[float] = None) -> Optional[str]:
        """Feed the current fix state. Returns the action taken, if any."""
        now = time.monotonic() if now is None else now

        if has_fix:
            if self._stage:
                logger.info(
                    f"GPS fix recovered after {self._stage} recovery "
                    f"step(s); watchdog reset"
                )
                self.stats.recoveries += 1
            self._last_fix_at = now
            self._last_action_at = None
            self._stage = 0
            return None

        if not self.enabled:
            return None

        if self._last_fix_at is None:
            # No fix has ever been seen. Time out from process start rather
            # than never acting: a receiver wedged before the first fix is
            # exactly the case where a restart helps most.
            self._last_fix_at = now
            return None

        if now - self._last_fix_at < self.no_fix_timeout_sec:
            return None

        if (self._last_action_at is not None
                and now - self._last_action_at < self.escalation_interval_sec):
            return None

        return self._escalate(now)

    def _escalate(self, now: float) -> Optional[str]:
        self._last_action_at = now
        self._stage = min(self._stage + 1, 3)

        try:
            if self._stage == 1:
                logger.warning(
                    f"No GPS fix for {self.no_fix_timeout_sec:.0f}s; "
                    f"re-applying receiver configuration"
                )
                self.gps.configure()
                self.stats.reconfigures += 1
                return "reconfigure"

            if self._stage == 2:
                logger.warning("Still no fix; hot-restarting the receiver")
                self.gps.restart(cold=False)
                self.stats.hot_restarts += 1
                return "hot_restart"

            logger.warning("Still no fix; cold-starting the receiver")
            self.gps.restart(cold=True)
            self.stats.cold_starts += 1
            return "cold_start"

        except Exception as exc:
            # A watchdog that raises is worse than one that does nothing.
            logger.error(f"GPS recovery step {self._stage} failed: {exc}")
            return None

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "stage": self._stage,
            "reconfigures": self.stats.reconfigures,
            "hot_restarts": self.stats.hot_restarts,
            "cold_starts": self.stats.cold_starts,
            "recoveries": self.stats.recoveries,
        }
