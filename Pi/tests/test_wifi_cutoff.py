"""
The in-flight WiFi cutoff.

WiFi is the largest controllable draw on the payload and at altitude it is
worse than useless -- there is no access point up there, so NetworkManager
scans and fails for the whole flight. But it cannot be off at boot, because the
pre-launch checklist is run over it. So it stays up until the balloon proves it
has launched.

"Proves" is the load-bearing word, and these tests are mostly about what is not
good enough proof.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from airborne.power import LaunchWiFiCutoff, PowerAction


class Radio:
    """Stands in for the real radio so no test can take the host's WiFi down."""

    def __init__(self, succeeds=True):
        self.calls = 0
        self.succeeds = succeeds

    def __call__(self):
        self.calls += 1
        return PowerAction(
            "wifi", self.succeeds, "test" if self.succeeds else "not authorized", 55
        )


def cutoff(**kwargs):
    kwargs.setdefault("altitude_agl_m", 300.0)
    kwargs.setdefault("confirmations_needed", 3)
    return LaunchWiFiCutoff(**kwargs)


# -- what counts as launched ------------------------------------------------

def test_fires_after_sustained_altitude_with_a_3d_fix():
    radio = Radio()
    c = cutoff(action=radio)

    for _ in range(2):
        assert c.update(altitude_agl_m=500.0, fix_type=2) is None
        assert not c.fired

    action = c.update(altitude_agl_m=500.0, fix_type=2)
    assert action is not None and action.applied
    assert c.fired
    assert radio.calls == 1


def test_a_2d_fix_is_never_enough():
    """
    A 2D fix reports an altitude it did not solve for. Cutting WiFi is
    irreversible for the flight, so it is exactly the wrong thing to trigger on
    a number the receiver did not compute.
    """
    radio = Radio()
    c = cutoff(action=radio)

    for _ in range(20):
        c.update(altitude_agl_m=5000.0, fix_type=1)

    assert not c.fired
    assert radio.calls == 0


def test_no_fix_is_never_enough():
    radio = Radio()
    c = cutoff(action=radio)
    for _ in range(20):
        c.update(altitude_agl_m=5000.0, fix_type=0)
    assert not c.fired


def test_sitting_on_the_pad_does_not_fire():
    radio = Radio()
    c = cutoff(action=radio)
    for _ in range(200):
        c.update(altitude_agl_m=0.4, fix_type=2)
    assert not c.fired
    assert radio.calls == 0


def test_a_single_glitched_fix_does_not_fire():
    """One bad altitude should not end the flight's connectivity."""
    radio = Radio()
    c = cutoff(action=radio)

    c.update(altitude_agl_m=2.0, fix_type=2)
    c.update(altitude_agl_m=9000.0, fix_type=2)   # glitch
    c.update(altitude_agl_m=1.5, fix_type=2)

    assert not c.fired
    assert radio.calls == 0


def test_confirmations_must_be_consecutive():
    radio = Radio()
    c = cutoff(action=radio)

    c.update(altitude_agl_m=400.0, fix_type=2)
    c.update(altitude_agl_m=400.0, fix_type=2)
    c.update(altitude_agl_m=10.0, fix_type=2)     # back down: count resets
    c.update(altitude_agl_m=400.0, fix_type=2)
    c.update(altitude_agl_m=400.0, fix_type=2)

    assert not c.fired


def test_a_gap_in_coverage_does_not_count_against_it():
    """
    Losing the fix is not evidence of not having launched. The count is held,
    not reset, so a balloon climbing through patchy coverage still triggers.
    """
    radio = Radio()
    c = cutoff(action=radio)

    c.update(altitude_agl_m=400.0, fix_type=2)
    c.update(altitude_agl_m=400.0, fix_type=2)
    c.update(altitude_agl_m=None, fix_type=0)     # lost it
    action = c.update(altitude_agl_m=420.0, fix_type=2)

    assert action is not None and c.fired


def test_missing_altitude_does_not_fire():
    radio = Radio()
    c = cutoff(action=radio)
    for _ in range(10):
        c.update(altitude_agl_m=None, fix_type=2)
    assert not c.fired


# -- the latch --------------------------------------------------------------

def test_it_only_fires_once():
    radio = Radio()
    c = cutoff(action=radio)

    for _ in range(50):
        c.update(altitude_agl_m=8000.0, fix_type=2)

    assert radio.calls == 1


def test_descent_does_not_bring_wifi_back():
    """Off for the flight means off, including all the way to the ground."""
    radio = Radio()
    c = cutoff(action=radio)

    for _ in range(3):
        c.update(altitude_agl_m=5000.0, fix_type=2)
    assert c.fired

    for altitude in (4000.0, 2000.0, 500.0, 50.0, 0.0):
        assert c.update(altitude_agl_m=altitude, fix_type=2) is None

    assert radio.calls == 1


def test_disabled_never_fires():
    radio = Radio()
    c = cutoff(action=radio, enabled=False)
    for _ in range(20):
        c.update(altitude_agl_m=9000.0, fix_type=2)
    assert not c.fired
    assert radio.calls == 0


# -- when the radio refuses -------------------------------------------------

def test_a_failed_cutoff_still_latches_and_is_reported():
    """
    If the sudoers rule is missing the payload cannot turn WiFi off. It must
    say so rather than retrying forever, and the status has to make the cause
    findable.
    """
    radio = Radio(succeeds=False)
    c = cutoff(action=radio)

    for _ in range(3):
        c.update(altitude_agl_m=500.0, fix_type=2)

    assert c.fired
    assert radio.calls == 1
    status = c.get_status()
    assert status["fired"]
    assert "stayed up" in status["detail"]


# -- status -----------------------------------------------------------------

def test_status_shows_progress_towards_firing():
    c = cutoff(action=Radio())
    c.update(altitude_agl_m=500.0, fix_type=2)

    status = c.get_status()
    assert status["armed"] and not status["fired"]
    assert status["confirmations"] == 1
    assert status["confirmations_needed"] == 3
    assert status["threshold_agl_m"] == 300.0


def test_threshold_is_configurable():
    radio = Radio()
    c = cutoff(action=radio, altitude_agl_m=1000.0, confirmations_needed=1)

    assert c.update(altitude_agl_m=999.0, fix_type=2) is None
    assert c.update(altitude_agl_m=1001.0, fix_type=2) is not None


# -- the setup that makes it possible at all --------------------------------

def test_the_payload_is_never_granted_sudo():
    """
    Measured on the target: the payload unit sets NoNewPrivileges=true, so sudo
    refuses outright -- "the no new privileges flag is set" -- and a sudoers
    grant would be dead weight as well as unnecessary privilege. The mechanism
    is a request file acted on by root-owned systemd units.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    assert not os.path.exists(os.path.join(root, "setup", "raptorhab-wifi.sudoers"))
    install = open(os.path.join(root, "setup", "install.sh")).read()
    assert "sudoers.d/raptorhab-wifi" not in install

    # Inspect the code, not the commentary: the function's docstring explains
    # at length why sudo cannot work here, so a plain substring search matches
    # the explanation rather than any call.
    import ast

    tree = ast.parse(open(os.path.join(root, "airborne", "power.py")).read())
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_request_wifi_off"
    )
    body = list(target.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)):
        body = body[1:]                       # drop the docstring

    literals = [
        node.value for node in ast.walk(ast.Module(body=body, type_ignores=[]))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any("sudo" in text for text in literals), \
        "the unprivileged path must not shell out to sudo"


def test_helper_and_units_ship_together():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    setup = os.path.join(root, "setup")

    helper = os.path.join(setup, "wifi-power.sh")
    assert os.path.exists(helper)
    assert os.access(helper, os.X_OK), "helper must be executable"

    watcher = open(os.path.join(setup, "raptorhab-wifi-off.path")).read()
    assert "PathExists=/var/lib/raptorhab/wifi-off.request" in watcher

    actor = open(os.path.join(setup, "raptorhab-wifi-off.service")).read()
    assert "/usr/local/sbin/raptorhab-wifi-power off" in actor
    # The request has to be consumed or the watcher never re-arms.
    assert "rm -f /var/lib/raptorhab/wifi-off.request" in actor


def test_installer_places_everything():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    install = open(os.path.join(root, "setup", "install.sh")).read()

    assert "raptorhab-wifi-power" in install
    assert "raptorhab-wifi-off.path" in install
    assert "raptorhab-wifi-restore.service" in install
    # A request file left from a previous flight would fire on the bench.
    assert "rm -f \"$STATE_DIR/wifi-off.request\"" in install


def test_boot_restore_does_not_depend_on_the_payload():
    """
    systemd-rfkill restores the in-flight block on the next boot. The payload
    reports the radio state at startup, but the restore itself must not depend
    on the payload running -- "the payload is broken" is exactly when reaching
    the Pi matters most.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    unit = open(os.path.join(root, "setup", "raptorhab-wifi-restore.service")).read()

    assert "/usr/local/sbin/raptorhab-wifi-power on" in unit
    assert "raptorhab-airborne" not in unit, "must not depend on the payload"
    assert "WantedBy=sysinit.target" in unit


def test_request_file_is_written_when_unprivileged(tmp_path, monkeypatch):
    """The payload's whole action is to ask; it must actually write the ask."""
    from airborne import power

    request = tmp_path / "wifi-off.request"
    monkeypatch.setattr(power, "WIFI_OFF_REQUEST", str(request))
    monkeypatch.setattr(power.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(power, "wifi_blocked", lambda: True)

    action = power.disable_wifi()

    assert action.applied
    assert request.exists()


def test_an_unheard_request_is_reported_as_failure(tmp_path, monkeypatch):
    """
    A request nobody is listening to looks exactly like success unless the
    result is checked. It is checked.
    """
    from airborne import power

    request = tmp_path / "wifi-off.request"
    monkeypatch.setattr(power, "WIFI_OFF_REQUEST", str(request))
    monkeypatch.setattr(power.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(power, "wifi_blocked", lambda: False)

    action = power._request_wifi_off(timeout_sec=0.5)

    assert action[0] is False
    assert "still up" in action[1]
