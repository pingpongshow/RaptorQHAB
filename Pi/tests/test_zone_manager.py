"""
Flight zone detection.

The expensive mistakes here are a false LANDED before apogee, which stops
image capture for the rest of the flight, and zone thrashing at a boundary,
which costs a radio reconfiguration every time.
"""

import pytest

from airborne.zone_manager import Zone, ZoneManager, haversine_m

LAUNCH = (39.7392, -104.9903)  # Denver
FIX_3D = 2
FIX_2D = 1
NO_FIX = 0


def at_distance(km, bearing_east=True):
    """A point roughly `km` east (or north) of the launch site."""
    import math

    if bearing_east:
        lon_offset = km / (111.32 * math.cos(math.radians(LAUNCH[0])))
        return LAUNCH[0], LAUNCH[1] + lon_offset
    return LAUNCH[0] + km / 111.32, LAUNCH[1]


@pytest.fixture
def manager():
    return ZoneManager(
        launch_latitude=LAUNCH[0],
        launch_longitude=LAUNCH[1],
        launch_altitude_m=1609.0,
        radius_m=8000.0,
        hysteresis_m=800.0,
        altitude_override_m=3000.0,
        landed_altitude_m=1000.0,
        landed_vertical_rate_mps=0.5,
        landed_dwell_sec=120.0,
        landed_arm_altitude_m=2000.0,
    )


def fly_to_altitude(manager, agl_m=5000.0, start_time=500.0):
    """
    Arm landing detection by actually flying.

    Landing detection is disarmed until the balloon has been high, so any test
    about landing has to get it airborne first.
    """
    now = start_time
    base = manager.launch_altitude_m or 0.0
    for altitude in range(0, int(agl_m) + 1, 500):
        manager.update(*LAUNCH, altitude_m=base + altitude, fix_type=FIX_3D, now=now)
        now += 10.0
    return now


def climb(manager, start_alt, rate_mps, seconds, step=10.0, start_time=1000.0,
          position=None):
    """Feed a steady climb or descent and return the final state."""
    latitude, longitude = position or LAUNCH
    now = start_time
    altitude = start_alt
    state = manager.state
    for _ in range(int(seconds / step)):
        state = manager.update(latitude, longitude, altitude, FIX_3D, now=now)
        now += step
        altitude += rate_mps * step
    return state


# --- distance -------------------------------------------------------------


def test_haversine_is_accurate_over_short_distances():
    """Denver to a point 10 km east."""
    lat, lon = at_distance(10.0)
    assert haversine_m(LAUNCH[0], LAUNCH[1], lat, lon) == pytest.approx(10000, rel=0.01)


def test_haversine_is_zero_at_the_same_point():
    assert haversine_m(*LAUNCH, *LAUNCH) == pytest.approx(0.0, abs=0.1)


def test_haversine_handles_the_antimeridian():
    assert haversine_m(0.0, 179.99, 0.0, -179.99) < 3000


# --- launch zone ----------------------------------------------------------


def test_starts_unknown_before_any_fix(manager):
    assert manager.zone is Zone.UNKNOWN


def test_on_the_pad_is_the_launch_zone(manager):
    state = manager.update(*LAUNCH, altitude_m=1609.0, fix_type=FIX_3D, now=1000.0)
    assert state.zone is Zone.LAUNCH


def test_inside_the_radius_stays_in_the_launch_zone(manager):
    lat, lon = at_distance(5.0)
    state = manager.update(lat, lon, altitude_m=1700.0, fix_type=FIX_3D, now=1000.0)
    assert state.zone is Zone.LAUNCH
    assert state.distance_from_launch_m == pytest.approx(5000, rel=0.02)


def test_a_fix_without_3d_is_ignored(manager):
    manager.update(*LAUNCH, altitude_m=1609.0, fix_type=FIX_3D, now=1000.0)
    lat, lon = at_distance(50.0)
    state = manager.update(lat, lon, altitude_m=1700.0, fix_type=FIX_2D, now=2000.0)
    assert state.zone is Zone.LAUNCH, "a 2D fix must not drive a zone change"


def test_no_fix_holds_the_current_zone(manager):
    """Q1: hold last known."""
    lat, lon = at_distance(50.0)
    manager.update(lat, lon, altitude_m=2000.0, fix_type=FIX_3D, now=1000.0)
    assert manager.zone is Zone.CRUISE

    state = manager.update(fix_type=NO_FIX, now=2000.0)
    assert state.zone is Zone.CRUISE


# --- hysteresis -----------------------------------------------------------


def test_leaving_requires_clearing_the_radius_plus_hysteresis(manager):
    manager.update(*LAUNCH, altitude_m=1609.0, fix_type=FIX_3D, now=1000.0)

    # 8.5 km: past the radius, but inside the 800 m margin.
    lat, lon = at_distance(8.5)
    assert manager.update(lat, lon, 1700.0, FIX_3D, now=1100.0).zone is Zone.LAUNCH

    # 9.0 km: clear of radius + hysteresis.
    lat, lon = at_distance(9.0)
    assert manager.update(lat, lon, 1700.0, FIX_3D, now=1200.0).zone is Zone.CRUISE


def test_returning_requires_coming_back_inside_the_margin(manager):
    lat, lon = at_distance(20.0)
    manager.update(lat, lon, 1700.0, FIX_3D, now=1000.0)
    assert manager.zone is Zone.CRUISE

    # 7.5 km: inside the radius but not yet inside radius - hysteresis.
    lat, lon = at_distance(7.5)
    assert manager.update(lat, lon, 1700.0, FIX_3D, now=1100.0).zone is Zone.CRUISE

    # 7.0 km: inside the margin.
    lat, lon = at_distance(7.0)
    assert manager.update(lat, lon, 1700.0, FIX_3D, now=1200.0).zone is Zone.LAUNCH


def test_drifting_along_the_boundary_does_not_thrash(manager):
    """Each zone change costs a radio reconfiguration."""
    manager.update(*LAUNCH, altitude_m=1609.0, fix_type=FIX_3D, now=1000.0)

    changes = 0
    previous = manager.zone
    now = 1100.0
    # Climbing slowly, so this is drift rather than a stationary payload.
    altitude = 1700.0
    for distance in (7.9, 8.1, 7.9, 8.2, 8.0, 8.3, 7.8, 8.1):
        lat, lon = at_distance(distance)
        zone = manager.update(lat, lon, altitude, FIX_3D, now=now).zone
        if zone is not previous:
            changes += 1
            previous = zone
        now += 60.0
        altitude += 60.0

    assert changes == 0, "hysteresis should absorb boundary wobble entirely"


# --- altitude override ----------------------------------------------------


def test_altitude_forces_cruise_even_directly_overhead(manager):
    """A balloon at 3 km is no longer a launch-site problem."""
    state = manager.update(*LAUNCH, altitude_m=1609.0 + 3500.0, fix_type=FIX_3D,
                           now=1000.0)
    assert state.zone is Zone.CRUISE
    assert "above" in state.reason


def test_below_the_altitude_override_stays_in_the_launch_zone(manager):
    state = manager.update(*LAUNCH, altitude_m=1609.0 + 2000.0, fix_type=FIX_3D,
                           now=1000.0)
    assert state.zone is Zone.LAUNCH


def test_altitude_override_can_be_disabled():
    manager = ZoneManager(
        launch_latitude=LAUNCH[0], launch_longitude=LAUNCH[1],
        launch_altitude_m=1609.0, altitude_override_m=0,
    )
    state = manager.update(*LAUNCH, altitude_m=1609.0 + 20000.0, fix_type=FIX_3D,
                           now=1000.0)
    assert state.zone is Zone.LAUNCH


def test_altitude_is_measured_above_the_launch_site(manager):
    """Denver launches at 1609 m MSL; AGL is what matters."""
    state = manager.update(*LAUNCH, altitude_m=1609.0 + 500.0, fix_type=FIX_3D,
                           now=1000.0)
    assert state.altitude_agl_m == pytest.approx(500.0)


# --- vertical rate --------------------------------------------------------


def test_vertical_rate_tracks_a_steady_climb(manager):
    state = climb(manager, start_alt=1609.0, rate_mps=5.0, seconds=200)
    assert state.vertical_rate_mps == pytest.approx(5.0, abs=0.2)


def test_vertical_rate_tracks_a_descent(manager):
    state = climb(manager, start_alt=20000.0, rate_mps=-8.0, seconds=200)
    assert state.vertical_rate_mps == pytest.approx(-8.0, abs=0.3)


def test_vertical_rate_is_none_without_enough_samples(manager):
    state = manager.update(*LAUNCH, altitude_m=1609.0, fix_type=FIX_3D, now=1000.0)
    assert state.vertical_rate_mps is None


def test_vertical_rate_is_robust_to_gps_altitude_noise(manager):
    """
    GPS altitude noise is routinely several metres. A first-and-last
    difference would read that as tens of m/s; the least-squares fit should
    not.
    """
    noise = [0, 4, -3, 5, -4, 2, -5, 3, -2, 4, -3, 1]
    now = 1000.0
    state = manager.state
    for offset in noise:
        state = manager.update(*LAUNCH, altitude_m=1609.0 + offset,
                               fix_type=FIX_3D, now=now)
        now += 10.0

    assert abs(state.vertical_rate_mps) < 0.5


# --- landing --------------------------------------------------------------


def test_landing_requires_the_dwell_period(manager):
    now = fly_to_altitude(manager)

    # Arrive on the ground and sit still.
    for _ in range(5):
        manager.update(*LAUNCH, altitude_m=1650.0, fix_type=FIX_3D, now=now)
        now += 10.0
    assert manager.zone is not Zone.LANDED, "dwell period not yet elapsed"

    for _ in range(25):
        manager.update(*LAUNCH, altitude_m=1650.0, fix_type=FIX_3D, now=now)
        now += 10.0
    assert manager.zone is Zone.LANDED


def test_a_descending_payload_is_not_landed(manager):
    """Falling under canopy at low altitude must not read as landed."""
    now = fly_to_altitude(manager)
    state = climb(manager, start_alt=3000.0, rate_mps=-5.0, seconds=300,
                  start_time=now)
    assert state.zone is not Zone.LANDED


def test_a_slow_float_at_altitude_is_not_landed(manager):
    """Stationary but high: floating, not landed."""
    state = climb(manager, start_alt=20000.0, rate_mps=0.0, seconds=600)
    assert state.zone is Zone.CRUISE


def test_a_stall_during_ascent_does_not_trigger_landing(manager):
    """
    A false LANDED before apogee would stop image capture for the rest of the
    flight, so a brief stall must not be enough.
    """
    now = fly_to_altitude(manager, agl_m=2500.0)

    # Stall low for less than the dwell period, then resume climbing.
    for _ in range(5):
        manager.update(*LAUNCH, altitude_m=2000.0, fix_type=FIX_3D, now=now)
        now += 10.0
    assert manager.zone is not Zone.LANDED

    climb(manager, start_alt=2000.0, rate_mps=4.0, seconds=200, start_time=now)
    assert manager.zone is not Zone.LANDED


def test_landed_is_sticky_once_declared(manager):
    """
    A payload in a tree moves a little in the wind. Flapping back to cruise
    would cost exactly the beacons the recovery crew needs.
    """
    now = fly_to_altitude(manager)
    for _ in range(30):
        manager.update(*LAUNCH, altitude_m=1650.0, fix_type=FIX_3D, now=now)
        now += 10.0
    assert manager.zone is Zone.LANDED

    # A gust: a metre of apparent motion.
    for offset in (0, 3, -2, 4, -3):
        manager.update(*LAUNCH, altitude_m=1650.0 + offset, fix_type=FIX_3D, now=now)
        now += 10.0
    assert manager.zone is Zone.LANDED


def test_landing_far_from_the_launch_site_still_registers(manager):
    """The usual case: the balloon lands a long way downrange."""
    now = fly_to_altitude(manager)
    lat, lon = at_distance(120.0)
    for _ in range(30):
        manager.update(lat, lon, altitude_m=1650.0, fix_type=FIX_3D, now=now)
        now += 10.0
    assert manager.zone is Zone.LANDED


def test_landing_detection_can_be_disabled():
    manager = ZoneManager(
        launch_latitude=LAUNCH[0], launch_longitude=LAUNCH[1],
        launch_altitude_m=1609.0, landed_dwell_sec=float("inf"),
    )
    now = fly_to_altitude(manager)
    for _ in range(60):
        manager.update(*LAUNCH, altitude_m=1650.0, fix_type=FIX_3D, now=now)
        now += 10.0
    assert manager.zone is not Zone.LANDED


# --- launch point capture -------------------------------------------------


def test_launch_point_is_captured_from_the_first_fix():
    manager = ZoneManager(launch_latitude=0.0, launch_longitude=0.0)
    assert not manager.launch_point_known

    manager.update(*LAUNCH, altitude_m=1609.0, fix_type=FIX_3D, now=1000.0)

    assert manager.launch_point_known
    assert manager.launch_latitude == pytest.approx(LAUNCH[0])
    assert manager.launch_altitude_m == pytest.approx(1609.0)
    assert manager.zone is Zone.LAUNCH


def test_a_configured_launch_point_is_not_overwritten():
    manager = ZoneManager(launch_latitude=LAUNCH[0], launch_longitude=LAUNCH[1])
    manager.update(51.5, -0.12, altitude_m=20.0, fix_type=FIX_3D, now=1000.0)
    assert manager.launch_latitude == pytest.approx(LAUNCH[0])


def test_auto_capture_can_be_disabled():
    manager = ZoneManager(
        launch_latitude=0.0, launch_longitude=0.0, auto_capture_launch_point=False
    )
    manager.update(*LAUNCH, altitude_m=1609.0, fix_type=FIX_3D, now=1000.0)
    assert not manager.launch_point_known


def test_launch_point_can_be_set_explicitly():
    manager = ZoneManager()
    manager.set_launch_point(LAUNCH[0], LAUNCH[1], 1609.0)
    assert manager.launch_point_known
    assert manager.update(*LAUNCH, altitude_m=1609.0, fix_type=FIX_3D,
                          now=1000.0).zone is Zone.LAUNCH


# --- a whole flight -------------------------------------------------------


def test_a_full_flight_profile_visits_every_zone(manager):
    """Ascent, drift downrange, descent, landing."""
    now = 1000.0
    seen = []

    def step(lat, lon, alt, count=1, dt=10.0):
        nonlocal now
        for _ in range(count):
            zone = manager.update(lat, lon, alt, FIX_3D, now=now).zone
            if not seen or seen[-1] is not zone:
                seen.append(zone)
            now += dt

    # On the pad
    step(*LAUNCH, 1609.0, count=3)
    # Ascending overhead, crosses the altitude override
    for altitude in range(1609, 6000, 500):
        step(*LAUNCH, float(altitude))
    # Drifting downrange at altitude
    for distance in range(10, 120, 10):
        lat, lon = at_distance(float(distance))
        step(lat, lon, 25000.0)
    # Descent
    lat, lon = at_distance(130.0)
    for altitude in range(25000, 1700, -2000):
        step(lat, lon, float(altitude))
    # On the ground, still
    step(lat, lon, 1650.0, count=30)

    assert seen[0] is Zone.LAUNCH
    assert Zone.CRUISE in seen
    assert seen[-1] is Zone.LANDED


def test_peak_altitude_is_tracked(manager):
    for altitude in (1609.0, 10000.0, 30000.0, 12000.0, 1650.0):
        manager.update(*LAUNCH, altitude_m=altitude, fix_type=FIX_3D, now=1000.0)
    assert manager.peak_altitude_m == pytest.approx(30000.0)


def test_status_is_json_friendly(manager):
    import json

    manager.update(*LAUNCH, altitude_m=1700.0, fix_type=FIX_3D, now=1000.0)
    status = manager.get_status()
    json.dumps(status)
    assert status["zone"] == "launch"
    assert status["radius_km"] == 8.0


# --- landing detection arming ---------------------------------------------
#
# Regression: a payload sitting on the launch pad during setup is low and
# stationary for far longer than any dwell period, so without an arming gate
# it declared itself LANDED before launch -- stopping image capture and
# dropping to slow recovery beacons while still on the ground.


def test_sitting_on_the_pad_does_not_declare_landing(manager):
    now = 1000.0
    for _ in range(120):  # 20 minutes of pre-flight setup
        manager.update(*LAUNCH, altitude_m=1610.0, fix_type=FIX_3D, now=now)
        now += 10.0

    assert manager.zone is Zone.LAUNCH
    assert not manager.landing_armed


def test_landing_arms_only_after_the_balloon_has_flown(manager):
    assert not manager.landing_armed

    # Below the arming altitude: still disarmed.
    fly_to_altitude(manager, agl_m=1500.0)
    assert not manager.landing_armed

    fly_to_altitude(manager, agl_m=2500.0, start_time=2000.0)
    assert manager.landing_armed


def test_a_low_flight_never_arms_landing(manager):
    """A tethered or aborted flight that never gets high stays in LAUNCH."""
    now = fly_to_altitude(manager, agl_m=1000.0)
    for _ in range(40):
        manager.update(*LAUNCH, altitude_m=1610.0, fix_type=FIX_3D, now=now)
        now += 10.0
    assert manager.zone is not Zone.LANDED


def test_arm_altitude_is_configurable():
    manager = ZoneManager(
        launch_latitude=LAUNCH[0], launch_longitude=LAUNCH[1],
        launch_altitude_m=1609.0, landed_arm_altitude_m=500.0,
    )
    fly_to_altitude(manager, agl_m=600.0)
    assert manager.landing_armed


def test_status_reports_arming_state(manager):
    manager.update(*LAUNCH, altitude_m=1700.0, fix_type=FIX_3D, now=1000.0)
    status = manager.get_status()
    assert status["landing_armed"] is False
    assert status["peak_altitude_agl_m"] == pytest.approx(91, abs=1)


# --- altitude history pruning ---------------------------------------------


def test_a_stale_sample_does_not_skew_the_vertical_rate(manager):
    """
    A gap in GPS coverage must not leave an old altitude in the window; a
    least-squares fit spanning the gap would report a wild rate.
    """
    manager.update(*LAUNCH, altitude_m=25000.0, fix_type=FIX_3D, now=1000.0)

    # Long gap, then steady readings on the ground.
    now = 5000.0
    state = manager.state
    for _ in range(20):
        state = manager.update(*LAUNCH, altitude_m=1650.0, fix_type=FIX_3D, now=now)
        now += 5.0

    assert abs(state.vertical_rate_mps) < 1.0
