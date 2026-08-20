#!/usr/bin/env python3
"""Force the payload through flight scenarios on the bench.

Feeds synthetic NMEA into the payload through a pseudo-terminal, so the whole
real pipeline runs: the NMEA parser, the GSA/GGA fix-type logic, the zone
manager, launch detection, flight-state persistence, and everything keyed off
them (repeater and mesh-log eligibility, the WiFi cutoff, zone airtime
budgets). Nothing is stubbed -- the payload cannot tell this from a real
receiver, which is the point.

Run ON the payload:

    python3 flightsim.py run flight            # pad -> ascent -> cruise ->
                                               #   descent -> landed
    python3 flightsim.py run cruise            # straight to altitude
    python3 flightsim.py run gps-loss          # cruise, lose the fix, recover
    python3 flightsim.py run 2d                # a fix the payload must refuse
    python3 flightsim.py restore               # put everything back

`run` rewrites gps_device in the live config to point at the simulator's pty
and restarts the service; `restore` points it back at the real receiver,
clears the flight state the simulation created, and restarts again.

Two things to know before running an ascent:

-  Launch detection is REAL here. The payload will record a launch in
   /var/lib/raptorhab/flight_state.json, and with wifi_off_after_launch set
   (the default) it will turn WiFi off when the simulated balloon climbs
   through the cutoff altitude. Use the USB cable; `restore` re-enables WiFi.
-  Always finish with `restore`. A payload left holding a simulated flight
   state will refuse to re-capture its launch point on the next real fix.
"""
import argparse
import json
import math
import os
import pty
import subprocess
import sys
import time

CONFIG_PATH = "/var/lib/raptorhab/config/airborne.json"
FLIGHT_STATE = "/var/lib/raptorhab/flight_state.json"
# Not /tmp: the service runs with PrivateTmp=true and cannot see the host's
# /tmp at all -- a link there fails silently, and the GPS reader falls back
# to the real receiver while every simulator-side sign says it is working.
PTY_LINK = "/var/lib/raptorhab/simgps"
SERVICE = "raptorhab-airborne"
REAL_GPS = "/dev/serial0"

# Anywhere on Earth works: the zone manager cares about movement relative to
# the launch point, not where the launch point is.
DEFAULT_LAT, DEFAULT_LON, DEFAULT_ALT = 40.0, -100.0, 200.0


def checksum(body: str) -> str:
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"{c:02X}"


def sentence(body: str) -> bytes:
    return f"${body}*{checksum(body)}\r\n".encode()


def dm(value: float, is_lat: bool) -> tuple:
    """Decimal degrees -> NMEA ddmm.mmmm plus hemisphere."""
    hemi = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    v = abs(value)
    deg = int(v)
    minutes = (v - deg) * 60
    width = 2 if is_lat else 3
    return f"{deg:0{width}d}{minutes:07.4f}", hemi


def nmea_cycle(lat, lon, alt, speed_ms, heading, fix_quality=1, gsa_mode=3, sats=11):
    """One second's worth of sentences, in the order a receiver emits them.

    GSA goes first: the parser accumulates its mode and the position sentence
    consumes it, which is how 2D and 3D are told apart.
    """
    t = time.gmtime()
    hms = f"{t.tm_hour:02d}{t.tm_min:02d}{t.tm_sec:02d}.00"
    date = f"{t.tm_mday:02d}{t.tm_mon:02d}{t.tm_year % 100:02d}"
    out = []

    svs = ",".join(str(5 + i) for i in range(min(sats, 12)))
    svs += "," * (12 - min(sats, 12))
    out.append(sentence(f"GNGSA,A,{gsa_mode},{svs},1.8,1.0,1.5"))

    if fix_quality > 0:
        lats, ns = dm(lat, True)
        lons, ew = dm(lon, False)
        out.append(sentence(
            f"GNGGA,{hms},{lats},{ns},{lons},{ew},{fix_quality},{sats:02d},"
            f"1.0,{alt:.1f},M,0.0,M,,"))
        knots = speed_ms * 1.9438
        out.append(sentence(
            f"GNRMC,{hms},A,{lats},{ns},{lons},{ew},{knots:.1f},"
            f"{heading:.1f},{date},,,A"))
    else:
        # No fix: empty position fields, void status. What a real receiver
        # sends under a roof.
        out.append(sentence(f"GNGGA,{hms},,,,,0,00,99.9,,M,,M,,"))
        out.append(sentence(f"GNRMC,{hms},V,,,,,,,{date},,,N"))
    return b"".join(out)


# ---------------------------------------------------------------------------
# Scenario profiles: (t_seconds) -> (alt_agl, speed_ms, fix_quality, gsa_mode)
# Compressed timelines; --speedup scales them further.
# ---------------------------------------------------------------------------

def profile_pad(t):
    return 0.0, 0.0, 1, 3

def profile_launch(t):
    if t < 30: return 0.0, 0.0, 1, 3
    return min((t - 30) * 40.0, 4000.0), 15.0, 1, 3      # brisk 40 m/s climb

def profile_cruise(t):
    return min(t * 200.0, 4000.0), 12.0, 1, 3            # get high, fast

def profile_descent(t):
    return max(4000.0 - t * 30.0, 10.0), 10.0, 1, 3

def profile_landed(t):
    # A landing detector needs a flight first: quick hop, then down and still.
    if t < 30: return min(t * 200.0, 4000.0), 12.0, 1, 3
    if t < 150: return max(4000.0 - (t - 30) * 40.0, 3.0), 8.0, 1, 3
    return 3.0, 0.0, 1, 3

def profile_gps_loss(t):
    if t < 40: return min(t * 200.0, 4000.0), 12.0, 1, 3
    if t < 100: return 4000.0, 12.0, 0, 1                # the roof: no fix
    return 4000.0, 12.0, 1, 3                            # and recovery

def profile_2d(t):
    # Quality says "fix", GSA says two-dimensional. The payload must hold its
    # zone and refuse to update the launch point from this.
    return 0.0, 0.0, 1, 2

SCENARIOS = {
    "pad": (profile_pad, "sitting on the pad with a 3D fix"),
    "launch": (profile_launch, "pad for 30 s, then a brisk ascent"),
    "cruise": (profile_cruise, "straight up to cruise altitude"),
    "descent": (profile_descent, "from altitude down to the ground"),
    "landed": (profile_landed, "a whole quick flight ending low and still"),
    "gps-loss": (profile_gps_loss, "climb, lose the fix entirely, recover"),
    "2d": (profile_2d, "a 2D fix the payload must refuse to act on"),
    "flight": (None, "pad, ascent, cruise dwell, descent, landed"),
}

def profile_flight(t):
    if t < 30: return profile_pad(t)
    if t < 130: return min((t - 30) * 40.0, 4000.0), 15.0, 1, 3
    if t < 250: return 4000.0, 12.0, 1, 3
    if t < 390: return max(4000.0 - (t - 250) * 30.0, 3.0), 10.0, 1, 3
    return 3.0, 0.0, 1, 3

SCENARIOS["flight"] = (profile_flight, SCENARIOS["flight"][1])


def sudo(*cmd):
    return subprocess.run(["sudo", *cmd], capture_output=True, text=True)


def set_gps_device(path: str):
    raw = sudo("cat", CONFIG_PATH)
    cfg = json.loads(raw.stdout) if raw.returncode == 0 and raw.stdout.strip() else {}
    cfg["gps_device"] = path
    p = subprocess.run(["sudo", "tee", CONFIG_PATH], input=json.dumps(cfg, indent=2, sort_keys=True),
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"could not write {CONFIG_PATH}: {p.stderr}")


def cmd_run(args):
    prof, desc = SCENARIOS[args.scenario]
    lat0, lon0, alt0 = args.lat, args.lon, args.ground_alt

    master, slave = pty.openpty()
    slave_path = os.ttyname(slave)
    if os.path.islink(PTY_LINK) or os.path.exists(PTY_LINK):
        os.unlink(PTY_LINK)
    os.symlink(slave_path, PTY_LINK)
    os.chmod(slave_path, 0o666)

    print(f"scenario : {args.scenario} -- {desc}")
    print(f"pty      : {slave_path} (linked at {PTY_LINK})")
    print(f"speedup  : x{args.speedup}")
    print()
    print("pointing the payload at the simulator and restarting it...")
    set_gps_device(PTY_LINK)
    sudo("systemctl", "restart", SERVICE)
    print("done. streaming -- watch with:")
    print(f"  journalctl -u {SERVICE} -f | grep -E 'zone|Launch|WiFi|Landing'")
    print("finish with:  python3 flightsim.py restore")
    print()

    t0 = time.monotonic()
    try:
        while True:
            t = (time.monotonic() - t0) * args.speedup
            alt_agl, speed, quality, gsa = prof(t)
            # Drift horizontally with the "wind" so distance grows too.
            drift = alt_agl / 4000.0 * 0.02          # up to ~2 km of drift
            lat = lat0 + drift * 0.5
            lon = lon0 + drift
            os.write(master, nmea_cycle(lat, lon, alt0 + alt_agl, speed,
                                        90.0, quality, gsa))
            print(f"\r  t={t:6.0f}s  alt={alt0 + alt_agl:7.1f} m "
                  f"(AGL {alt_agl:6.1f})  fix={'3D' if gsa == 3 else ('2D' if gsa == 2 else 'none') if quality else 'none'}   ",
                  end="", flush=True)
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopped. the payload is still reading the (now silent) pty.")
        print("run  python3 flightsim.py restore  to put the real GPS back.")


def cmd_restore(args):
    print("restoring the real GPS device...")
    set_gps_device(REAL_GPS)
    print("clearing the simulated flight state...")
    sudo("rm", "-f", FLIGHT_STATE)
    # The launch point the zone manager persisted alongside it, if any.
    sudo("systemctl", "restart", SERVICE)
    # WiFi may have been cut by a simulated ascent. The restore unit is a
    # boot-time unit, so call the power helper directly and fall back to a
    # plain rfkill unblock on installs that predate it.
    r = sudo("/usr/local/sbin/raptorhab-wifi-power", "on")
    if r.returncode != 0:
        sudo("rfkill", "unblock", "wifi")
    print("service restarted, WiFi unblocked")
    if os.path.islink(PTY_LINK):
        os.unlink(PTY_LINK)
    print("done. the payload is back on the real receiver with a clean state.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="stream a scenario into the payload")
    run.add_argument("scenario", choices=sorted(SCENARIOS))
    run.add_argument("--speedup", type=float, default=1.0,
                     help="compress the timeline by this factor")
    run.add_argument("--lat", type=float, default=DEFAULT_LAT)
    run.add_argument("--lon", type=float, default=DEFAULT_LON)
    run.add_argument("--ground-alt", type=float, default=DEFAULT_ALT)
    run.set_defaults(func=cmd_run)
    rst = sub.add_parser("restore", help="put the real GPS back and clean up")
    rst.set_defaults(func=cmd_restore)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
