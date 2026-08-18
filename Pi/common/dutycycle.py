"""
Regional duty-cycle enforcement.

Several regions cap how much of any hour a transmitter may occupy. EU 868 and
EU 433 are both 10%. RaptorHAB already clamps transmit *power* to the regional
ceiling; without this it would respect the power limit and quietly breach the
airtime one, which is the same kind of violation.

The balloon is a worse offender than most equipment here. At altitude its
signal reaches a very large area, so exceeding a duty cycle affects a whole
region rather than one room.

The budget is a rolling window rather than a fixed hour. A fixed hour lets a
transmitter spend its entire allowance in the last minute of one hour and the
first minute of the next, which is 20% over the two minutes that matter.

Reserving before transmitting, and settling afterwards, keeps the accounting
honest when a transmission takes longer than predicted -- and it means a
transmission is refused *before* it goes out rather than apologised for after.
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

logger = logging.getLogger(__name__)

WINDOW_SEC = 3600.0


@dataclass
class DutyCycleStatus:
    """A snapshot of the budget, for telemetry and the configuration UI."""

    limit_percent: float
    used_percent: float
    used_sec: float
    budget_sec: float
    remaining_sec: float
    blocked: int

    @property
    def exhausted(self) -> bool:
        return self.remaining_sec <= 0


class DutyCycleTracker:
    """
    Rolling-window airtime accounting for one band.

    Thread-safe: the beacon scheduler, the repeater and any receive window can
    all reach the radio, and a budget that is only nearly enforced is not
    enforced.
    """

    def __init__(self, limit_percent: float = 100.0):
        self._limit_percent = max(0.0, min(100.0, limit_percent))
        self._events: Deque[Tuple[float, float]] = deque()   # (start, seconds)
        self._lock = threading.Lock()
        self._blocked = 0

    @property
    def limit_percent(self) -> float:
        return self._limit_percent

    @property
    def unlimited(self) -> bool:
        return self._limit_percent >= 100.0

    def set_limit(self, limit_percent: float) -> None:
        """
        Change the limit, starting a fresh window.

        A duty cycle is a limit on a *band*. Changing region changes the
        frequency the balloon transmits on, so airtime spent on 906 MHz over
        the US is not EU 868 airtime and must not be charged against the EU
        budget. Carrying it over would silence a balloon that had done nothing
        wrong in the region it just entered.

        The reverse loophole -- hopping regions to reset the counter -- is not
        reachable: region changes require a 3D fix, a dwell period and a
        margin inside the new boundary.
        """
        with self._lock:
            new_limit = max(0.0, min(100.0, limit_percent))
            if new_limit == self._limit_percent:
                return

            logger.info(
                f"Duty cycle limit {self._limit_percent:g}% -> {new_limit:g}%; "
                f"starting a fresh window for the new band"
            )
            self._limit_percent = new_limit
            self._events.clear()

    # -- accounting --------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_SEC
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _used_sec(self, now: float) -> float:
        self._prune(now)
        return sum(duration for _, duration in self._events)

    @property
    def budget_sec(self) -> float:
        return WINDOW_SEC * self._limit_percent / 100.0

    def can_transmit(self, airtime_sec: float, now: Optional[float] = None) -> bool:
        """Whether a transmission of this length fits the remaining budget."""
        if self.unlimited:
            return True
        now = time.time() if now is None else now
        with self._lock:
            return self._used_sec(now) + airtime_sec <= self.budget_sec

    def reserve(self, airtime_sec: float, now: Optional[float] = None) -> bool:
        """
        Book airtime up front, returning False if it does not fit.

        Reserving before transmitting rather than recording after is what
        makes this a limit instead of a report: a transmission that would
        breach the budget never leaves.

        Airtime is recorded even where no limit applies, so the status a
        flight reports is meaningful in every region rather than only the
        restricted ones.
        """
        now = time.time() if now is None else now

        if self.unlimited:
            with self._lock:
                self._prune(now)
                self._events.append((now, airtime_sec))
            return True

        with self._lock:
            if self._used_sec(now) + airtime_sec > self.budget_sec:
                self._blocked += 1
                remaining = self.budget_sec - self._used_sec(now)
                logger.warning(
                    f"Duty cycle: refusing a {airtime_sec * 1000:.0f} ms "
                    f"transmission; only {remaining * 1000:.0f} ms of the "
                    f"{self._limit_percent:g}% hourly budget remains"
                )
                return False

            self._events.append((now, airtime_sec))
            return True

    def settle(self, reserved_sec: float, actual_sec: float,
               now: Optional[float] = None) -> None:
        """
        Correct a reservation upward once the real duration is known.

        Only upward. The reservation is the calculated time-on-air, which is
        what the packet genuinely occupies the channel for; the measured value
        is how long our call took, which includes SPI and scheduling overhead
        and is not a better estimate of channel occupancy. Letting a
        measurement shorter than the calculation reduce the charge would let
        the budget drift under the true usage -- and on a fast host it would
        zero it entirely.

        Use release() when a transmission did not happen at all.
        """
        if actual_sec <= reserved_sec or not self._events:
            return

        with self._lock:
            for index in range(len(self._events) - 1, -1, -1):
                start, duration = self._events[index]
                if abs(duration - reserved_sec) < 1e-9:
                    self._events[index] = (start, actual_sec)
                    return

    def release(self, reserved_sec: float) -> None:
        """
        Give back a reservation for a transmission that did not happen.

        A failed transmit occupies no channel, and charging for it would
        silence the balloon over a fault it already suffered.
        """
        if not self._events:
            return

        with self._lock:
            for index in range(len(self._events) - 1, -1, -1):
                if abs(self._events[index][1] - reserved_sec) < 1e-9:
                    del self._events[index]
                    return

    def status(self, now: Optional[float] = None) -> DutyCycleStatus:
        now = time.time() if now is None else now
        with self._lock:
            used = self._used_sec(now)
            budget = self.budget_sec
            return DutyCycleStatus(
                limit_percent=self._limit_percent,
                used_percent=round(100.0 * used / WINDOW_SEC, 3),
                used_sec=round(used, 2),
                budget_sec=round(budget, 1),
                remaining_sec=round(max(0.0, budget - used), 2),
                blocked=self._blocked,
            )

    def get_status(self) -> dict:
        status = self.status()
        return {
            "limit_percent": status.limit_percent,
            "used_percent": status.used_percent,
            "used_sec": status.used_sec,
            "budget_sec": status.budget_sec,
            "remaining_sec": status.remaining_sec,
            "blocked": status.blocked,
            "enforced": not self.unlimited,
        }
