"""
Regression tests for defects found in the post-v2 flight-code review.

Each of these is a silent failure: nothing raises, nothing logs an error, and
the payload carries on reporting success while doing the wrong thing.
"""

import pytest

from airborne.utils import get_battery_voltage, BATTERY_UNKNOWN
from airborne.zone_manager import ZoneManager, Zone


class TestBatteryIsNotInvented:
    """A fabricated battery reading is worse than no reading."""

    def test_reports_unknown_rather_than_a_full_cell(self):
        assert get_battery_voltage() == BATTERY_UNKNOWN

    def test_unknown_is_falsy_so_consumers_omit_the_percentage(self):
        # main.py guards the derived percentage with `if millivolts:`, and the
        # Meshtastic beacon only reports a level it was given. A truthy
        # placeholder would publish a permanent 100% to the whole mesh.
        assert not get_battery_voltage()


class TestLandedIsSticky:
    """
    A landed payload must not fall back to CRUISE.

    CRUISE beacons every five minutes; LANDED every one. The whole point of
    LANDED is the recovery crew, and the conditions that break it -- movement
    on the ground -- are the normal conditions of being on the ground.
    """

    LAUNCH = (40.0, -105.0)
    FIX_3D = 3

    def _landed_manager(self):
        zm = ZoneManager(
            launch_latitude=self.LAUNCH[0],
            launch_longitude=self.LAUNCH[1],
            launch_altitude_m=0.0,
            auto_capture_launch_point=False,
            landed_arm_altitude_m=2000.0,
            landed_altitude_m=1000.0,
            landed_vertical_rate_mps=0.5,
            landed_dwell_sec=120.0,
        )
        now = 1000.0
        # Fly high enough to arm landing detection.
        zm.update(*self.LAUNCH, altitude_m=5000.0, fix_type=self.FIX_3D, now=now)
        # Come down and sit still for longer than the dwell period.
        for step in range(0, 300, 20):
            zm.update(*self.LAUNCH, altitude_m=10.0, fix_type=self.FIX_3D,
                      now=now + 10 + step)
        assert zm.state.zone is Zone.LANDED, "setup failed to reach LANDED"
        return zm, now + 320

    def _feed(self, zm, altitudes, start):
        """Feed altitudes 10s apart, which is what produces a vertical rate."""
        now = start
        for altitude in altitudes:
            now += 10.0
            zm.update(*self.LAUNCH, altitude_m=altitude, fix_type=self.FIX_3D, now=now)
        return now

    def test_wind_in_a_tree_does_not_unland_it(self):
        """A payload swinging in a tree exceeds the stationary threshold."""
        zm, now = self._landed_manager()
        self._feed(zm, [10.0, 40.0, 12.0, 45.0, 11.0], now)
        assert zm.state.zone is Zone.LANDED

    def test_being_picked_up_does_not_unland_it(self):
        zm, now = self._landed_manager()
        self._feed(zm, [10.0, 60.0, 120.0, 180.0], now)
        assert zm.state.zone is Zone.LANDED

    def test_a_lost_fix_holds_the_zone(self):
        """No fix means keep doing what you were doing, not revert."""
        zm, now = self._landed_manager()
        zm.update(None, None, None, fix_type=0, now=now + 10)
        assert zm.state.zone is Zone.LANDED

    def test_genuine_flight_does_release_it(self):
        """Sticky must not mean permanently wrong."""
        zm, now = self._landed_manager()
        self._feed(zm, [500.0, 1500.0, 2500.0, 3000.0], now)
        assert zm.state.zone is not Zone.LANDED


class TestRadioModeNeverLies:
    """
    Mode bookkeeping that disagrees with the chip transmits into the void.

    ensure_gfsk() returns early when it believes it is already in GFSK, so a
    failed switch that left the mode saying GFSK would transmit FSK-framed
    bytes at whatever the chip was actually configured for -- successfully, and
    silently.
    """

    def test_failed_lora_switch_marks_the_mode_unknown(self):
        from common.radio_lora import RadioMode, LoRaConfig
        from common.radio_manager import RadioModeManager

        class ExplodingRadio:
            def __init__(self):
                self.restored = 0

            def configure_lora(self, **kwargs):
                raise IOError("SPI went away")

            def restore_gfsk(self):
                self.restored += 1

            def set_tx_power(self, dbm):
                pass

        radio = ExplodingRadio()
        manager = RadioModeManager(radio, gfsk_tx_power_dbm=22)
        manager.set_lora_settings(
            config=LoRaConfig(), frequency_mhz=906.875, requested_power_dbm=14)

        with pytest.raises(IOError):
            manager.ensure_lora()

        assert manager.mode is RadioMode.UNCONFIGURED, (
            "a failed switch must not leave the mode claiming to know the chip")

        # And the next GFSK request must actually reconfigure rather than
        # short-circuit on a stale belief.
        manager.ensure_gfsk()
        assert radio.restored == 1
        assert manager.mode is RadioMode.GFSK
