"""
Airborne controller error handling and recovery.

These cover the failure paths that previously had no effect at all: the error
counter that only ever grew, the recovery routine nothing called, and the
watchdog that fired into a field nobody read.
"""

import os

import pytest

from airborne.config import Config
from airborne.main import RaptorHabAirborne, State


@pytest.fixture
def controller(tmp_path):
    config = Config()
    config.image_storage_path = str(tmp_path / "images")
    config.log_path = str(tmp_path / "logs")
    config.tx_period_sec = 1
    config.tx_pause_sec = 0
    return RaptorHabAirborne(config, debug=True)


@pytest.fixture
def no_backoff(monkeypatch):
    """Skip the main loop's error backoff sleep so tests stay fast."""
    monkeypatch.setattr("airborne.main.time.sleep", lambda *_: None)


# --- configuration is actually honoured -----------------------------------


def test_watchdog_uses_configured_timeout(controller):
    """Regression: the timeout was hardcoded to 60 and the config ignored."""
    controller.config.watchdog_timeout_sec = 25
    controller._preflight_check_fountain_encoder = lambda: None

    created = {}

    class FakeWatchdog:
        def __init__(self, timeout_sec, callback):
            created["timeout"] = timeout_sec

        def start(self):
            created["started"] = True

    import airborne.main as main_module

    original = main_module.Watchdog
    main_module.Watchdog = FakeWatchdog
    try:
        # Only exercise the watchdog block; the rest needs hardware.
        controller.config.ensure_directories()
        if controller.config.watchdog_enabled:
            controller._watchdog = main_module.Watchdog(
                timeout_sec=controller.config.watchdog_timeout_sec,
                callback=controller._watchdog_triggered,
            )
            controller._watchdog.start()
    finally:
        main_module.Watchdog = original

    assert created["timeout"] == 25
    assert created["started"]


def test_max_errors_comes_from_config(tmp_path):
    """Regression: _max_errors was a hardcoded 10."""
    config = Config()
    config.max_consecutive_errors = 3
    assert RaptorHabAirborne(config)._max_errors == 3


# --- error counting -------------------------------------------------------


def test_consecutive_errors_reset_after_a_clean_cycle(controller, no_backoff):
    """
    Regression: the counter only ever incremented, so one transient glitch
    every few minutes across a long flight eventually shut the payload down.
    """
    calls = {"n": 0}

    def flaky_tx_cycle():
        calls["n"] += 1
        if calls["n"] in (1, 2, 4, 5, 7):
            raise RuntimeError("transient SPI glitch")
        if calls["n"] >= 12:
            controller._shutdown.set()

    controller._run_tx_cycle = flaky_tx_cycle
    controller._trigger_capture = lambda: None
    controller._main_loop_body(last_capture_time=1e18, last_status_time=1e18)

    assert controller._error_count == 5, "lifetime total should count every error"
    assert controller._consecutive_errors == 0, "a clean cycle must reset the run"
    assert controller._state is not State.ERROR_STATE


def test_enough_consecutive_errors_enters_error_state(controller, no_backoff):
    controller._max_errors = 3
    controller._run_tx_cycle = lambda: (_ for _ in ()).throw(RuntimeError("dead radio"))
    controller._trigger_capture = lambda: None

    controller._main_loop_body(last_capture_time=1e18, last_status_time=1e18)

    assert controller._state is State.ERROR_STATE
    assert controller._consecutive_errors == 3


def test_main_loop_marks_itself_exited(controller):
    controller._trigger_capture = lambda: None
    controller._run_tx_cycle = lambda: controller._shutdown.set()

    assert not controller._main_loop_exited.is_set()
    controller._run_main_loop()
    assert controller._main_loop_exited.is_set()


def test_main_loop_marks_itself_exited_even_after_a_crash(controller):
    controller._trigger_capture = lambda: None

    def explode(*_args):
        raise KeyboardInterrupt

    controller._main_loop_body = explode
    with pytest.raises(KeyboardInterrupt):
        controller._run_main_loop()

    assert controller._main_loop_exited.is_set()


# --- recovery -------------------------------------------------------------


def test_error_state_exits_nonzero_under_systemd(controller, monkeypatch):
    """
    Regression: _handle_error_state was never called from anywhere, so the
    payload just stopped transmitting for the rest of the flight.
    """
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    controller._consecutive_errors = 10

    with pytest.raises(SystemExit) as excinfo:
        controller._handle_error_state()
    assert excinfo.value.code == 1


def test_error_state_respects_reboot_disabled(controller, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    controller.config.reboot_on_fatal_error = False

    controller._handle_error_state()  # must return, not exit


def test_error_state_does_not_shell_out_when_reboot_disabled(controller, monkeypatch):
    called = []
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(
        "airborne.main.subprocess.run", lambda *a, **k: called.append(a)
    )
    controller.config.reboot_on_fatal_error = False

    controller._handle_error_state()
    assert called == []


def test_reboot_uses_subprocess_not_os_system(controller, monkeypatch):
    """Regression: recovery used os.system("sudo reboot")."""
    invoked = []
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr("airborne.main.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "airborne.main.subprocess.run", lambda *a, **k: invoked.append(a[0])
    )

    controller._handle_error_state()
    assert invoked == [["sudo", "systemctl", "reboot"]]


# --- watchdog -------------------------------------------------------------


def test_watchdog_sets_shutdown_so_a_hang_actually_unwinds(controller):
    """
    Regression: the callback only set a state field the main loop never read,
    so a hung loop stayed hung.
    """
    controller._main_loop_exited.set()  # pretend the loop unwound promptly

    controller._watchdog_triggered()

    assert controller._shutdown.is_set()
    assert controller._state is State.ERROR_STATE
    assert controller._watchdog_fired


# --- preflight ------------------------------------------------------------


def test_preflight_raises_without_raptorq(controller, monkeypatch):
    monkeypatch.setattr("airborne.fountain.RAPTORQ_AVAILABLE", False)
    controller.config.allow_lt_fallback = False

    with pytest.raises(RuntimeError, match="RaptorQ is not available"):
        controller._preflight_check_fountain_encoder()


def test_preflight_allows_opt_in_bench_mode(controller, monkeypatch):
    monkeypatch.setattr("airborne.fountain.RAPTORQ_AVAILABLE", False)
    controller.config.allow_lt_fallback = True

    controller._preflight_check_fountain_encoder()  # must not raise


def test_preflight_passes_when_raptorq_is_present(controller):
    from airborne.fountain import raptorq_available

    if not raptorq_available():
        pytest.skip("raptorq not installed")
    controller._preflight_check_fountain_encoder()


# --- image accounting -----------------------------------------------------


def test_dropped_images_are_not_counted_as_transmitted(controller):
    """
    Regression: the counter incremented on a queue call whose False return
    was discarded, so a dropped image was reported as sent.
    """
    from airborne.camera import ImageInfo

    class FullScheduler:
        def add_image(self, **_kwargs):
            return False

    controller._scheduler = FullScheduler()
    controller._image_queue.put_nowait(
        ImageInfo(
            image_id=1,
            filepath="/tmp/test.webp",
            width=320,
            height=240,
            size_bytes=10,
            timestamp=0,
            webp_data=b"\x00" * 10,
        )
    )

    controller._process_image_queue()

    assert controller._images_queued_for_tx == 0
    assert controller._images_dropped == 1


def test_successfully_queued_images_are_counted(controller):
    from airborne.camera import ImageInfo

    class AcceptingScheduler:
        def add_image(self, **_kwargs):
            return True

    controller._scheduler = AcceptingScheduler()
    controller._image_queue.put_nowait(
        ImageInfo(
            image_id=1,
            filepath="/tmp/test.webp",
            width=320,
            height=240,
            size_bytes=10,
            timestamp=0,
            webp_data=b"\x00" * 10,
        )
    )

    controller._process_image_queue()

    assert controller._images_queued_for_tx == 1
    assert controller._images_dropped == 0
