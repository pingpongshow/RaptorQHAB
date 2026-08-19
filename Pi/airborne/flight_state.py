"""
Flight state that has to survive a restart.

The payload restarts in flight. That is by design -- systemd restarts it, the
watchdog restarts it, and both are better than a payload that has stopped. But
a restart used to throw away everything the flight had learned, and two of
those things cannot be relearned in the air:

  - **The launch point.** It is captured from the first 3D fix. Restart at 20 km
    and the payload captures a "launch point" 20 km up, so every AGL figure for
    the rest of the flight is measured from the wrong datum. The balloon then
    reads 0 m AGL at altitude: it never crosses the WiFi cutoff threshold, never
    reaches the altitude override, and its zone logic is working from a number
    that is simply false.

  - **Landing detection arming.** It arms once the balloon has been above
    2000 m AGL, so a payload on the pad never mistakes itself for a landed one.
    Restart during descent and it disarms, cannot re-arm -- the balloon is on
    its way down and will not see 2000 m AGL again -- and never declares itself
    landed. It keeps taking pictures in a field instead of becoming the
    recovery beacon, which is the one job that matters at that point.

Deciding whether a restart happened in flight is the interesting part, and the
answer is altitude. If the payload comes up higher than the launch site it
recorded, it is still flying; nothing else explains it. A payload restarting on
the pad is at launch altitude, and one restarting after recovery is on the
ground somewhere -- neither is hundreds of metres above where it took off from.

Horizontal distance deliberately is not used. A payload recovered 50 km
downrange is far from its launch point and firmly on the ground, so distance
would give exactly the wrong answer in the case that matters most.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Optional

logger = logging.getLogger(__name__)

STATE_VERSION = 1

# How long a saved state stays usable. A flight is hours; this is generous
# enough to cover a long one plus the recovery drive, and short enough that
# next weekend's flight from a different field starts clean.
DEFAULT_MAX_AGE_SEC = 24 * 3600.0

# How far above the recorded launch altitude counts as "still flying". Well
# clear of the tens of metres a GPS solution wanders while stationary, and far
# below anything a real flight reaches.
DEFAULT_INFLIGHT_MARGIN_M = 150.0


@dataclass
class FlightState:
    """What a restart needs to know about the flight already in progress."""

    launch_latitude: float
    launch_longitude: float
    launch_altitude_m: Optional[float]
    launch_settled: bool = False
    peak_altitude_agl_m: float = 0.0
    landing_armed: bool = False
    saved_at: float = 0.0
    version: int = STATE_VERSION

    def age_sec(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return now - self.saved_at


def load(path: str, max_age_sec: float = DEFAULT_MAX_AGE_SEC,
         now: Optional[float] = None) -> Optional[FlightState]:
    """
    Read saved flight state, or None if there is nothing usable.

    Never raises. A corrupt or unreadable state file means the payload starts
    fresh, which is the behaviour it had before any of this existed -- a
    degraded launch reference is a far better outcome than a payload that will
    not boot.
    """
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning(f"Ignoring unreadable flight state at {path}: {e}")
        return None

    try:
        if raw.get("version") != STATE_VERSION:
            logger.info(
                f"Ignoring flight state written by version "
                f"{raw.get('version')!r}; this build expects {STATE_VERSION}"
            )
            return None
        state = FlightState(
            launch_latitude=float(raw["launch_latitude"]),
            launch_longitude=float(raw["launch_longitude"]),
            launch_altitude_m=(
                None if raw.get("launch_altitude_m") is None
                else float(raw["launch_altitude_m"])
            ),
            launch_settled=bool(raw.get("launch_settled", False)),
            peak_altitude_agl_m=float(raw.get("peak_altitude_agl_m", 0.0)),
            landing_armed=bool(raw.get("landing_armed", False)),
            saved_at=float(raw.get("saved_at", 0.0)),
        )
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"Ignoring malformed flight state at {path}: {e}")
        return None

    age = state.age_sec(now)
    if age > max_age_sec or age < -max_age_sec:
        # Negative ages happen: the payload has no RTC, so a state file written
        # after NTP synced looks like the future to a payload that has just
        # booted and not synced yet. Either way it is not this flight.
        logger.info(
            f"Ignoring flight state {age / 3600:.1f} h old (limit "
            f"{max_age_sec / 3600:.0f} h); treating this as a new flight"
        )
        return None

    return state


def save(path: str, state: FlightState, now: Optional[float] = None) -> bool:
    """
    Write flight state so a restart can pick it up. Never raises.

    Atomic, because the balloon loses power without warning and a half-written
    state file read on the next boot is worse than none at all -- it would be
    rejected as malformed, which is safe, but only by luck.
    """
    state.saved_at = time.time() if now is None else now
    state.version = STATE_VERSION

    tmp = f"{path}.part"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(asdict(state), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError as e:
        logger.warning(f"Could not save flight state to {path}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def clear(path: str) -> None:
    """Forget the current flight. Used by the installer and by tests."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Could not clear flight state at {path}: {e}")


def indicates_in_flight(
    state: Optional[FlightState],
    current_altitude_m: Optional[float],
    margin_m: float = DEFAULT_INFLIGHT_MARGIN_M,
) -> bool:
    """
    Whether this restart happened in the air rather than on the ground.

    Altitude above the recorded launch site is the whole test. A payload that
    comes up hundreds of metres higher than where it took off from is still
    flying; nothing else explains it.
    """
    if state is None or current_altitude_m is None:
        return False
    if state.launch_altitude_m is None:
        return False
    return current_altitude_m > state.launch_altitude_m + margin_m
