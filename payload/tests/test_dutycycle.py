"""
Regional duty-cycle enforcement.

EU 868 and EU 433 cap transmission at 10% of any hour. The payload already
clamps power to the regional ceiling; without this it would respect that and
breach the airtime limit, which is the same kind of violation. From altitude
the balloon covers a whole region, so the consequence is proportionally worse.
"""

import pytest

from common.dutycycle import WINDOW_SEC, DutyCycleTracker


def test_no_limit_means_no_bookkeeping():
    tracker = DutyCycleTracker(100.0)
    assert tracker.unlimited
    for _ in range(1000):
        assert tracker.reserve(60.0)


def test_the_budget_is_a_share_of_the_hour():
    assert DutyCycleTracker(10.0).budget_sec == pytest.approx(360.0)
    assert DutyCycleTracker(1.0).budget_sec == pytest.approx(36.0)


def test_transmission_is_refused_once_the_budget_is_spent():
    tracker = DutyCycleTracker(10.0)          # 360 s per hour
    assert tracker.reserve(300.0, now=1000)
    assert not tracker.reserve(100.0, now=1010), "would exceed 360 s"
    assert tracker.reserve(50.0, now=1010), "fits in what remains"


def test_refusal_happens_before_transmitting_not_after():
    """
    reserve() is the gate. A design that recorded airtime afterwards would
    report violations rather than prevent them.
    """
    tracker = DutyCycleTracker(1.0)           # 36 s per hour
    assert tracker.reserve(30.0, now=1000)
    assert not tracker.reserve(10.0, now=1001)
    assert tracker.status(now=1001).used_sec == pytest.approx(30.0), (
        "a refused transmission must not be charged")


def test_the_window_rolls_rather_than_resetting_on_the_hour():
    """
    A fixed hour would let a transmitter spend its whole allowance in the last
    minute of one hour and the first of the next -- 20% across the two minutes
    that actually matter.
    """
    tracker = DutyCycleTracker(10.0)
    assert tracker.reserve(360.0, now=1000)
    assert not tracker.reserve(10.0, now=1000 + 3599)
    assert tracker.reserve(10.0, now=1000 + 3601), "the oldest airtime has aged out"


def test_old_airtime_expires_gradually():
    tracker = DutyCycleTracker(10.0)
    for i in range(6):
        assert tracker.reserve(60.0, now=1000 + i * 60)
    assert not tracker.reserve(60.0, now=1400)

    # The first 60 s block leaves the window before the rest.
    assert tracker.reserve(60.0, now=1000 + 3601)


def test_settle_corrects_an_estimate_upward():
    tracker = DutyCycleTracker(10.0)
    tracker.reserve(10.0, now=1000)
    tracker.settle(10.0, 14.0, now=1000)
    assert tracker.status(now=1000).used_sec == pytest.approx(14.0)


def test_settle_never_reduces_the_charge():
    """
    The reservation is calculated time-on-air -- what the packet occupies the
    channel for. The measurement is how long our call took. Letting a short
    measurement reduce the charge would drift the budget under true usage, and
    on a fast host would zero it entirely.
    """
    tracker = DutyCycleTracker(10.0)
    tracker.reserve(10.0, now=1000)
    tracker.settle(10.0, 0.001, now=1000)
    assert tracker.status(now=1000).used_sec == pytest.approx(10.0)


def test_release_returns_airtime_for_a_transmission_that_failed():
    """A failed transmit occupies no channel and must not be charged."""
    tracker = DutyCycleTracker(10.0)
    tracker.reserve(100.0, now=1000)
    tracker.release(100.0)
    assert tracker.status(now=1000).used_sec == pytest.approx(0.0)


def test_changing_region_starts_a_fresh_window():
    """
    A duty cycle limits a band. Airtime spent on 906 MHz over the US is not
    EU 868 airtime, so entering Europe must not arrive with the budget already
    spent -- that would silence a balloon that had done nothing wrong there.
    """
    tracker = DutyCycleTracker(100.0)
    tracker.reserve(500.0, now=1000)
    assert tracker.status(now=1000).used_sec == pytest.approx(500.0)

    tracker.set_limit(10.0)
    assert tracker.status(now=1000).used_sec == pytest.approx(0.0)
    assert tracker.reserve(300.0, now=1000), "the new band starts clear"


def test_setting_the_same_limit_does_not_reset_the_window():
    """Re-applying the same region must not become a way to clear the budget."""
    tracker = DutyCycleTracker(10.0)
    tracker.reserve(300.0, now=1000)
    tracker.set_limit(10.0)
    assert tracker.status(now=1000).used_sec == pytest.approx(300.0)


def test_airtime_is_recorded_even_where_no_limit_applies():
    """So a flight report is meaningful in every region, not just restricted ones."""
    tracker = DutyCycleTracker(100.0)
    tracker.reserve(120.0, now=1000)
    assert tracker.status(now=1000).used_sec == pytest.approx(120.0)


def test_blocked_transmissions_are_counted():
    tracker = DutyCycleTracker(1.0)
    tracker.reserve(36.0, now=1000)
    for _ in range(3):
        tracker.reserve(1.0, now=1000)
    assert tracker.status(now=1000).blocked == 3


def test_status_is_json_friendly():
    import json

    tracker = DutyCycleTracker(10.0)
    tracker.reserve(36.0, now=1000)
    status = tracker.get_status()
    json.dumps(status)
    assert status["enforced"] is True
    assert status["limit_percent"] == 10.0


def test_a_zero_percent_limit_blocks_everything():
    """A region we may not transmit in at all."""
    tracker = DutyCycleTracker(0.0)
    assert not tracker.reserve(0.001)


def test_the_tracker_is_thread_safe():
    """The beacon scheduler, the repeater and receive windows all share it."""
    import threading

    tracker = DutyCycleTracker(50.0)          # 1800 s
    granted = []

    def worker():
        for _ in range(50):
            granted.append(tracker.reserve(1.0))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert sum(granted) == 400, "all 400 one-second slots fit in 1800 s"
    assert tracker.status().used_sec == pytest.approx(400.0, abs=1.0)
