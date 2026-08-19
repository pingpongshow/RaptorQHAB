"""
Whole-flight simulation against the payload controller.

Phase 4's exit criterion: drive a synthetic GPS track through the real
controller, with a fake radio, and check that the balloon spends its airtime
the way the configuration says it should -- through ascent, drift, descent,
and landing.

This is also the dry-run harness suggested in the roadmap. It replays a full
flight in under a second, which is worth more than any single feature for a
system you get one shot at.
"""

import math

import pytest

from airborne.config import Config
from airborne.main import RaptorHabAirborne
from airborne.transmit_scheduler import Activity
from airborne.zone_manager import Zone
from common.radio_lora import RadioMode
from common.radio_manager import RadioModeManager

LAUNCH_LAT = 39.7392
LAUNCH_LON = -104.9903
LAUNCH_ALT = 1609.0


class FakeRadio:
    def __init__(self):
        self.lora_packets = []
        self.gfsk_packets = []
        self.configure_calls = []
        self.gfsk_restores = 0

    def configure_lora(self, lora_config, frequency_mhz, tx_power_dbm):
        self.configure_calls.append((frequency_mhz, tx_power_dbm))

    def restore_gfsk(self):
        self.gfsk_restores += 1

    def set_tx_power(self, power_dbm):
        pass

    def set_standby(self, rc=True):
        pass

    def transmit(self, packet, timeout_ms=5000):
        self.gfsk_packets.append(packet)
        return True

    def transmit_lora(self, packet, timeout_ms=10000):
        self.lora_packets.append(packet)
        return True


class FakeGPS:
    def __init__(self, latitude, longitude, altitude, fix_type=2):
        self.latitude = latitude
        self.longitude = longitude
        self.altitude = altitude
        self.fix_type = fix_type
        self.satellites = 11
        self.speed = 12.0
        self.heading = 90.0
        self.time_utc = 0


def make_controller(tmp_path, **overrides):
    config = Config()
    config.image_storage_path = str(tmp_path / "images")
    config.log_path = str(tmp_path / "logs")
    config.meshtastic_enabled = True
    config.meshtastic_inter_packet_delay_ms = 0
    config.zone_scheduling_enabled = True
    config.zone_launch_latitude = LAUNCH_LAT
    config.zone_launch_longitude = LAUNCH_LON
    config.meshtastic_region_dwell_sec = 0
    config.meshtastic_region_edge_margin_km = 0
    for key, value in overrides.items():
        setattr(config, key, value)

    controller = RaptorHabAirborne(config, debug=True)
    radio = FakeRadio()
    controller._radio = radio
    controller._radio_manager = RadioModeManager(
        radio, gfsk_tx_power_dbm=config.radio_power_dbm
    )
    controller._initialize_meshtastic()
    controller._initialize_zone_scheduling()
    controller._zone_manager.launch_altitude_m = LAUNCH_ALT
    return controller, radio


def position_at(km_east, altitude_m):
    lon_offset = km_east / (111.32 * math.cos(math.radians(LAUNCH_LAT)))
    return FakeGPS(LAUNCH_LAT, LAUNCH_LON + lon_offset, altitude_m)


def feed(controller, gps, now):
    """Push one GPS sample through the zone and region logic."""
    with controller._gps_lock:
        controller._current_gps = gps

    controller._zone_manager.update(
        latitude=gps.latitude,
        longitude=gps.longitude,
        altitude_m=gps.altitude,
        fix_type=gps.fix_type,
        now=now,
    )
    controller._tx_scheduler.set_zone(controller._zone_manager.zone, now=now)


def allocate(controller, seconds, now, beacon_cost=3.0, slice_cost=None):
    """Run the allocator over simulated time and tally the airtime."""
    scheduler = controller._tx_scheduler
    tally = {a: 0.0 for a in Activity}
    deadline = now + seconds

    while now < deadline:
        grant = scheduler.next_slice(now=now)
        actual = (
            beacon_cost
            if grant.activity is Activity.MESHTASTIC
            else (slice_cost or grant.duration_sec)
        )
        tally[grant.activity] += actual
        scheduler.record(grant.activity, actual, now=now)
        if grant.activity is Activity.MESHTASTIC:
            scheduler.record_beacon(now=now)
        now += actual

    return tally, now


# --- the flight -----------------------------------------------------------


def test_full_flight_visits_every_zone_in_order(tmp_path):
    controller, _radio = make_controller(tmp_path)
    now = 1000.0

    seen = []

    def observe():
        zone = controller._zone_manager.zone
        if not seen or seen[-1] is not zone:
            seen.append(zone)

    # Pre-flight on the pad, 20 minutes.
    for _ in range(120):
        feed(controller, position_at(0.0, LAUNCH_ALT), now)
        observe()
        now += 10.0

    # Ascent to 30 km, drifting east.
    for step in range(60):
        feed(controller, position_at(step * 2.0, LAUNCH_ALT + step * 500), now)
        observe()
        now += 10.0

    # Float, drifting further out.
    for step in range(30):
        feed(controller, position_at(120.0 + step * 2.0, LAUNCH_ALT + 30000), now)
        observe()
        now += 10.0

    # Descent.
    for step in range(30):
        feed(controller, position_at(180.0, LAUNCH_ALT + 30000 - step * 1000), now)
        observe()
        now += 10.0

    # On the ground.
    for _ in range(40):
        feed(controller, position_at(180.0, LAUNCH_ALT + 20), now)
        observe()
        now += 10.0

    assert seen[0] is Zone.LAUNCH, "must not be UNKNOWN once a fix arrives"
    assert Zone.CRUISE in seen
    assert seen[-1] is Zone.LANDED
    assert Zone.LANDED not in seen[:2], "must not declare landing on the pad"


def test_airtime_matches_the_budget_in_each_zone(tmp_path):
    """The headline requirement: 98% images near the pad, ~5% in cruise."""
    controller, _radio = make_controller(tmp_path)
    now = 1000.0

    # On the pad, inside the launch radius.
    feed(controller, position_at(0.0, LAUNCH_ALT), now)
    launch_tally, now = allocate(controller, 4000.0, now)
    launch_total = sum(launch_tally.values())
    launch_image_pct = 100.0 * launch_tally[Activity.IMAGES] / launch_total

    # Downrange and high: cruise.
    feed(controller, position_at(100.0, LAUNCH_ALT + 25000), now)
    cruise_tally, now = allocate(controller, 6000.0, now)
    cruise_total = sum(cruise_tally.values())
    cruise_image_pct = 100.0 * cruise_tally[Activity.IMAGES] / cruise_total
    cruise_idle_pct = 100.0 * cruise_tally[Activity.IDLE] / cruise_total

    assert launch_image_pct > 90.0, f"launch images {launch_image_pct:.0f}%"
    assert cruise_image_pct < 12.0, f"cruise images {cruise_image_pct:.0f}%"
    assert cruise_idle_pct > 75.0, f"cruise idle {cruise_idle_pct:.0f}%"


def test_landed_stops_images_and_keeps_beaconing(tmp_path):
    """
    On the ground the image link is usually blocked by terrain, and the LoRa
    beacon is what finds the payload.
    """
    controller, _radio = make_controller(tmp_path)
    now = 1000.0

    # Fly, so landing detection arms.
    for step in range(60):
        feed(controller, position_at(step * 2.0, LAUNCH_ALT + step * 500), now)
        now += 10.0

    # Land and sit still.
    for _ in range(40):
        feed(controller, position_at(120.0, LAUNCH_ALT + 20), now)
        now += 10.0

    assert controller._zone_manager.zone is Zone.LANDED
    assert not controller._capture_allowed()

    tally, now = allocate(controller, 3000.0, now)
    assert tally[Activity.IMAGES] == 0.0
    assert tally[Activity.MESHTASTIC] > 0.0


def test_beacons_keep_flowing_through_the_whole_flight(tmp_path):
    """No stretch of the flight may go without a beacon."""
    controller, _radio = make_controller(tmp_path)
    scheduler = controller._tx_scheduler
    now = 1000.0

    beacon_times = []
    profile = (
        [(0.0, LAUNCH_ALT)] * 20
        + [(s * 2.0, LAUNCH_ALT + s * 500) for s in range(60)]
        + [(120.0 + s * 2.0, LAUNCH_ALT + 30000) for s in range(30)]
    )

    for km, altitude in profile:
        feed(controller, position_at(km, altitude), now)
        for _ in range(30):
            grant = scheduler.next_slice(now=now)
            actual = 3.0 if grant.activity is Activity.MESHTASTIC else grant.duration_sec
            if grant.activity is Activity.MESHTASTIC:
                beacon_times.append(now)
                scheduler.record_beacon(now=now)
            scheduler.record(grant.activity, actual, now=now)
            now += actual

    assert len(beacon_times) > 10

    gaps = [b - a for a, b in zip(beacon_times, beacon_times[1:])]
    # The launch-zone interval is the longest at 600 s; allow one slice of slop.
    assert max(gaps) < 700.0, f"longest beacon gap was {max(gaps):.0f}s"


def test_zone_and_region_both_track_a_transatlantic_drift(tmp_path):
    """
    A long-duration flight crossing from the US to Europe should end up in
    cruise on the EU 868 band, which the HF board can reach.
    """
    controller, _radio = make_controller(tmp_path)
    now = 1000.0

    feed(controller, position_at(0.0, LAUNCH_ALT), now)
    assert controller._zone_manager.zone is Zone.LAUNCH
    assert controller._region_manager.state.code == "US"

    now += 1000.0
    berlin = FakeGPS(52.52, 13.40, LAUNCH_ALT + 25000)
    with controller._gps_lock:
        controller._current_gps = berlin
    controller._zone_manager.update(
        berlin.latitude, berlin.longitude, berlin.altitude, 2, now=now
    )
    controller._region_manager.update(berlin.latitude, berlin.longitude, 2, now=now)
    controller._region_manager.update(berlin.latitude, berlin.longitude, 2, now=now + 1)
    controller._apply_region_to_radio()

    assert controller._zone_manager.zone is Zone.CRUISE
    assert controller._region_manager.state.code == "EU_868"

    settings = controller._radio_manager.lora_settings
    assert settings.frequency_mhz == pytest.approx(869.525, abs=0.0005)
    assert settings.tx_power_dbm == 14, "clamped to the EU 868 ceiling"


def test_capture_is_disabled_only_after_landing(tmp_path):
    controller, _radio = make_controller(tmp_path)
    now = 1000.0

    feed(controller, position_at(0.0, LAUNCH_ALT), now)
    assert controller._capture_allowed()

    feed(controller, position_at(100.0, LAUNCH_ALT + 25000), now + 100)
    assert controller._capture_allowed(), "cruise still takes pictures"


def test_gps_loss_mid_flight_holds_the_zone(tmp_path):
    """Q1: hold last known mode."""
    controller, _radio = make_controller(tmp_path)
    now = 1000.0

    feed(controller, position_at(100.0, LAUNCH_ALT + 25000), now)
    assert controller._zone_manager.zone is Zone.CRUISE

    with controller._gps_lock:
        controller._current_gps = None
    controller._update_zone()

    assert controller._zone_manager.zone is Zone.CRUISE


def test_zone_scheduling_can_be_disabled(tmp_path):
    controller, _radio = make_controller(tmp_path, zone_scheduling_enabled=False)
    controller._zone_manager = None
    controller._tx_scheduler = None

    assert controller._capture_allowed(), "capture always runs without zones"


def test_the_radio_ends_in_gfsk_after_a_beacon_slice(tmp_path):
    """The image downlink must always get the radio back."""
    controller, radio = make_controller(tmp_path)
    now = 1000.0

    feed(controller, position_at(0.0, LAUNCH_ALT), now)
    controller._run_beacon_cycle()

    assert controller._radio_manager.mode is RadioMode.GFSK
    assert len(radio.lora_packets) >= 3
