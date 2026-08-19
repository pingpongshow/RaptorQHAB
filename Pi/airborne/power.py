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
from dataclasses import dataclass, field
from typing import List

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


def disable_wifi() -> PowerAction:
    """
    Take the WiFi radio down.

    rfkill is used rather than bringing the interface down, because an
    interface that is merely down still has a powered radio. This is also
    reversible with a reboot, which matters when the alternative is a payload
    you cannot get back into.
    """
    ok, detail = _run(["rfkill", "block", "wifi"])
    if not ok:
        # NetworkManager will fight rfkill on some images; ask it too.
        ok_nm, detail_nm = _run(["nmcli", "radio", "wifi", "off"])
        if ok_nm:
            return PowerAction("wifi", True, "disabled via nmcli", 55)
        return PowerAction("wifi", False, f"{detail}; {detail_nm}")
    return PowerAction("wifi", True, "rfkill blocked", 55)


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
