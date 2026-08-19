"""
Power saving, and the limits that keep a long flight from eating itself.

None of this changes what the payload does. It changes how long it can keep
doing it, which on a battery is the same thing.
"""

import sys
from pathlib import Path

import pytest

from airborne.power import (
    PowerAction, PowerReport, apply_flight_power_saving,
)
from airborne.commands import MAX_PENDING_ACKS
from airborne.config import AirborneConfig
from airborne.params import CATEGORY_ORDER, SPECS_BY_NAME


class TestPowerSavingIsOptIn:
    """
    Disabling WiFi takes away SSH. Doing that by surprise to a bench Pi would
    be hostile, so it has to be chosen.
    """

    def test_off_by_default(self):
        assert AirborneConfig().flight_power_saving is False

    def test_the_individual_switches_default_on(self):
        """Once opted in, the operator should not have to enable each one."""
        config = AirborneConfig()
        assert config.power_disable_wifi
        assert config.power_disable_bluetooth
        assert config.power_disable_hdmi
        assert config.power_disable_led

    def test_every_switch_is_in_the_schema(self):
        for name in ("flight_power_saving", "power_disable_wifi",
                     "power_disable_bluetooth", "power_disable_hdmi",
                     "power_disable_led"):
            assert name in SPECS_BY_NAME, f"{name} is not configurable"

    def test_the_category_is_ordered(self):
        """Otherwise the config UIs cannot group them."""
        assert "Power" in CATEGORY_ORDER


class TestPowerSavingNeverStopsTheFlight:
    """
    A payload must not refuse to fly because it could not turn off an LED.
    Every step is individually failure-tolerant.
    """

    def test_missing_tools_do_not_raise(self):
        # On a development machine rfkill and tvservice do not exist, which is
        # the same shape as a stripped-down image in the field.
        report = apply_flight_power_saving()
        assert isinstance(report, PowerReport)

    def test_failures_are_reported_not_hidden(self):
        report = apply_flight_power_saving()
        assert report.actions, "the report must say what was attempted"
        for action in report.actions:
            if not action.applied:
                assert action.detail, "a failure must explain itself"

    def test_saving_only_counts_what_actually_applied(self):
        report = PowerReport(actions=[
            PowerAction("wifi", True, "", 55),
            PowerAction("hdmi", False, "not available", 25),
        ])
        assert report.estimated_saving_ma == 55

    def test_each_subsystem_can_be_left_alone(self):
        report = apply_flight_power_saving(
            disable_wifi_radio=False, disable_bt=False,
            disable_video=False, disable_led=False)
        assert report.actions == []


def test_the_ack_queue_is_bounded():
    """
    Acks drain one per transmit opportunity. Without a cap, a fault that queues
    them faster than the radio sends would grow the list until a 512 MB board
    with no swap gave up.
    """
    from airborne.commands import CommandHandler
    processor = object.__new__(CommandHandler)
    processor._pending_acks = []

    for i in range(MAX_PENDING_ACKS * 3):
        CommandHandler.queue_ack(processor, b"ack", priority=i)

    assert len(processor._pending_acks) == MAX_PENDING_ACKS


def test_the_highest_priority_acks_survive_the_cap():
    """Dropping the newest urgent ack would be exactly the wrong sacrifice."""
    from airborne.commands import CommandHandler
    processor = object.__new__(CommandHandler)
    processor._pending_acks = []

    for priority in range(MAX_PENDING_ACKS * 2):
        CommandHandler.queue_ack(processor, bytes([priority % 256]),
                                   priority=priority)

    priorities = [p for p, _ in processor._pending_acks]
    assert priorities[0] == max(priorities)
    assert min(priorities) > 0, "the low-priority tail should have been dropped"
