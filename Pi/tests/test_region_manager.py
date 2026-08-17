"""
Region tracking: when the balloon retunes, when it holds, and when it must
stop transmitting altogether.

The safety-critical behaviour is the last one. Over territory with no known
band plan there is no correct frequency, and picking one anyway risks
transmitting outside what is permitted.
"""

import pytest

from airborne.region_manager import RegionManager, RegionSource

DENVER = (39.74, -104.99)      # US
BERLIN = (52.52, 13.40)        # EU_868
TOKYO = (35.68, 139.69)        # JP
MID_PACIFIC = (0.0, -140.0)    # nowhere
FIX_3D = 2
FIX_2D = 1
NO_FIX = 0


@pytest.fixture
def manager():
    return RegionManager(
        home_region_code="US",
        auto_switch=True,
        dwell_sec=60.0,
        edge_margin_km=25.0,
    )


def settle(manager, position, start=1000.0, fix=FIX_3D):
    """Feed a position until any dwell period has elapsed."""
    manager.update(*position, fix_type=fix, now=start)
    manager.update(*position, fix_type=fix, now=start + manager.dwell_sec + 1)
    return manager.state


# --- startup --------------------------------------------------------------


def test_starts_on_the_home_region_before_any_fix(manager):
    assert manager.region.code == "US"
    assert manager.state.source is RegionSource.HOME_DEFAULT
    assert manager.may_transmit


def test_manual_mode_ignores_position_entirely():
    manual = RegionManager(home_region_code="EU_868", auto_switch=False)
    manual.update(*DENVER, fix_type=FIX_3D)
    assert manual.region.code == "EU_868"
    assert manual.state.source is RegionSource.CONFIGURED


def test_unknown_home_region_falls_back_to_us():
    fallback = RegionManager(home_region_code="ATLANTIS")
    assert fallback.region.code == "US"


def test_set_home_region_validates():
    manager = RegionManager(auto_switch=False)
    assert manager.set_home_region("JP")
    assert manager.region.code == "JP"
    assert not manager.set_home_region("NOWHERE")
    assert manager.region.code == "JP"


# --- adopting a region ----------------------------------------------------


def test_first_fix_confirms_the_region_from_gps(manager):
    manager.update(*DENVER, fix_type=FIX_3D, now=1000.0)
    assert manager.region.code == "US"
    assert manager.state.source is RegionSource.GPS


def test_switching_regions_requires_the_dwell_period(manager):
    settle(manager, DENVER)

    manager.update(*BERLIN, fix_type=FIX_3D, now=2000.0)
    assert manager.region.code == "US", "must not switch instantly"
    assert manager.get_status()["pending_candidate"] == "EU_868"

    manager.update(*BERLIN, fix_type=FIX_3D, now=2000.0 + 30)
    assert manager.region.code == "US", "dwell period not yet elapsed"

    manager.update(*BERLIN, fix_type=FIX_3D, now=2000.0 + 61)
    assert manager.region.code == "EU_868"
    assert manager.state.source is RegionSource.GPS


def test_returning_before_the_dwell_elapses_cancels_the_change(manager):
    """A balloon wobbling across a border must not thrash between bands."""
    settle(manager, DENVER)

    manager.update(*BERLIN, fix_type=FIX_3D, now=2000.0)
    assert manager.get_status()["pending_candidate"] == "EU_868"

    manager.update(*DENVER, fix_type=FIX_3D, now=2030.0)
    assert manager.region.code == "US"
    assert manager.get_status()["pending_candidate"] is None


def test_edge_margin_blocks_a_change_near_a_boundary():
    """Just inside a new region does not count; you must be well inside."""
    manager = RegionManager(dwell_sec=0.0, edge_margin_km=100.0)
    settle(manager, DENVER, fix=FIX_3D)

    # A point inside the Europe box but within 100 km of its edge.
    manager.update(34.5, 10.0, fix_type=FIX_3D, now=3000.0)
    manager.update(34.5, 10.0, fix_type=FIX_3D, now=3100.0)
    assert manager.region.code == "US"


def test_edge_margin_of_zero_disables_the_test():
    manager = RegionManager(dwell_sec=0.0, edge_margin_km=0)
    settle(manager, DENVER)
    manager.update(*BERLIN, fix_type=FIX_3D, now=3000.0)
    manager.update(*BERLIN, fix_type=FIX_3D, now=3001.0)
    assert manager.region.code == "EU_868"


def test_a_third_region_restarts_the_dwell(manager):
    settle(manager, DENVER)

    manager.update(*BERLIN, fix_type=FIX_3D, now=2000.0)
    manager.update(*TOKYO, fix_type=FIX_3D, now=2030.0)
    assert manager.get_status()["pending_candidate"] == "JP"

    # The Berlin dwell must not carry over to Tokyo.
    manager.update(*TOKYO, fix_type=FIX_3D, now=2070.0)
    assert manager.region.code == "US"

    manager.update(*TOKYO, fix_type=FIX_3D, now=2095.0)
    assert manager.region.code == "JP"


# --- fix quality ----------------------------------------------------------


def test_a_2d_fix_does_not_drive_a_region_change(manager):
    settle(manager, DENVER)
    manager.update(*BERLIN, fix_type=FIX_2D, now=2000.0)
    manager.update(*BERLIN, fix_type=FIX_2D, now=2100.0)
    assert manager.region.code == "US"


def test_2d_fixes_are_accepted_when_configured_to():
    manager = RegionManager(dwell_sec=0.0, edge_margin_km=0, require_3d_fix=False)
    manager.update(*BERLIN, fix_type=FIX_2D, now=1000.0)
    manager.update(*BERLIN, fix_type=FIX_2D, now=1001.0)
    assert manager.region.code == "EU_868"


# --- GPS loss -------------------------------------------------------------


def test_gps_loss_holds_the_last_known_region(manager):
    """Q1: hold last known mode."""
    settle(manager, DENVER)
    assert manager.state.source is RegionSource.GPS

    manager.update(None, None, fix_type=NO_FIX, now=3000.0)
    assert manager.region.code == "US"
    assert manager.state.source is RegionSource.HELD
    assert manager.may_transmit


def test_gps_loss_discards_a_pending_change(manager):
    """A half-confirmed change must not survive a gap in position data."""
    settle(manager, DENVER)
    manager.update(*BERLIN, fix_type=FIX_3D, now=2000.0)
    assert manager.get_status()["pending_candidate"] == "EU_868"

    manager.update(None, None, fix_type=NO_FIX, now=2030.0)
    assert manager.get_status()["pending_candidate"] is None
    assert manager.region.code == "US"


def test_region_resumes_from_gps_after_a_fix_returns(manager):
    settle(manager, DENVER)
    manager.update(None, None, fix_type=NO_FIX, now=3000.0)
    assert manager.state.source is RegionSource.HELD

    manager.update(*DENVER, fix_type=FIX_3D, now=3100.0)
    assert manager.state.source is RegionSource.GPS


# --- unknown territory: the safety case -----------------------------------


def test_unknown_territory_suspends_transmission(manager):
    settle(manager, DENVER)

    manager.update(*MID_PACIFIC, fix_type=FIX_3D, now=3000.0)
    assert manager.region is None
    assert manager.state.source is RegionSource.NONE
    assert not manager.may_transmit, "must never guess a frequency"


def test_unknown_territory_takes_effect_immediately(manager):
    """
    No dwell period here. Continuing to transmit on the previous region's
    frequency while over unknown territory is exactly what must not happen.
    """
    settle(manager, DENVER)
    manager.update(*MID_PACIFIC, fix_type=FIX_3D, now=3000.0)
    assert not manager.may_transmit


def test_transmission_resumes_on_re_entering_known_territory(manager):
    settle(manager, DENVER)
    manager.update(*MID_PACIFIC, fix_type=FIX_3D, now=3000.0)
    assert not manager.may_transmit

    manager.update(*BERLIN, fix_type=FIX_3D, now=3100.0)
    assert manager.may_transmit
    assert manager.region.code == "EU_868"


def test_re_entry_skips_the_dwell_period(manager):
    """
    Coming back from "no region" there is nothing to thrash against, so the
    balloon should get back on the air immediately rather than staying silent
    for another dwell period.
    """
    settle(manager, DENVER)
    manager.update(*MID_PACIFIC, fix_type=FIX_3D, now=3000.0)
    manager.update(*DENVER, fix_type=FIX_3D, now=3010.0)
    assert manager.may_transmit
    assert manager.region.code == "US"


def test_gps_loss_over_unknown_territory_stays_silent(manager):
    settle(manager, DENVER)
    manager.update(*MID_PACIFIC, fix_type=FIX_3D, now=3000.0)
    manager.update(None, None, fix_type=NO_FIX, now=3100.0)
    assert not manager.may_transmit


def test_null_island_does_not_select_a_region(manager):
    """(0, 0) is what an unset GPS reads."""
    settle(manager, DENVER)
    manager.update(0.0, 0.0, fix_type=FIX_3D, now=3000.0)
    assert not manager.may_transmit


# --- status ---------------------------------------------------------------


def test_status_reports_the_regional_limits(manager):
    settle(manager, BERLIN, start=1000.0)
    manager.update(*BERLIN, fix_type=FIX_3D, now=1200.0)

    status = manager.get_status()
    assert status["region"] == "EU_868"
    assert status["power_limit_dbm"] == 14
    assert status["duty_cycle_percent"] == 10.0
    assert status["auto_switch"] is True


def test_status_is_json_friendly(manager):
    import json

    settle(manager, DENVER)
    json.dumps(manager.get_status())
