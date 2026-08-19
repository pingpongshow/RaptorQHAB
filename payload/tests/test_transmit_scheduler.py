"""
Airtime allocation across images, Meshtastic, and idle.

The two properties that matter: the long-run ratios track the configured
budget, and the Meshtastic beacon interval is a floor that an image backlog
cannot push past. A balloon that stops beaconing because it has pictures to
send is a balloon nobody can find.
"""

import pytest

from airborne.transmit_scheduler import (
    Activity,
    DEFAULT_SCHEDULES,
    TransmitScheduler,
    ZoneSchedule,
    schedules_from_config,
)
from airborne.zone_manager import Zone


@pytest.fixture
def scheduler():
    return TransmitScheduler(slice_sec=1.0)


def run_for(scheduler, seconds, now=1000.0, beacon_cost_sec=3.0):
    """
    Drive the scheduler through simulated time and tally where airtime went.

    Beacon slices are charged more than their grant, because a real beacon
    cycle is several LongFast packets and overruns its slice -- exactly the
    behaviour the debt model has to absorb.
    """
    tally = {a: 0.0 for a in Activity}
    deadline = now + seconds

    while now < deadline:
        grant = scheduler.next_slice(now=now)
        actual = beacon_cost_sec if grant.activity is Activity.MESHTASTIC else grant.duration_sec

        tally[grant.activity] += actual
        scheduler.record(grant.activity, actual, now=now)
        if grant.activity is Activity.MESHTASTIC:
            scheduler.record_beacon(now=now)

        now += actual

    total = sum(tally.values())
    return {a: 100.0 * v / total for a, v in tally.items()}, now


# --- schedule validation --------------------------------------------------


def test_percentages_over_one_hundred_are_rejected():
    with pytest.raises(ValueError, match="exceeds 100%"):
        ZoneSchedule(image_percent=80.0, mesh_percent=30.0, beacon_interval_sec=60)


def test_negative_percentages_are_rejected():
    with pytest.raises(ValueError, match="negative"):
        ZoneSchedule(image_percent=-1.0, mesh_percent=10.0, beacon_interval_sec=60)


def test_a_non_positive_beacon_interval_is_rejected():
    with pytest.raises(ValueError, match="beacon interval"):
        ZoneSchedule(image_percent=50.0, mesh_percent=10.0, beacon_interval_sec=0)


def test_idle_is_the_remainder():
    schedule = ZoneSchedule(image_percent=5.0, mesh_percent=5.0, beacon_interval_sec=300)
    assert schedule.idle_percent == pytest.approx(90.0)


def test_default_schedules_match_the_flight_plan():
    launch = DEFAULT_SCHEDULES[Zone.LAUNCH]
    cruise = DEFAULT_SCHEDULES[Zone.CRUISE]
    landed = DEFAULT_SCHEDULES[Zone.LANDED]

    assert launch.image_percent == 98.0
    assert cruise.image_percent == 5.0
    assert cruise.mesh_percent == 5.0
    assert landed.image_percent == 0.0
    assert not landed.capture_enabled


# --- ratio tracking -------------------------------------------------------


def test_launch_zone_spends_almost_everything_on_images(scheduler):
    scheduler.set_zone(Zone.LAUNCH, now=1000.0)
    fractions, _ = run_for(scheduler, 4000.0)

    assert fractions[Activity.IMAGES] > 90.0
    assert fractions[Activity.MESHTASTIC] < 8.0


def test_cruise_zone_is_mostly_idle(scheduler):
    """The point of cruise: conserve battery for the descent."""
    scheduler.set_zone(Zone.CRUISE, now=1000.0)
    fractions, _ = run_for(scheduler, 6000.0)

    assert fractions[Activity.IDLE] > 80.0
    assert fractions[Activity.IMAGES] == pytest.approx(5.0, abs=3.0)


def test_landed_zone_never_schedules_images(scheduler):
    scheduler.set_zone(Zone.LANDED, now=1000.0)
    fractions, _ = run_for(scheduler, 3000.0)

    assert fractions[Activity.IMAGES] == 0.0
    assert fractions[Activity.MESHTASTIC] > 0.0


def test_landed_zone_disables_capture(scheduler):
    scheduler.set_zone(Zone.LANDED)
    assert not scheduler.capture_enabled

    scheduler.set_zone(Zone.CRUISE)
    assert scheduler.capture_enabled


def test_ratios_hold_even_when_slices_overrun():
    """
    A slice always finishes the packet it started, so overruns are normal.
    Charging the real duration is what keeps the long-run ratio honest.
    """
    scheduler = TransmitScheduler(slice_sec=1.0)
    scheduler.set_zone(Zone.CRUISE, now=1000.0)

    now = 1000.0
    tally = {a: 0.0 for a in Activity}
    for _ in range(2000):
        grant = scheduler.next_slice(now=now)
        # Every slice runs 50% long.
        actual = grant.duration_sec * 1.5
        tally[grant.activity] += actual
        scheduler.record(grant.activity, actual, now=now)
        if grant.activity is Activity.MESHTASTIC:
            scheduler.record_beacon(now=now)
        now += actual

    total = sum(tally.values())
    assert 100.0 * tally[Activity.IMAGES] / total == pytest.approx(5.0, abs=3.0)


# --- the beacon floor -----------------------------------------------------


def test_a_beacon_is_due_immediately_at_startup(scheduler):
    scheduler.set_zone(Zone.CRUISE, now=1000.0)
    grant = scheduler.next_slice(now=1000.0)
    assert grant.activity is Activity.MESHTASTIC
    assert grant.beacon_due


def test_the_beacon_interval_is_honoured(scheduler):
    scheduler.set_zone(Zone.CRUISE, now=1000.0)
    scheduler.record_beacon(now=1000.0)

    # Well before the 300 s interval, nothing forces a beacon.
    grants = [scheduler.next_slice(now=1000.0 + t) for t in range(0, 250, 10)]
    assert not any(g.beacon_due for g in grants)

    assert scheduler.next_slice(now=1000.0 + 301).beacon_due


def test_the_beacon_floor_beats_a_huge_image_backlog():
    """
    The property that matters most: a balloon with pictures to send must still
    beacon, or nobody can find it.
    """
    scheduler = TransmitScheduler(
        schedules={
            Zone.LAUNCH: ZoneSchedule(
                image_percent=99.0, mesh_percent=0.5, beacon_interval_sec=120.0
            )
        },
        slice_sec=1.0,
    )
    scheduler.set_zone(Zone.LAUNCH, now=1000.0)

    now = 1000.0
    beacon_times = []
    for _ in range(4000):
        grant = scheduler.next_slice(now=now)
        if grant.activity is Activity.MESHTASTIC:
            beacon_times.append(now)
            scheduler.record_beacon(now=now)
        scheduler.record(grant.activity, grant.duration_sec, now=now)
        now += grant.duration_sec

    assert len(beacon_times) >= 20, "beacons must keep flowing despite the backlog"

    gaps = [b - a for a, b in zip(beacon_times, beacon_times[1:])]
    assert max(gaps) < 130.0, f"a beacon gap of {max(gaps):.0f}s exceeds the interval"


def test_a_beacon_interval_the_budget_cannot_afford_is_flagged():
    """
    A 30 s interval on a 0.1% budget is a misconfiguration. The interval still
    wins -- being findable outranks the budget -- but the mismatch is counted
    so it shows up in status rather than silently distorting the ratios.
    """
    scheduler = TransmitScheduler(
        schedules={
            Zone.CRUISE: ZoneSchedule(
                image_percent=90.0, mesh_percent=0.1, beacon_interval_sec=30.0
            )
        },
        slice_sec=1.0,
    )
    scheduler.set_zone(Zone.CRUISE, now=1000.0)

    now = 1000.0
    for _ in range(500):
        grant = scheduler.next_slice(now=now)
        scheduler.record(grant.activity, 3.0, now=now)
        if grant.activity is Activity.MESHTASTIC:
            scheduler.record_beacon(now=now)
        now += 3.0

    assert scheduler.stats.beacons_forced > 0


def test_seconds_until_beacon_counts_down(scheduler):
    scheduler.set_zone(Zone.CRUISE, now=1000.0)
    scheduler.record_beacon(now=1000.0)

    assert scheduler.seconds_until_beacon(now=1000.0) == pytest.approx(300.0)
    assert scheduler.seconds_until_beacon(now=1150.0) == pytest.approx(150.0)
    assert scheduler.seconds_until_beacon(now=2000.0) == 0.0


# --- zone changes ---------------------------------------------------------


def test_changing_zone_switches_the_budget(scheduler):
    scheduler.set_zone(Zone.LAUNCH, now=1000.0)
    assert scheduler.schedule_for().image_percent == 98.0

    scheduler.set_zone(Zone.CRUISE, now=2000.0)
    assert scheduler.schedule_for().image_percent == 5.0


def test_zone_change_clears_accrued_debt(scheduler):
    """
    Entitlement earned under the launch budget is meaningless once cruise
    applies. Carrying it over would produce a burst of catch-up images exactly
    when the balloon should be conserving battery.
    """
    scheduler.set_zone(Zone.LAUNCH, now=1000.0)
    run_for(scheduler, 2000.0, now=1000.0)

    scheduler.set_zone(Zone.CRUISE, now=3000.0)
    fractions, _ = run_for(scheduler, 3000.0, now=3000.0)

    assert fractions[Activity.IMAGES] < 15.0, "no catch-up burst after the switch"


def test_setting_the_same_zone_is_a_no_op(scheduler):
    scheduler.set_zone(Zone.CRUISE, now=1000.0)
    scheduler.next_slice(now=1000.0)
    before = dict(scheduler._debt)

    scheduler.set_zone(Zone.CRUISE, now=1100.0)
    assert scheduler._debt == before


def test_unknown_zone_uses_the_launch_budget(scheduler):
    """Before the first fix the balloon is almost certainly still on the pad."""
    scheduler.set_zone(Zone.UNKNOWN, now=1000.0)
    assert scheduler.schedule_for().image_percent == 98.0


# --- robustness -----------------------------------------------------------


def test_a_long_stall_does_not_create_a_runaway_backlog(scheduler):
    """A debug pause must not be followed by an unbounded catch-up run."""
    scheduler.set_zone(Zone.CRUISE, now=1000.0)
    scheduler.next_slice(now=1000.0)

    scheduler.next_slice(now=1000.0 + 86400)  # a day later

    assert max(scheduler._debt.values()) <= 61.0


def test_slices_are_never_shorter_than_the_minimum():
    scheduler = TransmitScheduler(slice_sec=0.01, min_slice_sec=0.5)
    scheduler.set_zone(Zone.CRUISE, now=1000.0)
    assert scheduler.next_slice(now=1000.0).duration_sec >= 0.5


def test_a_zero_budget_activity_is_never_scheduled():
    scheduler = TransmitScheduler(
        schedules={
            Zone.CRUISE: ZoneSchedule(
                image_percent=0.0, mesh_percent=10.0, beacon_interval_sec=60.0
            )
        },
        slice_sec=1.0,
    )
    scheduler.set_zone(Zone.CRUISE, now=1000.0)

    fractions, _ = run_for(scheduler, 2000.0)
    assert fractions[Activity.IMAGES] == 0.0


# --- configuration --------------------------------------------------------


def test_schedules_are_built_from_config():
    from airborne.config import Config

    schedules = schedules_from_config(Config())

    assert schedules[Zone.LAUNCH].image_percent == 98.0
    assert schedules[Zone.CRUISE].image_percent == 5.0
    assert schedules[Zone.LANDED].image_percent == 0.0
    assert not schedules[Zone.LANDED].capture_enabled


def test_an_invalid_config_falls_back_to_defaults_rather_than_raising():
    """A bad percentage must not stop the payload from flying."""
    from airborne.config import Config

    config = Config()
    config.zone_cruise_image_percent = 80.0
    config.zone_cruise_mesh_percent = 80.0  # sums past 100

    schedules = schedules_from_config(config)
    assert schedules[Zone.CRUISE] == DEFAULT_SCHEDULES[Zone.CRUISE]


def test_unknown_zone_shares_the_launch_schedule():
    from airborne.config import Config

    schedules = schedules_from_config(Config())
    assert schedules[Zone.UNKNOWN] == schedules[Zone.LAUNCH]


# --- status ---------------------------------------------------------------


def test_status_reports_target_and_actual(scheduler):
    import json

    scheduler.set_zone(Zone.CRUISE, now=1000.0)
    run_for(scheduler, 3000.0)

    status = scheduler.get_status()
    json.dumps(status)

    assert status["zone"] == "cruise"
    assert status["target_image_percent"] == 5.0
    assert status["actual_image_percent"] == pytest.approx(5.0, abs=3.0)
    assert status["total_scheduled_sec"] > 0
