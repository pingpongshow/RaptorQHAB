"""
Region tracking: when the balloon retunes, when it holds, and when it must
stop transmitting altogether.

The safety-critical behaviour is the last one. Over territory with no known
band plan there is no correct frequency, and picking one anyway risks
transmitting outside what is permitted.
"""

import pytest

from airborne.region_manager import RegionManager, RegionSource

# The default front end is the Waveshare Core1262-HF (850-930 MHz), which
# reaches every Meshtastic region except the 433 MHz ones and China. Kyiv sits
# on UA_868 (868.375 MHz, reachable); Beijing is on CN (478.875 MHz) and is the
# "hardware cannot reach this" case for an HF board.
DENVER = (39.74, -104.99)      # US        906.875 MHz  reachable
TOKYO = (35.68, 139.69)        # JP        925.675 MHz  reachable
SYDNEY = (-33.87, 151.21)      # ANZ       919.875 MHz  reachable
BERLIN = (52.52, 13.40)        # EU_868    869.525 MHz  reachable on HF
BEIJING = (39.90, 116.41)      # CN        478.875 MHz  NOT reachable on HF
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
    manual = RegionManager(home_region_code="JP", auto_switch=False)
    manual.update(*DENVER, fix_type=FIX_3D)
    assert manual.region.code == "JP"
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

    manager.update(*TOKYO, fix_type=FIX_3D, now=2000.0)
    assert manager.region.code == "US", "must not switch instantly"
    assert manager.get_status()["pending_candidate"] == "JP"

    manager.update(*TOKYO, fix_type=FIX_3D, now=2000.0 + 30)
    assert manager.region.code == "US", "dwell period not yet elapsed"

    manager.update(*TOKYO, fix_type=FIX_3D, now=2000.0 + 61)
    assert manager.region.code == "JP"
    assert manager.state.source is RegionSource.GPS


def test_returning_before_the_dwell_elapses_cancels_the_change(manager):
    """A balloon wobbling across a border must not thrash between bands."""
    settle(manager, DENVER)

    manager.update(*TOKYO, fix_type=FIX_3D, now=2000.0)
    assert manager.get_status()["pending_candidate"] == "JP"

    manager.update(*DENVER, fix_type=FIX_3D, now=2030.0)
    assert manager.region.code == "US"
    assert manager.get_status()["pending_candidate"] is None


def test_edge_margin_blocks_a_change_near_a_boundary():
    """Just inside a new region does not count; you must be well inside."""
    manager = RegionManager(dwell_sec=0.0, edge_margin_km=200.0)
    settle(manager, DENVER, fix=FIX_3D)

    # Just inside the southern edge of the Japan box.
    manager.update(24.5, 130.0, fix_type=FIX_3D, now=3000.0)
    manager.update(24.5, 130.0, fix_type=FIX_3D, now=3100.0)
    assert manager.region.code == "US"


def test_edge_margin_of_zero_disables_the_test():
    manager = RegionManager(dwell_sec=0.0, edge_margin_km=0)
    settle(manager, DENVER)
    manager.update(*TOKYO, fix_type=FIX_3D, now=3000.0)
    manager.update(*TOKYO, fix_type=FIX_3D, now=3001.0)
    assert manager.region.code == "JP"


def test_a_third_region_restarts_the_dwell(manager):
    settle(manager, DENVER)

    manager.update(*TOKYO, fix_type=FIX_3D, now=2000.0)
    manager.update(*SYDNEY, fix_type=FIX_3D, now=2030.0)
    assert manager.get_status()["pending_candidate"] == "ANZ"

    # The Japan dwell must not carry over to Australia.
    manager.update(*SYDNEY, fix_type=FIX_3D, now=2070.0)
    assert manager.region.code == "US"

    manager.update(*SYDNEY, fix_type=FIX_3D, now=2095.0)
    assert manager.region.code == "ANZ"


# --- fix quality ----------------------------------------------------------


def test_a_2d_fix_does_not_drive_a_region_change(manager):
    settle(manager, DENVER)
    manager.update(*TOKYO, fix_type=FIX_2D, now=2000.0)
    manager.update(*TOKYO, fix_type=FIX_2D, now=2100.0)
    assert manager.region.code == "US"


def test_2d_fixes_are_accepted_when_configured_to():
    manager = RegionManager(dwell_sec=0.0, edge_margin_km=0, require_3d_fix=False)
    manager.update(*TOKYO, fix_type=FIX_2D, now=1000.0)
    manager.update(*TOKYO, fix_type=FIX_2D, now=1001.0)
    assert manager.region.code == "JP"


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
    manager.update(*TOKYO, fix_type=FIX_3D, now=2000.0)
    assert manager.get_status()["pending_candidate"] == "JP"

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

    manager.update(*TOKYO, fix_type=FIX_3D, now=3100.0)
    assert manager.may_transmit
    assert manager.region.code == "JP"


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
    settle(manager, TOKYO, start=1000.0)
    manager.update(*TOKYO, fix_type=FIX_3D, now=1200.0)

    status = manager.get_status()
    assert status["region"] == "JP"
    assert status["power_limit_dbm"] == 13
    assert status["frequency_mhz"] == pytest.approx(925.675, abs=0.0005)
    assert status["auto_switch"] is True


def test_status_is_json_friendly(manager):
    import json

    settle(manager, DENVER)
    json.dumps(manager.get_status())


# --- hardware band limits -------------------------------------------------
#
# The SX1262 die covers 150-960 MHz, but the board's matching network, filters
# and PA are tuned for one range. An HF board driven at 433 MHz radiates almost
# nothing into a badly matched load and risks damaging the amplifier, so a
# region the hardware cannot reach must be as unavailable as unmapped ocean.


def test_hf_board_reaches_the_sub_ghz_regions_but_not_433_or_china():
    from common.meshtastic.regions import HARDWARE_BANDS, regions_within_band

    reachable = {r.code for r in regions_within_band(HARDWARE_BANDS["HF"])}
    for expected in ("US", "EU_868", "JP", "ANZ", "IN", "RU", "BR_902"):
        assert expected in reachable
    for unreachable in ("EU_433", "UA_433", "MY_433", "PH_433", "CN"):
        assert unreachable not in reachable


def test_flying_over_an_unreachable_region_suspends_transmission(manager):
    """China on an HF board: go quiet rather than transmit at 478 MHz."""
    settle(manager, DENVER)
    assert manager.may_transmit

    manager.update(*BEIJING, fix_type=FIX_3D, now=3000.0)

    assert not manager.may_transmit
    assert manager.state.source is RegionSource.UNSUPPORTED
    assert manager.active_frequency_mhz is None


def test_unreachable_region_takes_effect_without_a_dwell_period(manager):
    """No grace period for transmitting out of band."""
    settle(manager, DENVER)
    manager.update(*BEIJING, fix_type=FIX_3D, now=3000.0)
    assert not manager.may_transmit


def test_leaving_an_unreachable_region_restores_transmission(manager):
    settle(manager, DENVER)
    manager.update(*BEIJING, fix_type=FIX_3D, now=3000.0)
    assert not manager.may_transmit

    manager.update(*TOKYO, fix_type=FIX_3D, now=3100.0)
    assert manager.may_transmit
    assert manager.region.code == "JP"


def test_an_lf_board_reaches_china_and_not_america():
    from common.meshtastic.regions import HARDWARE_BANDS

    manager = RegionManager(
        home_region_code="CN",
        dwell_sec=0.0,
        edge_margin_km=0,
        hardware_band=HARDWARE_BANDS["LF"],
    )
    assert manager.may_transmit
    assert manager.active_frequency_mhz == pytest.approx(478.875, abs=0.0005)

    manager.update(*DENVER, fix_type=FIX_3D, now=1000.0)
    assert not manager.may_transmit, "an LF board cannot transmit at 906 MHz"


def test_an_hf_board_reaches_both_america_and_europe():
    """The practical benefit of the HF front end: no mid-Atlantic blackout."""
    manager = RegionManager(dwell_sec=0.0, edge_margin_km=0)

    manager.update(*DENVER, fix_type=FIX_3D, now=1000.0)
    assert manager.may_transmit
    assert manager.active_frequency_mhz == pytest.approx(906.875, abs=0.0005)

    manager.update(*BERLIN, fix_type=FIX_3D, now=2000.0)
    manager.update(*BERLIN, fix_type=FIX_3D, now=2001.0)
    assert manager.may_transmit
    assert manager.region.code == "EU_868"
    assert manager.active_frequency_mhz == pytest.approx(869.525, abs=0.0005)


def test_an_unreachable_home_region_refuses_to_transmit_from_the_start():
    """
    Misconfiguration is caught at startup rather than producing a payload that
    silently never beacons.
    """
    manager = RegionManager(home_region_code="EU_433", auto_switch=False)
    assert not manager.may_transmit
    assert manager.state.source is RegionSource.UNSUPPORTED


def test_supported_regions_are_reported_for_the_ui(manager):
    codes = manager.supported_region_codes
    assert "US" in codes
    assert "EU_868" in codes
    assert "EU_433" not in codes
    assert codes == sorted(codes)


def test_active_frequency_matches_the_adopted_region(manager):
    settle(manager, DENVER)
    assert manager.active_frequency_mhz == pytest.approx(906.875, abs=0.0005)

    manager.update(*TOKYO, fix_type=FIX_3D, now=4000.0)
    manager.update(*TOKYO, fix_type=FIX_3D, now=4100.0)
    assert manager.active_frequency_mhz == pytest.approx(925.675, abs=0.0005)


def test_status_reports_the_hardware_band(manager):
    status = manager.get_status()
    assert "850" in status["hardware_band"]
    assert "EU_433" not in status["supported_regions"]
    assert "EU_868" in status["supported_regions"]
