"""
Turning off what the balloon is not using.

A Pi Zero 2 W draws roughly 120-150 mA doing nothing in particular, and a
surprising share of that goes to subsystems a payload never uses. Nothing here
makes the payload faster or better; it makes the battery last longer, which on
a flight is the same thing as making the mission longer.

Measured figures for a Zero 2 W, from the Raspberry Pi documentation and
widely reproduced elsewhere. Treat them as the right order of magnitude rather
than a specification:

    WiFi associated and idle     40-70 mA
    Bluetooth idle                5-15 mA
    HDMI output                     ~25 mA
    Activity LED                    ~3 mA

That is potentially 100 mA off a 150 mA baseline. On a 3000 mAh pack it is the
difference between a flight that beacons for eight hours after landing and one
that goes quiet in four.

Why this is off by default
--------------------------
Disabling WiFi takes away SSH, which is how most people reach the payload.
Doing that automatically to someone's bench Pi would be hostile, and doing it
by surprise mid-flight worse still. So it is an explicit choice made before
launch, and the installer's --check says plainly when it is off.

The USB console keeps working throughout: it is a separate gadget on a
different bus and nothing here touches it. A payload with WiFi disabled is
still fully reachable over the cable.
"""

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PowerAction:
    what: str
    applied: bool
    detail: str = ""
    estimated_ma: int = 0


@dataclass
class PowerReport:
    actions: List[PowerAction] = field(default_factory=list)

    @property
    def estimated_saving_ma(self) -> int:
        return sum(a.estimated_ma for a in self.actions if a.applied)

    def as_dict(self) -> dict:
        return {
            "estimated_saving_ma": self.estimated_saving_ma,
            "actions": [
                {"what": a.what, "applied": a.applied,
                 "detail": a.detail, "estimated_ma": a.estimated_ma}
                for a in self.actions
            ],
        }


def _run(command: List[str], timeout: float = 10.0) -> tuple:
    """Run a command, returning (ok, output). Never raises."""
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout)
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except FileNotFoundError:
        return False, f"{command[0]} not found"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return False, str(exc)


WIFI_HELPER = "/usr/local/sbin/raptorhab-wifi-power"
WIFI_OFF_REQUEST = "/var/lib/raptorhab/wifi-off.request"
RFKILL_SYSFS = "/sys/class/rfkill"


def wifi_blocked() -> Optional[bool]:
    """
    Whether the WiFi radio is soft-blocked, or None if it cannot be read.

    /sys/class/rfkill is world-readable, unlike /dev/rfkill, so the payload can
    check the result of an action it is not privileged to perform itself.
    """
    try:
        for entry in sorted(os.listdir(RFKILL_SYSFS)):
            base = os.path.join(RFKILL_SYSFS, entry)
            with open(os.path.join(base, "type")) as f:
                if f.read().strip() != "wlan":
                    continue
            with open(os.path.join(base, "soft")) as f:
                return f.read().strip() == "1"
    except OSError:
        return None
    return None


def _request_wifi_off(timeout_sec: float = 10.0) -> tuple:
    """
    Ask systemd to turn WiFi off, and wait to see that it did.

    The payload cannot do this itself and deliberately is not given the power
    to. Measured on the target, every escalation route is closed:

        rfkill block wifi     -> cannot open /dev/rfkill: Permission denied
        nmcli radio wifi off  -> Not authorized to perform this operation
        sudo -n <helper> off  -> the "no new privileges" flag is set

    That last one is the interesting failure: the unit sets
    NoNewPrivileges=true, so a sudoers rule cannot help either. Weakening the
    payload's hardening to let it escalate would be a poor trade for a power
    saving. Instead it writes a file in its own state directory, and a systemd
    path unit -- already root -- does the privileged part. The payload gains no
    ability to run anything as root, only to ask for one specific action.

    Waits for confirmation from sysfs rather than assuming, because a request
    nobody is listening to looks exactly like success.
    """
    try:
        os.makedirs(os.path.dirname(WIFI_OFF_REQUEST), exist_ok=True)
        with open(WIFI_OFF_REQUEST, "w") as f:
            f.write("off\n")
    except OSError as e:
        return False, f"could not write {WIFI_OFF_REQUEST}: {e}"

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if wifi_blocked():
            return True, "blocked via systemd path unit"
        time.sleep(0.25)

    state = wifi_blocked()
    if state is None:
        return False, (
            "request written but rfkill state is unreadable; cannot confirm"
        )
    return False, (
        f"request written to {WIFI_OFF_REQUEST} but WiFi is still up after "
        f"{timeout_sec:.0f}s -- is raptorhab-wifi-off.path enabled?"
    )


def disable_wifi() -> PowerAction:
    """
    Take the WiFi radio down.

    rfkill rather than bringing the interface down, because an interface that
    is merely down still has a powered radio.

    The helper also clears the state systemd-rfkill saves under
    /var/lib/systemd/rfkill, so this really is undone by a power cycle. It was
    not before: that state persists by design, and a payload blocked in flight
    would have come back from recovery still blocked and unreachable.
    """
    if os.geteuid() == 0:
        ok, detail = _run([WIFI_HELPER, "off"])
        if not ok:
            ok, detail = _run(["rfkill", "block", "wifi"])
        return PowerAction("wifi", ok, detail or "blocked", 55 if ok else 0)

    ok, detail = _request_wifi_off()
    return PowerAction("wifi", ok, detail, 55 if ok else 0)


def restore_wifi() -> PowerAction:
    """
    Confirm WiFi is up, which at startup it should already be.

    Restoring is the job of raptorhab-wifi-restore.service, which runs as root
    at every boot and does not depend on the payload starting at all -- "the
    payload is broken" being exactly when reaching the Pi matters most. This
    only reports, so a payload that finds the radio still blocked says so
    instead of failing silently.
    """
    if os.geteuid() == 0:
        ok, detail = _run([WIFI_HELPER, "on"])
        return PowerAction("wifi", ok, detail or "unblocked", 0)

    state = wifi_blocked()
    if state is None:
        return PowerAction("wifi", False, "rfkill state unreadable", 0)
    if state:
        return PowerAction(
            "wifi", False,
            "WiFi is still blocked at startup; is "
            "raptorhab-wifi-restore.service enabled?", 0,
        )
    return PowerAction("wifi", True, "already up", 0)


def disable_bluetooth() -> PowerAction:
    ok, detail = _run(["rfkill", "block", "bluetooth"])
    return PowerAction("bluetooth", ok, detail or "rfkill blocked", 10 if ok else 0)


def disable_hdmi() -> PowerAction:
    """
    Turn off the display pipeline, if this system still has one to turn off.

    This is the one saving that often is not available, and saying so is more
    useful than pretending otherwise:

    - `tvservice -o` worked on the legacy firmware stack and was removed on
      Bookworm.
    - `vcgencmd display_power 0` is a firmware call that the KMS driver does
      not honour. On a Zero 2 W running Bookworm it returns success and leaves
      the state at 1, which is exactly the kind of false confirmation worth
      catching -- so the result is read back rather than trusted.
    - The DRM `enabled` files are read-only status, not switches.

    The saving is usually already realised: on Pi OS Lite with no monitor
    attached and no display server running, the HDMI PHY is not driving
    anything. The 25 mA figure belongs to a desktop image with an attached
    display, which a payload is not.
    """
    ok, _ = _run(["tvservice", "-o"])
    if ok:
        return PowerAction("hdmi", True, "tvservice off", 25)

    ok, _ = _run(["vcgencmd", "display_power", "0"])
    if ok:
        # Read it back. The call succeeds on KMS systems without doing
        # anything, and a saving that did not happen must not be counted.
        ok_state, state = _run(["vcgencmd", "display_power"])
        if ok_state and state.strip().endswith("=0"):
            return PowerAction("hdmi", True, "vcgencmd display_power 0", 25)
        return PowerAction(
            "hdmi", False,
            "vcgencmd accepted the call but the state stayed on; this is a "
            "KMS system, where HDMI is already idle with nothing attached")

    return PowerAction(
        "hdmi", False,
        "no way to switch HDMI on this system; on Pi OS Lite with no monitor "
        "attached it is already drawing nothing")


def disable_activity_led() -> PowerAction:
    """The green LED is useful on a bench and invisible at 30 km."""
    for path in ("/sys/class/leds/ACT/brightness",
                 "/sys/class/leds/led0/brightness"):
        if os.path.exists(path):
            try:
                with open(path, "w") as handle:
                    handle.write("0")
                return PowerAction("activity_led", True, path, 3)
            except OSError as exc:
                return PowerAction("activity_led", False, str(exc))
    return PowerAction("activity_led", False, "no LED control found")


def apply_flight_power_saving(disable_wifi_radio: bool = True,
                              disable_bt: bool = True,
                              disable_video: bool = True,
                              disable_led: bool = True) -> PowerReport:
    """
    Switch off what a flying payload does not use.

    Every step is individually optional and individually failure-tolerant: a
    payload must not refuse to fly because it could not turn off an LED.
    """
    report = PowerReport()

    if disable_wifi_radio:
        report.actions.append(disable_wifi())
    if disable_bt:
        report.actions.append(disable_bluetooth())
    if disable_video:
        report.actions.append(disable_hdmi())
    if disable_led:
        report.actions.append(disable_activity_led())

    for action in report.actions:
        if action.applied:
            logger.info(f"Power saving: {action.what} off ({action.detail})")
        else:
            logger.warning(
                f"Power saving: could not disable {action.what}: {action.detail}")

    logger.info(
        f"Power saving applied; roughly {report.estimated_saving_ma} mA less "
        f"draw. The USB console is unaffected.")
    return report


@dataclass
class CutoffState:
    """What the WiFi cutoff is doing, for telemetry and the console."""

    enabled: bool
    armed: bool
    fired: bool
    altitude_agl_m: Optional[float]
    threshold_m: float
    confirmations: int
    confirmations_needed: int
    detail: str = ""


class LaunchWiFiCutoff:
    """
    Turn WiFi off once the balloon has demonstrably launched.

    WiFi is the single largest controllable draw on the payload, and in flight
    it is worse than useless: there is no access point at altitude, so
    NetworkManager scans, fails, and scans again for the whole flight. But it
    cannot simply be disabled at boot, because the pre-launch checklist is run
    over it -- that is how the operator confirms the fix, the link and the key.

    So the radio stays up until the balloon proves it has left, and then goes
    down for good.

    "Proves" is doing real work in that sentence. Three conditions, all
    required:

      - A 3D fix. A 2D fix reports an altitude it did not solve for, which is
        exactly the wrong thing to trigger an irreversible action on. This is
        only trustworthy because the GPS layer now distinguishes the two; it
        used to call every fix 3D.
      - Height above the launch reference beyond the threshold. Also only
        trustworthy because the launch reference is now settled rather than
        taken from the receiver's first and worst fix.
      - The height sustained across several consecutive updates. One glitched
        fix should not end the flight's connectivity.

    The latch lives in the process, not on the card. A power cycle brings WiFi
    back, which is the documented way back into a recovered payload.
    """

    def __init__(
        self,
        altitude_agl_m: float = 300.0,
        confirmations_needed: int = 3,
        enabled: bool = True,
        action: Optional[Callable[[], PowerAction]] = None,
    ):
        """
        Args:
            altitude_agl_m: Height above the launch site that counts as flown.
            confirmations_needed: Consecutive updates above it before acting.
            enabled: Master switch.
            action: What to call when it fires. Injectable for testing, so the
                tests never touch the real radio.
        """
        self.altitude_agl_m = altitude_agl_m
        self.confirmations_needed = max(1, confirmations_needed)
        self.enabled = enabled
        self._action = action or disable_wifi

        self._fired = False
        self._confirmations = 0
        self._detail = "waiting for launch"

    @property
    def fired(self) -> bool:
        return self._fired

    def update(
        self,
        altitude_agl_m: Optional[float],
        fix_type: int,
    ) -> Optional[PowerAction]:
        """
        Feed one position update. Returns the action taken, or None.

        Called from the main loop after the zone manager has run, so the AGL
        figure is the settled one.
        """
        if not self.enabled or self._fired:
            return None

        if fix_type < 2:
            # Not a 3D fix. Do not count it, and do not hold the count either:
            # a gap in coverage is not evidence against having launched.
            self._detail = "no 3D fix"
            return None

        if altitude_agl_m is None or altitude_agl_m < self.altitude_agl_m:
            if self._confirmations:
                self._detail = "dropped back below the threshold"
            self._confirmations = 0
            return None

        self._confirmations += 1
        if self._confirmations < self.confirmations_needed:
            self._detail = (
                f"above {self.altitude_agl_m:.0f} m AGL "
                f"({self._confirmations}/{self.confirmations_needed})"
            )
            return None

        self._fired = True
        action = self._action()
        self._detail = (
            f"launched at {altitude_agl_m:.0f} m AGL; {action.detail}"
            if action.applied
            else f"launched at {altitude_agl_m:.0f} m AGL but WiFi stayed up: {action.detail}"
        )

        if action.applied:
            logger.info(
                f"Launch detected at {altitude_agl_m:.0f} m AGL: WiFi off for "
                f"the rest of the flight ({action.detail}). Power-cycle the "
                f"payload to get it back."
            )
        else:
            # Loud. The operator planned for this saving and is not getting it,
            # and the usual cause is the sudoers rule not being installed.
            logger.error(
                f"Launch detected at {altitude_agl_m:.0f} m AGL but WiFi could "
                f"not be turned off: {action.detail}. Check that "
                f"{WIFI_HELPER} and /etc/sudoers.d/raptorhab-wifi are installed."
            )
        return action

    def state(self, altitude_agl_m: Optional[float] = None) -> CutoffState:
        return CutoffState(
            enabled=self.enabled,
            armed=self.enabled and not self._fired,
            fired=self._fired,
            altitude_agl_m=altitude_agl_m,
            threshold_m=self.altitude_agl_m,
            confirmations=self._confirmations,
            confirmations_needed=self.confirmations_needed,
            detail=self._detail,
        )

    def get_status(self) -> dict:
        state = self.state()
        return {
            "enabled": state.enabled,
            "armed": state.armed,
            "fired": state.fired,
            "threshold_agl_m": state.threshold_m,
            "confirmations": state.confirmations,
            "confirmations_needed": state.confirmations_needed,
            "detail": state.detail,
        }
