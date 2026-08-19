"""
The Python ground station must fuse positions the same way the macOS app does.

Two ground stations that disagree about where the balloon is are worse than
one, so this pins the same rules the Swift implementation follows.
"""

import sys
import time
from pathlib import Path

import pytest

GS = Path(__file__).resolve().parents[2] / "groundstation/python"
if str(GS) not in sys.path:
    sys.path.insert(0, str(GS))

from raptorhabgs.core.position_fusion import (  # noqa: E402
    PositionFusion, PositionFix, PositionSource, reconcile_timestamp,
)


def _fix(source, lat=40.0, lon=-105.0, alt=1000.0, age=0.0):
    return PositionFix(source=source, latitude=lat, longitude=lon,
                       altitude=alt, timestamp=time.time() - age)


class TestPriority:
    def test_raptor_outranks_meshtastic(self):
        f = PositionFusion()
        f.submit(_fix(PositionSource.MESHTASTIC_DIRECT, lat=41.0))
        f.submit(_fix(PositionSource.RAPTOR, lat=40.0))
        assert f.best().source is PositionSource.RAPTOR

    def test_mqtt_never_displaces_a_fresh_raptor_fix(self):
        f = PositionFusion()
        f.submit(_fix(PositionSource.RAPTOR))
        f.submit(_fix(PositionSource.MESHTASTIC_MQTT, lat=12.0))
        assert f.best().source is PositionSource.RAPTOR

    def test_handover_when_the_higher_source_goes_stale(self):
        f = PositionFusion()
        # RAPTOR is stale after 45s; a 90s-old fix must lose to a fresh mesh one.
        f.submit(_fix(PositionSource.RAPTOR, age=90))
        f.submit(_fix(PositionSource.MESHTASTIC_DIRECT, lat=41.0))
        assert f.best().source is PositionSource.MESHTASTIC_DIRECT


class TestRejectsNonsense:
    def test_null_island_is_rejected(self):
        f = PositionFusion()
        f.submit(_fix(PositionSource.RAPTOR, lat=0.0, lon=0.0))
        assert f.best() is None

    def test_out_of_range_is_rejected(self):
        f = PositionFusion()
        f.submit(_fix(PositionSource.RAPTOR, lat=95.0, lon=-105.0))
        assert f.best() is None


class TestClockHandling:
    """The same defect fixed in the macOS app, pinned here too."""

    def test_unsynced_node_epoch_zero_uses_reception_time(self):
        assert reconcile_timestamp(0.0) == pytest.approx(time.time(), abs=2)

    def test_future_clock_uses_reception_time(self):
        assert reconcile_timestamp(time.time() + 86_400) == pytest.approx(
            time.time(), abs=2)

    def test_plausible_time_is_trusted(self):
        supplied = time.time() - 30
        assert reconcile_timestamp(supplied) == pytest.approx(supplied, abs=0.01)

    def test_a_fix_from_an_unsynced_node_is_usable_on_arrival(self):
        f = PositionFusion()
        f.submit_meshtastic(40.0, -105.0, 1000.0, timestamp=0.0)
        best = f.best()
        assert best is not None and not best.is_stale


class TestDeadReckoning:
    def test_extrapolation_is_labelled_not_disguised(self):
        f = PositionFusion()
        now = time.time()
        f.submit(PositionFix(PositionSource.RAPTOR, 40.0, -105.0, 1000.0, now - 120))
        f.submit(PositionFix(PositionSource.RAPTOR, 40.1, -105.0, 1200.0, now - 110))
        best = f.best()
        assert best.source is PositionSource.DEAD_RECKONING
        assert "extrapolated" in (best.detail or "")

    def test_extrapolation_can_be_disabled(self):
        f = PositionFusion()
        f.extrapolation_enabled = False
        now = time.time()
        f.submit(PositionFix(PositionSource.RAPTOR, 40.0, -105.0, 1000.0, now - 120))
        f.submit(PositionFix(PositionSource.RAPTOR, 40.1, -105.0, 1200.0, now - 110))
        assert f.best().source is PositionSource.RAPTOR   # stale, but honest


def test_track_is_thinned_for_drawing():
    f = PositionFusion()
    for i in range(3000):
        f.submit(_fix(PositionSource.RAPTOR, lat=40.0 + i * 1e-5))
    assert len(f.track(thin_to=500)) <= 1001
