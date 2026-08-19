"""
Surviving a restart in flight.

The payload restarts in the air -- systemd does it, the watchdog does it, and
both beat a payload that has stopped. But a restart used to discard two things
that cannot be relearned at altitude: the launch point, and whether landing
detection had armed.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from airborne import flight_state
from airborne.zone_manager import ZoneManager

# Somewhere real, so the distances mean something.
LAUNCH_LAT, LAUNCH_LON, LAUNCH_ALT = 51.50118, -0.10868, 165.0


def manager(tmp_path, **kwargs):
    kwargs.setdefault("launch_latitude", 0.0)
    kwargs.setdefault("launch_longitude", 0.0)
    kwargs.setdefault("radius_m", 8000.0)
    kwargs.setdefault("launch_settle_sec", 0.0)
    kwargs.setdefault("state_path", str(tmp_path / "flight_state.json"))
    return ZoneManager(**kwargs)


def fly_to(mgr, altitude_m, start=1000.0, lat=LAUNCH_LAT, lon=LAUNCH_LON):
    """Take a manager from the pad up to an altitude."""
    mgr.update(latitude=lat, longitude=lon, altitude_m=LAUNCH_ALT,
               fix_type=2, now=start)
    mgr.update(latitude=lat, longitude=lon, altitude_m=altitude_m,
               fix_type=2, now=start + 60)
    return mgr


# -- the core decision ------------------------------------------------------

def test_restart_in_flight_keeps_the_launch_point(tmp_path):
    """
    The defect: a restart at 20 km captured a "launch point" 20 km up, so every
    AGL figure for the rest of the flight was measured from the wrong datum.
    The balloon read 0 m AGL at altitude -- never crossing the WiFi cutoff, the
    altitude override, or anything else keyed to height above the ground.
    """
    first = fly_to(manager(tmp_path), 20000.0)
    assert first.launch_altitude_m == pytest.approx(LAUNCH_ALT)

    # Power cycle at altitude: a new manager, first fix at 20 km.
    second = manager(tmp_path)
    second.update(latitude=38.9, longitude=-121.4, altitude_m=20000.0,
                  fix_type=2, now=2000.0)

    assert second.launch_altitude_m == pytest.approx(LAUNCH_ALT)
    assert second.launch_latitude == pytest.approx(LAUNCH_LAT)
    assert second.state.altitude_agl_m == pytest.approx(20000.0 - LAUNCH_ALT, abs=1)


def test_restart_on_the_pad_captures_fresh(tmp_path):
    """
    A payload restarting on the pad is at launch altitude, not above it, so it
    takes a new reading rather than trusting an old file.
    """
    fly_to(manager(tmp_path), 20000.0)

    # Same site, next morning, but well inside the age limit.
    second = manager(tmp_path)
    second.update(latitude=51.57000, longitude=-0.21000, altitude_m=95.0,
                  fix_type=2, now=3000.0)

    assert second.launch_altitude_m == pytest.approx(95.0)
    assert second.launch_latitude == pytest.approx(51.57000)


def test_restart_after_recovery_captures_fresh(tmp_path):
    """
    The case horizontal distance would get wrong. A payload recovered 50 km
    downrange is far from its launch point and firmly on the ground; distance
    says "in flight", altitude says "on the ground", and altitude is right.
    """
    fly_to(manager(tmp_path), 25000.0)

    second = manager(tmp_path)
    second.update(latitude=51.97000, longitude=0.49000, altitude_m=180.0,
                  fix_type=2, now=4000.0)

    assert second.launch_latitude == pytest.approx(51.97000)
    assert not second._resumed


def test_landing_site_slightly_higher_than_launch_still_captures_fresh(tmp_path):
    """The margin has to absorb ordinary terrain, not just GPS wander."""
    fly_to(manager(tmp_path), 25000.0)

    second = manager(tmp_path)
    second.update(latitude=39.1, longitude=-120.5,
                  altitude_m=LAUNCH_ALT + 100.0, fix_type=2, now=4000.0)

    assert not second._resumed


# -- arming -----------------------------------------------------------------

def test_arming_survives_a_restart_during_descent(tmp_path):
    """
    Landing detection arms above 2000 m AGL. A payload that restarts on the way
    down will never see that height again, so without persistence it comes down
    disarmed, never declares itself landed, and keeps taking pictures in a
    field instead of becoming the recovery beacon.
    """
    first = fly_to(manager(tmp_path), 25000.0)
    assert first.landing_armed

    second = manager(tmp_path)
    second.update(latitude=39.0, longitude=-121.0, altitude_m=8000.0,
                  fix_type=2, now=5000.0)

    assert second.landing_armed, "came down disarmed"
    assert second._peak_altitude_agl_m >= 2000.0


def test_a_fresh_flight_starts_disarmed(tmp_path):
    """Arming must not leak into the next flight; that is the pad-detection bug."""
    fly_to(manager(tmp_path), 25000.0)

    second = manager(tmp_path)
    second.update(latitude=38.7, longitude=-121.2, altitude_m=95.0,
                  fix_type=2, now=6000.0)

    assert not second.landing_armed


# -- the state file ---------------------------------------------------------

def test_state_older_than_the_limit_is_ignored(tmp_path):
    path = str(tmp_path / "flight_state.json")
    flight_state.save(path, flight_state.FlightState(
        launch_latitude=LAUNCH_LAT, launch_longitude=LAUNCH_LON,
        launch_altitude_m=LAUNCH_ALT, landing_armed=True,
    ), now=time.time() - 48 * 3600)

    assert flight_state.load(path) is None


def test_state_from_the_future_is_ignored(tmp_path):
    """
    The payload has no RTC. A state file written after NTP synced looks like
    the future to a payload that has booted and not synced yet.
    """
    path = str(tmp_path / "flight_state.json")
    flight_state.save(path, flight_state.FlightState(
        launch_latitude=LAUNCH_LAT, launch_longitude=LAUNCH_LON,
        launch_altitude_m=LAUNCH_ALT,
    ), now=time.time() + 60 * 86400)

    assert flight_state.load(path) is None


def test_corrupt_state_does_not_stop_the_payload(tmp_path):
    """A bad state file means a fresh start, never a payload that will not boot."""
    path = tmp_path / "flight_state.json"
    path.write_text("{ this is not json")

    assert flight_state.load(str(path)) is None

    mgr = manager(tmp_path)
    mgr.update(latitude=38.7, longitude=-121.2, altitude_m=95.0,
               fix_type=2, now=1000.0)
    assert mgr.launch_altitude_m == pytest.approx(95.0)


def test_truncated_state_is_rejected(tmp_path):
    path = tmp_path / "flight_state.json"
    path.write_text('{"version": 1, "launch_latitude": 38.6')
    assert flight_state.load(str(path)) is None


def test_state_from_another_version_is_ignored(tmp_path):
    path = tmp_path / "flight_state.json"
    path.write_text(json.dumps({
        "version": 99, "launch_latitude": 1.0, "launch_longitude": 2.0,
        "launch_altitude_m": 3.0, "saved_at": time.time(),
    }))
    assert flight_state.load(str(path)) is None


def test_saving_is_atomic(tmp_path):
    path = str(tmp_path / "flight_state.json")
    assert flight_state.save(path, flight_state.FlightState(
        launch_latitude=LAUNCH_LAT, launch_longitude=LAUNCH_LON,
        launch_altitude_m=LAUNCH_ALT,
    ))
    assert not os.path.exists(path + ".part")


def test_an_unwritable_path_is_survivable(tmp_path):
    """Losing persistence is a degraded flight, not a failed one."""
    mgr = manager(tmp_path, state_path="/nonexistent-root/state.json")
    mgr.update(latitude=38.7, longitude=-121.2, altitude_m=95.0,
               fix_type=2, now=1000.0)
    assert mgr.launch_altitude_m == pytest.approx(95.0)


def test_persistence_can_be_disabled(tmp_path):
    mgr = manager(tmp_path, state_path=None)
    fly_to(mgr, 20000.0)
    assert not os.listdir(tmp_path)


# -- the decision function on its own ---------------------------------------

@pytest.mark.parametrize("altitude, in_flight, label", [
    (20000.0, True,  "at altitude"),
    (LAUNCH_ALT + 400, True,  "well above the pad"),
    (LAUNCH_ALT + 150, False, "exactly the margin"),
    (LAUNCH_ALT + 40,  False, "GPS wander on the pad"),
    (LAUNCH_ALT,       False, "on the pad"),
    (LAUNCH_ALT - 200, False, "recovered lower than launch"),
    (None,             False, "no altitude"),
])
def test_in_flight_decision(altitude, in_flight, label):
    state = flight_state.FlightState(
        launch_latitude=LAUNCH_LAT, launch_longitude=LAUNCH_LON,
        launch_altitude_m=LAUNCH_ALT,
    )
    assert flight_state.indicates_in_flight(state, altitude) is in_flight, label


def test_no_saved_state_is_never_in_flight():
    assert not flight_state.indicates_in_flight(None, 20000.0)


def test_saved_state_without_an_altitude_is_never_in_flight():
    """Nothing to compare against, so it cannot be evidence of anything."""
    state = flight_state.FlightState(
        launch_latitude=LAUNCH_LAT, launch_longitude=LAUNCH_LON,
        launch_altitude_m=None,
    )
    assert not flight_state.indicates_in_flight(state, 20000.0)


# -- write volume -----------------------------------------------------------

def test_the_ascent_does_not_hammer_the_card(tmp_path, monkeypatch):
    """
    Saving on every new peak would write to the SD card about once a second for
    the whole climb.
    """
    from airborne import zone_manager as zm

    writes = []
    real_save = flight_state.save
    monkeypatch.setattr(
        zm.flight_state, "save",
        lambda path, state, now=None: (writes.append(1), real_save(path, state, now))[1],
    )

    mgr = manager(tmp_path)
    mgr.update(latitude=LAUNCH_LAT, longitude=LAUNCH_LON, altitude_m=LAUNCH_ALT,
               fix_type=2, now=1000.0)
    # A 30 km ascent, one fix a second.
    for step in range(1, 1500):
        mgr.update(latitude=LAUNCH_LAT, longitude=LAUNCH_LON,
                   altitude_m=LAUNCH_ALT + step * 20.0, fix_type=2,
                   now=1000.0 + step)

    assert len(writes) < 150, f"{len(writes)} writes across one ascent"


# -- what this was for ------------------------------------------------------

def test_the_wifi_cutoff_refires_after_a_restart_in_flight(tmp_path):
    """
    The reason persistence was needed. Without it, a payload restarting at
    20 km recaptured its launch point at 20 km, read 0 m AGL, and left WiFi
    scanning uselessly for the rest of the flight because it never again saw
    the 300 m of "climb" the cutoff waits for.
    """
    from airborne.power import LaunchWiFiCutoff, PowerAction

    fly_to(manager(tmp_path), 20000.0)

    calls = []
    cutoff = LaunchWiFiCutoff(
        altitude_agl_m=300.0, confirmations_needed=3,
        action=lambda: (calls.append(1), PowerAction("wifi", True, "test", 55))[1],
    )

    resumed = manager(tmp_path)
    for step in range(3):
        state = resumed.update(
            latitude=38.9, longitude=-121.4, altitude_m=19800.0,
            fix_type=2, now=2000.0 + step,
        )
        cutoff.update(altitude_agl_m=state.altitude_agl_m, fix_type=2)

    assert cutoff.fired, "WiFi would have stayed up for the rest of the flight"
    assert calls == [1]
