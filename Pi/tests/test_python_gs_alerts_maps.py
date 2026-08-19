"""
Alert logic and tile maths for the Python ground station.

Alerts are tested without making noise: the player is replaced, so this asserts
on *when* the ground station decides to alert, which is the part that matters.
"""

import sys
import time
from pathlib import Path

import pytest

GS = Path(__file__).resolve().parents[2] / "RaptorHABGS_Python"
if str(GS) not in sys.path:
    sys.path.insert(0, str(GS))

from raptorhabgs.core.audio_alerts import (  # noqa: E402
    AudioAlertManager, AudioAlertConfig, AlertType,
)
from raptorhabgs.core.offline_maps import (  # noqa: E402
    deg_to_tile, tiles_for_region, OfflineMapManager,
)


@pytest.fixture
def manager():
    m = AudioAlertManager(AudioAlertConfig(enabled=True))
    played = []
    m.player.play = lambda alert, volume=0.7: played.append(alert) or True
    m.player.speak = lambda message: None
    m.played = played
    return m


class TestWhatWarrantsANoise:
    def test_urgent_alerts_are_on_by_default(self):
        for alert in (AlertType.BURST, AlertType.LANDING,
                      AlertType.SIGNAL_LOST, AlertType.LOW_BATTERY):
            assert alert.default_enabled, f"{alert} should default to on"

    def test_routine_alerts_are_off_by_default(self):
        """An alert that fires constantly is one the operator learns to ignore."""
        for alert in (AlertType.TELEMETRY_RECEIVED, AlertType.IMAGE_RECEIVED,
                      AlertType.ALTITUDE_MILESTONE, AlertType.SIGNAL_RESTORED):
            assert not alert.default_enabled, f"{alert} should default to off"


class TestBurstDetection:
    def test_burst_fires_on_a_real_descent(self, manager):
        manager.config.per_alert["BURST"] = True
        manager.on_telemetry(altitude_m=25000, vertical_rate_mps=5.0)
        manager.on_telemetry(altitude_m=30000, vertical_rate_mps=4.0)
        manager.on_telemetry(altitude_m=29000, vertical_rate_mps=-20.0)
        assert AlertType.BURST in manager.played

    def test_burst_does_not_fire_on_the_ground(self, manager):
        """A payload jostled on the pad must not announce burst."""
        manager.config.per_alert["BURST"] = True
        manager.on_telemetry(altitude_m=50, vertical_rate_mps=-8.0)
        assert AlertType.BURST not in manager.played

    def test_burst_announces_once(self, manager):
        manager.config.per_alert["BURST"] = True
        manager.on_telemetry(altitude_m=30000, vertical_rate_mps=2.0)
        for _ in range(5):
            manager.on_telemetry(altitude_m=20000, vertical_rate_mps=-25.0)
        assert manager.played.count(AlertType.BURST) == 1


class TestBatteryAlert:
    def test_unknown_battery_does_not_cry_wolf(self, manager):
        """
        The payload reports 0 mV when no monitor is fitted. Treating that as a
        flat battery would alert for the entire flight.
        """
        manager.on_telemetry(altitude_m=1000, battery_mv=0)
        assert AlertType.LOW_BATTERY not in manager.played

    def test_genuinely_low_battery_alerts(self, manager):
        manager.on_telemetry(altitude_m=1000, battery_mv=3200)
        assert AlertType.LOW_BATTERY in manager.played


class TestSignalLoss:
    def test_silence_is_noticed(self, manager):
        manager.config.signal_lost_after_sec = 0.2
        manager.on_telemetry(altitude_m=1000)
        time.sleep(0.3)
        manager.check_signal()
        assert AlertType.SIGNAL_LOST in manager.played

    def test_restoration_is_announced(self, manager):
        manager.config.per_alert["SIGNAL_RESTORED"] = True
        manager.config.signal_lost_after_sec = 0.2
        manager.on_telemetry(altitude_m=1000)
        time.sleep(0.3)
        manager.check_signal()
        manager.on_telemetry(altitude_m=1100)
        assert AlertType.SIGNAL_RESTORED in manager.played

    def test_signal_loss_announces_once_not_repeatedly(self, manager):
        manager.config.signal_lost_after_sec = 0.2
        manager.on_telemetry(altitude_m=1000)
        time.sleep(0.3)
        for _ in range(5):
            manager.check_signal()
        assert manager.played.count(AlertType.SIGNAL_LOST) == 1


def test_rearm_suppresses_a_flapping_threshold(manager):
    """A value oscillating around a threshold must not produce a beep stream."""
    for _ in range(10):
        manager.play(AlertType.LOW_BATTERY, "low")
    assert manager.played.count(AlertType.LOW_BATTERY) == 1


def test_flight_reset_rearms_milestones(manager):
    manager.config.per_alert["ALTITUDE_MILESTONE"] = True
    manager.on_telemetry(altitude_m=5000)
    first = manager.played.count(AlertType.ALTITUDE_MILESTONE)
    manager.reset_flight()
    manager._last_played.clear()
    manager.on_telemetry(altitude_m=5000)
    assert manager.played.count(AlertType.ALTITUDE_MILESTONE) > first


class TestTileMaths:
    def test_known_tile(self):
        # Boulder, Colorado at zoom 12.
        assert deg_to_tile(40.015, -105.27, 12) == (850, 1550)

    def test_zoom_zero_is_a_single_tile(self):
        assert deg_to_tile(51.5, -0.1, 0) == (0, 0)

    def test_latitude_is_clamped_to_the_projection(self):
        """Web Mercator cannot represent the poles; it must clamp, not crash."""
        x, y = deg_to_tile(89.9, 0.0, 4)
        assert 0 <= y < 2 ** 4

    def test_region_grows_with_zoom(self):
        small = tiles_for_region(40.0, -105.0, 5.0, 10, 10)
        large = tiles_for_region(40.0, -105.0, 5.0, 10, 12)
        assert len(large) > len(small)

    def test_a_huge_region_is_refused_without_acknowledgement(self, tmp_path):
        """OSM's tiles are donated; a bulk pull needs a deliberate decision."""
        manager = OfflineMapManager(tmp_path / "tiles.mbtiles")
        with pytest.raises(ValueError, match="large"):
            manager.download_region(40.0, -105.0, 200.0, 5, 16)

    def test_cache_round_trip(self, tmp_path):
        manager = OfflineMapManager(tmp_path / "tiles.mbtiles")
        manager.cache.put(12, 850, 1550, b"\x89PNG-tile-data")
        assert manager.cache.get(12, 850, 1550) == b"\x89PNG-tile-data"
        assert manager.cache.has(12, 850, 1550)
        assert manager.cache.stats()["tiles"] == 1

    def test_offline_miss_returns_nothing_rather_than_blocking(self, tmp_path):
        manager = OfflineMapManager(tmp_path / "tiles.mbtiles", allow_network=False)
        assert manager.get_tile(12, 1, 1) is None
