"""
Releasing the camera between captures.

The sensor and ISP used to run for the whole flight while being used about one
second in thirty. These tests pin the lifecycle: that the default is unchanged,
that a released camera comes back by itself, and that a failure to restart is
reported rather than silently returning a frame from a stopped pipeline.
"""

import pytest

from airborne.camera import CameraModule
from airborne.config import AirborneConfig
from airborne.params import SPECS_BY_NAME


class FakePicamera2:
    """Enough of Picamera2 to exercise the lifecycle without hardware."""

    def __init__(self):
        self.started = False
        self.starts = 0
        self.stops = 0
        self.frames_taken = 0
        self.fail_next_start = False

    def create_still_configuration(self, **kwargs):
        return {}

    def configure(self, config):
        pass

    def set_controls(self, controls):
        pass

    def start(self):
        if self.fail_next_start:
            raise RuntimeError("sensor did not come up")
        self.started = True
        self.starts += 1

    def stop(self):
        self.started = False
        self.stops += 1

    def close(self):
        self.started = False

    def capture_array(self):
        if not self.started:
            raise RuntimeError("capture from a stopped pipeline")
        self.frames_taken += 1
        import numpy
        return numpy.zeros((16, 16, 3), dtype=numpy.uint8)


@pytest.fixture
def camera(tmp_path):
    def build(release_when_idle, warmup_frames=2):
        cam = CameraModule(resolution=(16, 16), burst_count=1,
                           storage_path=str(tmp_path), callsign="TEST",
                           release_when_idle=release_when_idle,
                           warmup_sec=0.0, warmup_frames=warmup_frames)
        cam._camera = FakePicamera2()
        cam._initialized = True
        cam._streaming = False
        return cam
    return build


class TestDefaultIsUnchanged:
    """Nobody's flight changes behaviour because of a power feature they did
    not ask for."""

    def test_config_defaults_to_off(self):
        assert AirborneConfig().camera_release_when_idle is False

    def test_release_does_nothing_when_disabled(self, camera):
        cam = camera(release_when_idle=False)
        cam._streaming = True
        cam.release()
        assert cam.streaming, "the camera should have been left alone"

    def test_it_is_configurable(self):
        for name in ("camera_release_when_idle", "camera_warmup_sec",
                     "camera_warmup_frames"):
            assert name in SPECS_BY_NAME


class TestLifecycle:
    def test_release_stops_the_pipeline(self, camera):
        cam = camera(release_when_idle=True)
        cam._streaming = True
        cam._camera.started = True
        cam.release()
        assert not cam.streaming
        assert cam._camera.stops == 1

    def test_capture_restarts_a_released_camera(self, camera):
        """
        The restart belongs inside capture(), not in the caller. A capture that
        silently returned a frame from a stopped pipeline would be a miserable
        bug to chase.
        """
        cam = camera(release_when_idle=True)
        assert not cam.streaming
        cam.capture(0.0, 0.0, 0.0)
        assert cam._camera.starts == 1
        assert cam.streaming

    def test_warmup_frames_are_discarded_before_the_kept_one(self, camera):
        cam = camera(release_when_idle=True, warmup_frames=3)
        cam.capture(0.0, 0.0, 0.0)
        # three thrown away, then one kept for the burst of one
        assert cam._camera.frames_taken == 4

    def test_a_failed_restart_reports_rather_than_returning_junk(self, camera):
        cam = camera(release_when_idle=True)
        cam._camera.fail_next_start = True
        assert cam.capture(0.0, 0.0, 0.0) is None
        assert not cam.streaming

    def test_a_camera_that_will_not_stop_does_not_stop_the_flight(self, camera):
        cam = camera(release_when_idle=True)
        cam._streaming = True
        cam._camera.started = True

        def refuse():
            raise RuntimeError("busy")
        cam._camera.stop = refuse

        cam.release()          # must not raise
        assert cam.streaming, "left streaming, which is the safe direction"

    def test_repeated_cycles_do_not_leak_starts(self, camera):
        cam = camera(release_when_idle=True)
        for _ in range(5):
            cam.capture(0.0, 0.0, 0.0)
            cam.release()
        assert cam._camera.starts == 5
        assert cam._camera.stops == 5
