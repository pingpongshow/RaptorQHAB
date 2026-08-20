"""
Allocates the balloon's airtime between images, Meshtastic, and idle.

Each flight zone gets a budget expressed as percentages of wall-clock time:

    LAUNCH   images 98%, mesh 1%, idle 1%    -- recovery crew is right here
    CRUISE   images  5%, mesh 5%, idle 90%   -- conserve battery, stay findable
    LANDED   images  0%, mesh 5%, idle 95%   -- beacons are what find the payload

Two rules matter more than the percentages themselves:

  1. The Meshtastic beacon interval is a hard floor, not a target. If the
     image queue is backed up, images yield. A balloon that stops beaconing
     because it has pictures to send is a balloon nobody can find.

  2. A slice boundary never interrupts a packet in flight. The scheduler hands
     out permission to transmit for a bounded period; the caller finishes
     whatever it started.

The allocator is deliberately time-based rather than packet-based. Packets
differ enormously in airtime -- a GFSK image packet is a couple of
milliseconds, a LongFast beacon is several hundred -- so "5% of packets" and
"5% of airtime" are wildly different things, and airtime is what actually
costs battery and occupies the channel.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

from airborne.zone_manager import Zone

logger = logging.getLogger(__name__)

# Elapsed time here comes from the monotonic clock, never the wall clock. The
# payload runs on a Pi with no RTC: it boots with whatever fake-hwclock saved
# and systemd-timesyncd later steps the clock, sometimes by months. Every
# interval in this module -- dwell timers, rolling windows, beacon spacing --
# would be wrong across that step, and a step backwards would stall them for
# the length of the jump.


class Activity(str, Enum):
    """What the payload should be doing right now."""

    IMAGES = "images"
    MESHTASTIC = "meshtastic"
    LISTEN = "listen"
    IDLE = "idle"


@dataclass(frozen=True)
class ZoneSchedule:
    """Airtime budget and beacon cadence for one zone."""

    image_percent: float
    mesh_percent: float
    beacon_interval_sec: float
    capture_enabled: bool = True
    listen_percent: float = 0.0

    def __post_init__(self):
        if min(self.image_percent, self.mesh_percent, self.listen_percent) < 0:
            raise ValueError("percentages cannot be negative")
        total = self.image_percent + self.mesh_percent + self.listen_percent
        if total > 100.0:
            raise ValueError(
                f"image {self.image_percent}% + mesh {self.mesh_percent}% + "
                f"listen {self.listen_percent}% exceeds 100% of airtime"
            )
        if self.beacon_interval_sec <= 0:
            raise ValueError("beacon interval must be positive")

    @property
    def idle_percent(self) -> float:
        return 100.0 - self.image_percent - self.mesh_percent - self.listen_percent


DEFAULT_SCHEDULES: Dict[Zone, ZoneSchedule] = {
    Zone.UNKNOWN: ZoneSchedule(
        image_percent=98.0, mesh_percent=1.0, beacon_interval_sec=600.0
    ),
    Zone.LAUNCH: ZoneSchedule(
        image_percent=98.0, mesh_percent=1.0, beacon_interval_sec=600.0
    ),
    Zone.CRUISE: ZoneSchedule(
        image_percent=5.0, mesh_percent=5.0, beacon_interval_sec=300.0
    ),
    Zone.LANDED: ZoneSchedule(
        image_percent=0.0,
        mesh_percent=5.0,
        beacon_interval_sec=60.0,
        capture_enabled=False,
    ),
}


@dataclass
class SchedulerStats:
    """Where the airtime actually went, per activity."""

    image_sec: float = 0.0
    mesh_sec: float = 0.0
    listen_sec: float = 0.0
    idle_sec: float = 0.0
    beacons_due: int = 0
    beacons_forced: int = 0

    @property
    def total_sec(self) -> float:
        return self.image_sec + self.mesh_sec + self.listen_sec + self.idle_sec

    def fractions(self) -> Dict[str, float]:
        total = self.total_sec
        if total <= 0:
            return {"images": 0.0, "meshtastic": 0.0, "listen": 0.0, "idle": 0.0}
        return {
            "images": 100.0 * self.image_sec / total,
            "meshtastic": 100.0 * self.mesh_sec / total,
            "listen": 100.0 * self.listen_sec / total,
            "idle": 100.0 * self.idle_sec / total,
        }


@dataclass
class Slice:
    """One grant of airtime to a single activity."""

    activity: Activity
    duration_sec: float
    zone: Zone
    beacon_due: bool = False


class TransmitScheduler:
    """
    Hands out bounded slices of airtime according to the active zone.

    Uses a debt model rather than a fixed rotation: each activity accrues
    entitlement in proportion to its share of elapsed wall-clock time, and the
    activity furthest behind its entitlement goes next. That keeps the
    long-run ratios right even when slices run long -- which they will, since
    a slice always finishes the packet it started.
    """

    def __init__(
        self,
        schedules: Optional[Dict[Zone, ZoneSchedule]] = None,
        slice_sec: float = 2.0,
        min_slice_sec: float = 0.25,
    ):
        """
        Args:
            schedules: Per-zone budgets. Defaults to DEFAULT_SCHEDULES.
            slice_sec: Nominal length of one grant. Shorter interleaves more
                finely at the cost of more radio mode switches.
            min_slice_sec: Never grant less than this; a slice shorter than
                the mode-switch cost is pure overhead.
        """
        self.schedules = dict(schedules or DEFAULT_SCHEDULES)
        self.slice_sec = slice_sec
        self.min_slice_sec = min_slice_sec

        self._zone = Zone.UNKNOWN
        self._debt: Dict[Activity, float] = {a: 0.0 for a in Activity}
        self._last_beacon_time: Optional[float] = None
        self._last_tick: Optional[float] = None

        self.stats = SchedulerStats()

    # -- configuration -----------------------------------------------------

    def set_zone(self, zone: Zone, now: Optional[float] = None) -> None:
        """
        Change the active zone.

        Debt is reset, because entitlement accrued under the launch-zone
        budget is meaningless once the cruise budget applies -- carrying it
        over would produce a burst of catch-up images exactly when the balloon
        should be conserving battery.
        """
        if zone is self._zone:
            return

        logger.info(
            f"Transmit schedule {self._zone.value} -> {zone.value}: "
            f"{self.schedule_for(zone).image_percent:.0f}% images, "
            f"{self.schedule_for(zone).mesh_percent:.0f}% mesh, "
            f"beacon every {self.schedule_for(zone).beacon_interval_sec:.0f}s"
        )
        self._zone = zone
        self._debt = {a: 0.0 for a in Activity}
        self._last_tick = now

    def schedule_for(self, zone: Optional[Zone] = None) -> ZoneSchedule:
        zone = zone or self._zone
        return self.schedules.get(zone, DEFAULT_SCHEDULES[Zone.LAUNCH])

    @property
    def zone(self) -> Zone:
        return self._zone

    @property
    def capture_enabled(self) -> bool:
        """Whether image capture should be running at all in this zone."""
        return self.schedule_for().capture_enabled

    # -- the allocator -----------------------------------------------------

    def next_slice(self, now: Optional[float] = None) -> Slice:
        """
        Decide what the payload should do next, and for how long.

        The beacon floor is checked first: if a beacon is overdue it wins
        regardless of accrued debt, because being findable outranks any
        image backlog.
        """
        now = time.monotonic() if now is None else now
        schedule = self.schedule_for()

        self._accrue(now, schedule)

        if self._beacon_overdue(now, schedule):
            self.stats.beacons_due += 1
            if self._debt[Activity.MESHTASTIC] <= 0:
                # Overdue despite having no accrued entitlement: the budget is
                # too small for the configured interval. Honour the interval
                # anyway and record it, so the mismatch is visible.
                self.stats.beacons_forced += 1
            return Slice(
                activity=Activity.MESHTASTIC,
                duration_sec=max(self.min_slice_sec, self.slice_sec),
                zone=self._zone,
                beacon_due=True,
            )

        activity = self._most_indebted(schedule)
        duration = max(self.min_slice_sec, self.slice_sec)

        return Slice(activity=activity, duration_sec=duration, zone=self._zone)

    def _accrue(self, now: float, schedule: ZoneSchedule) -> None:
        """Grant each activity entitlement proportional to elapsed time."""
        if self._last_tick is None:
            self._last_tick = now
            return

        elapsed = now - self._last_tick
        self._last_tick = now

        if elapsed <= 0:
            return

        # A long gap (a stall, a debug pause) should not create a huge backlog
        # that then runs unchecked.
        elapsed = min(elapsed, 60.0)

        self._debt[Activity.IMAGES] += elapsed * schedule.image_percent / 100.0
        self._debt[Activity.MESHTASTIC] += elapsed * schedule.mesh_percent / 100.0
        self._debt[Activity.LISTEN] += elapsed * schedule.listen_percent / 100.0
        self._debt[Activity.IDLE] += elapsed * schedule.idle_percent / 100.0

    def _most_indebted(self, schedule: ZoneSchedule) -> Activity:
        """
        The activity owed the most airtime.

        Activities with a zero budget are skipped entirely, so a LANDED
        payload never gets handed an image slice.
        """
        budgets = {
            Activity.IMAGES: schedule.image_percent,
            Activity.MESHTASTIC: schedule.mesh_percent,
            Activity.LISTEN: schedule.listen_percent,
            Activity.IDLE: schedule.idle_percent,
        }
        eligible = [a for a in Activity if budgets[a] > 0]
        if not eligible:
            return Activity.IDLE

        return max(eligible, key=lambda a: self._debt[a])

    def _beacon_overdue(self, now: float, schedule: ZoneSchedule) -> bool:
        if self._last_beacon_time is None:
            return True
        return now - self._last_beacon_time >= schedule.beacon_interval_sec

    # -- accounting --------------------------------------------------------

    def record(
        self, activity: Activity, actual_sec: float, now: Optional[float] = None
    ) -> None:
        """
        Report how long a slice actually took.

        Slices routinely overrun their grant, because a packet in flight is
        always allowed to finish. Charging the real duration is what keeps the
        long-run ratios honest.
        """
        now = time.monotonic() if now is None else now
        actual_sec = max(0.0, actual_sec)

        self._debt[activity] -= actual_sec

        if activity is Activity.IMAGES:
            self.stats.image_sec += actual_sec
        elif activity is Activity.MESHTASTIC:
            self.stats.mesh_sec += actual_sec
        elif activity is Activity.LISTEN:
            self.stats.listen_sec += actual_sec
        else:
            self.stats.idle_sec += actual_sec

    def record_beacon(self, now: Optional[float] = None) -> None:
        """Note that a beacon cycle went out, restarting the interval."""
        self._last_beacon_time = time.monotonic() if now is None else now

    def seconds_until_beacon(self, now: Optional[float] = None) -> float:
        """How long until the next beacon is due. Zero means now."""
        now = time.monotonic() if now is None else now
        if self._last_beacon_time is None:
            return 0.0
        remaining = (
            self.schedule_for().beacon_interval_sec - (now - self._last_beacon_time)
        )
        return max(0.0, remaining)

    def get_status(self) -> dict:
        schedule = self.schedule_for()
        fractions = self.stats.fractions()
        return {
            "zone": self._zone.value,
            "target_image_percent": schedule.image_percent,
            "target_mesh_percent": schedule.mesh_percent,
            "target_listen_percent": schedule.listen_percent,
            "actual_listen_percent": round(fractions["listen"], 1),
            "target_idle_percent": schedule.idle_percent,
            "actual_image_percent": round(fractions["images"], 1),
            "actual_mesh_percent": round(fractions["meshtastic"], 1),
            "actual_idle_percent": round(fractions["idle"], 1),
            "beacon_interval_sec": schedule.beacon_interval_sec,
            "seconds_until_beacon": round(self.seconds_until_beacon(), 1),
            "capture_enabled": schedule.capture_enabled,
            "beacons_due": self.stats.beacons_due,
            "beacons_forced": self.stats.beacons_forced,
            "total_scheduled_sec": round(self.stats.total_sec, 1),
        }


def schedules_from_config(config) -> Dict[Zone, ZoneSchedule]:
    """
    Build the per-zone budgets from an AirborneConfig.

    Invalid combinations fall back to the defaults for that zone with a loud
    log line, rather than raising: a bad percentage in the config file must
    not stop the payload from flying.
    """
    def build(zone: Zone, image_pct, mesh_pct, interval, capture=True,
              listen=0.0) -> ZoneSchedule:
        try:
            return ZoneSchedule(
                image_percent=float(image_pct),
                mesh_percent=float(mesh_pct),
                beacon_interval_sec=float(interval),
                capture_enabled=capture,
                listen_percent=float(listen),
            )
        except ValueError as e:
            logger.error(
                f"Invalid {zone.value} schedule ({image_pct}% images, "
                f"{mesh_pct}% mesh): {e}. Using defaults."
            )
            return DEFAULT_SCHEDULES[zone]

    launch = build(
        Zone.LAUNCH,
        config.zone_launch_image_percent,
        config.zone_launch_mesh_percent,
        config.zone_launch_beacon_interval_sec,
    )
    cruise = build(
        Zone.CRUISE,
        config.zone_cruise_image_percent,
        config.zone_cruise_mesh_percent,
        config.zone_cruise_beacon_interval_sec,
        # Listening serves two features in cruise. Whichever wants more airtime
        # sets the budget; the window is shared, so a packet heard is available
        # to the repeater and to the mesh log alike.
        listen=max(
            config.repeater_rx_percent if config.repeater_enabled else 0.0,
            (config.mesh_log_rx_percent if config.mesh_log_enabled else 0.0),
        ),
    )
    landed = build(
        Zone.LANDED,
        0.0,
        config.zone_landed_mesh_percent,
        config.zone_landed_beacon_interval_sec,
        capture=False,
        # Listening matters most once it is down. This is the moment somebody
        # wants to ask the balloon where it is, and with imagery off there is
        # nothing to trade away -- the time comes out of idle, so it costs
        # battery and nothing else.
        listen=(config.repeater_rx_percent if config.repeater_enabled else 0.0),
    )

    return {
        Zone.UNKNOWN: launch,  # before the first fix, assume still on the pad
        Zone.LAUNCH: launch,
        Zone.CRUISE: cruise,
        Zone.LANDED: landed,
    }
