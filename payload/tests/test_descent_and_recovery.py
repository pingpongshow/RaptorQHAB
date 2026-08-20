"""Descent zone, flight summary, GPS watchdog, and altitude-triggered
balloon mode.

Descent is the half hour that decides whether the payload is found: the
landing prediction converges then, and the balloon drops below the horizon
of everything that was hearing it. These verify it gets its own behaviour,
and that the pieces meant to survive a bad receiver actually do.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from airborne.zone_manager import Zone, ZoneManager
from airborne.transmit_scheduler import DEFAULT_SCHEDULES, schedules_from_config
from airborne.flight_summary import FlightSummary
from airborne.gps_watchdog import GPSWatchdog
from common.gps import GPSReader
from common.protocol import (FlightSummaryPayload, build_packet, parse_packet)
from common.constants import PacketType


def flown_manager(tmp_path, **kw):
    """A manager that has actually flown, so landing/descent are armed."""
    m = ZoneManager(launch_latitude=40.0, launch_longitude=-100.0,
                    state_path=str(tmp_path / "state.json"), **kw)
    m.update(latitude=40.0, longitude=-100.0, altitude_m=200.0, fix_type=3, now=0)
    # Climb past the arming altitude.
    for i, alt in enumerate([1000, 2000, 3000, 4000], start=1):
        m.update(latitude=40.0, longitude=-100.0, altitude_m=200.0 + alt,
                 fix_type=3, now=i * 10)
    return m


# --- Descent zone ---------------------------------------------------------

def test_descent_needs_sustained_fall_not_a_dip(tmp_path):
    m = flown_manager(tmp_path, descent_dwell_sec=20.0)
    t = 100.0
    # One reading of descent is not descent.
    m.update(latitude=40.0, longitude=-100.0, altitude_m=4100.0, fix_type=3, now=t)
    assert m.zone is not Zone.DESCENT


def test_descent_is_entered_after_the_dwell(tmp_path):
    m = flown_manager(tmp_path, descent_dwell_sec=20.0)
    alt, t = 4200.0, 100.0
    # The rolling rate estimate still contains the ascent for the first
    # several readings, so confirmation lands later than the raw fall implies.
    for _ in range(20):
        t += 5.0
        alt -= 150.0                       # -30 m/s
        m.update(latitude=40.0, longitude=-100.0, altitude_m=alt, fix_type=3, now=t)
    assert m.zone is Zone.DESCENT


def test_descent_survives_a_momentary_updraft(tmp_path):
    """A payload on a parachute is not a smooth faller. Bouncing back to
    cruise cadence mid-descent is the failure this prevents."""
    m = flown_manager(tmp_path, descent_dwell_sec=20.0)
    alt, t = 4200.0, 100.0
    for _ in range(20):
        t += 5.0; alt -= 150.0
        m.update(latitude=40.0, longitude=-100.0, altitude_m=alt, fix_type=3, now=t)
    assert m.zone is Zone.DESCENT
    # A brief stall: still descent.
    t += 5.0
    m.update(latitude=40.0, longitude=-100.0, altitude_m=alt, fix_type=3, now=t)
    assert m.zone is Zone.DESCENT


def test_descent_does_not_trigger_before_the_balloon_has_flown(tmp_path):
    """Lowering the payload off a table is not a descent."""
    m = ZoneManager(launch_latitude=40.0, launch_longitude=-100.0,
                    state_path=str(tmp_path / "s.json"), descent_dwell_sec=1.0)
    t = 0.0
    for alt in (210, 205, 200, 195, 190, 185):
        t += 5.0
        m.update(latitude=40.0, longitude=-100.0, altitude_m=alt, fix_type=3, now=t)
    assert m.zone is not Zone.DESCENT


def test_descent_schedule_favours_the_mesh_over_imagery():
    d = DEFAULT_SCHEDULES[Zone.DESCENT]
    c = DEFAULT_SCHEDULES[Zone.CRUISE]
    assert d.mesh_percent > d.image_percent, "the beacon is worth more here"
    assert d.beacon_interval_sec < c.beacon_interval_sec
    assert d.telemetry_interval_packets is not None
    assert d.telemetry_interval_packets < 5


# --- Flight summary -------------------------------------------------------

def test_summary_round_trips_over_the_wire():
    s = FlightSummary()
    s.note_position(40.0, -100.0, 200.0, 5.0, now=0)
    s.note_position(40.1, -100.0, 31000.0, -25.0, now=3600)
    s.note_temperature(-40.0); s.note_temperature(35.0)
    payload = s.as_payload(packets_sent=650000, images_captured=231,
                           zone_index=3, now=3600)
    packet = build_packet(PacketType.FLIGHT_SUMMARY, 1, payload)
    parsed = parse_packet(packet)
    assert parsed is not None
    back = FlightSummaryPayload.deserialize(parsed[3])
    assert abs(back.max_altitude_m - 31000.0) < 1
    assert abs(back.max_descent_rate_mps - 25.0) < 0.2
    assert back.min_cpu_temp_c == -40.0
    assert back.images_captured == 231


def test_summary_odometer_ignores_a_gps_teleport():
    """The one number here that cannot correct itself later."""
    s = FlightSummary()
    s.note_position(40.0, -100.0, 200.0, 0.0, now=0)
    s.note_position(40.01, -100.0, 200.0, 0.0, now=10)
    honest = s.distance_travelled_m
    s.note_position(80.0, -100.0, 200.0, 0.0, now=20)   # 4400 km glitch
    assert s.distance_travelled_m == honest


# --- GPS watchdog ---------------------------------------------------------

class FakeGPS:
    def __init__(self): self.actions = []
    def configure(self): self.actions.append("configure")
    def restart(self, cold=False): self.actions.append("cold" if cold else "hot")


def test_watchdog_is_patient_then_escalates():
    g = FakeGPS()
    w = GPSWatchdog(g, no_fix_timeout_sec=300, escalation_interval_sec=180)
    w.update(True, now=0)
    for t in (60, 150, 299):
        assert w.update(False, now=t) is None
    assert g.actions == [], "a cloud is not a fault"
    assert w.update(False, now=301) == "reconfigure"
    assert w.update(False, now=482) == "hot_restart"
    assert w.update(False, now=663) == "cold_start"
    assert w.update(False, now=844) == "cold_start"   # capped


def test_watchdog_resets_when_the_fix_returns():
    g = FakeGPS()
    w = GPSWatchdog(g, no_fix_timeout_sec=100, escalation_interval_sec=50)
    w.update(True, now=0)
    w.update(False, now=101)
    assert w.stage == 1
    w.update(True, now=120)
    assert w.stage == 0 and w.get_status()["recoveries"] == 1


def test_a_failing_recovery_step_does_not_raise():
    class Broken(FakeGPS):
        def configure(self): raise RuntimeError("receiver is not listening")
    w = GPSWatchdog(Broken(), no_fix_timeout_sec=10, escalation_interval_sec=10)
    w.update(True, now=0)
    assert w.update(False, now=20) is None      # swallowed, not raised


# --- Balloon mode by altitude --------------------------------------------

def test_balloon_mode_is_not_applied_on_the_ground():
    g = GPSReader(simulate=False, airborne_mode=True)
    sent = []
    g._send_pmtk = lambda c: sent.append(c)
    assert g.ensure_high_altitude_mode(0.0) is False
    assert g.ensure_high_altitude_mode(3000.0) is False
    assert sent == []


def test_balloon_mode_applies_above_the_threshold():
    g = GPSReader(simulate=False, airborne_mode=True)
    sent = []
    g._send_pmtk = lambda c: sent.append(c)
    assert g.ensure_high_altitude_mode(9000.0) is True
    assert any("886,3" in c for c in sent)


def test_flight_state_forces_the_mode_without_an_altitude():
    """A receiver that lost its fix on the way up reports no altitude to
    trigger on -- and is exactly the one that needs the relaxed model."""
    g = GPSReader(simulate=False, airborne_mode=True)
    sent = []
    g._send_pmtk = lambda c: sent.append(c)
    assert g.ensure_high_altitude_mode(None, force=True) is True
    assert any("886,3" in c for c in sent)


def test_a_confirmed_mode_is_not_sent_again():
    g = GPSReader(simulate=False, airborne_mode=True)
    sent = []
    g._send_pmtk = lambda c: sent.append(c)
    g.ensure_high_altitude_mode(9000.0)
    g._balloon_mode_confirmed = True
    g.ensure_high_altitude_mode(9000.0)
    assert len([c for c in sent if "886,3" in c]) == 1


def test_a_restart_invalidates_the_mode():
    """A restarted receiver has forgotten the mode; the payload must not
    believe it is still applied."""
    g = GPSReader(simulate=False, airborne_mode=True)
    g._send_pmtk = lambda c: None
    g.ensure_high_altitude_mode(9000.0)
    g._balloon_mode_confirmed = True
    g.restart(cold=False)
    assert g.balloon_mode_confirmed is False


def test_descent_holds_when_the_rate_estimate_goes_missing(tmp_path):
    """A falling payload is the thing whose rate estimate disappears -- the
    altitude history thins as fixes get harder near the ground. Treating an
    unknown rate as "not descending" lengthened the beacon from 45 s to 300 s
    at the exact moment the balloon was dropping below the horizon."""
    m = flown_manager(tmp_path, descent_dwell_sec=20.0)
    alt, t = 4200.0, 100.0
    for _ in range(20):
        t += 5.0; alt -= 150.0
        m.update(latitude=40.0, longitude=-100.0, altitude_m=alt, fix_type=3, now=t)
    assert m.zone is Zone.DESCENT

    # A gap long enough that the rolling window can no longer produce a rate.
    t += 600.0
    m.update(latitude=40.0, longitude=-100.0, altitude_m=alt, fix_type=3, now=t)
    assert m.zone is Zone.DESCENT, "an unknown rate must not release descent"


def test_genuine_ascent_does_release_descent(tmp_path):
    """The one thing that should get it out: actually climbing again."""
    m = flown_manager(tmp_path, descent_dwell_sec=20.0)
    alt, t = 4200.0, 100.0
    for _ in range(20):
        t += 5.0; alt -= 150.0
        m.update(latitude=40.0, longitude=-100.0, altitude_m=alt, fix_type=3, now=t)
    assert m.zone is Zone.DESCENT
    for _ in range(20):
        t += 5.0; alt += 150.0
        m.update(latitude=40.0, longitude=-100.0, altitude_m=alt, fix_type=3, now=t)
    assert m.zone is not Zone.DESCENT


def test_the_backstop_does_not_defeat_the_altitude_threshold():
    """Cruise begins around 3 km; the high-altitude model is wanted near 8.
    Forcing on the zone alone would apply it every flight at cruise entry,
    which is the boot-time behaviour this replaced."""
    main_py = os.path.join(os.path.dirname(__file__), "..", "airborne", "main.py")
    src = open(main_py).read()
    idx = src.find("ensure_high_altitude_mode")
    assert idx > 0
    window = src[idx:idx + 800]
    assert "altitude_agl_m is None" in window, \
        "the zone backstop must require a missing altitude"
