"""
Regression tests for the balloon-side code review.

Each test names the defect it pins down. Several of these are failure modes
that only appear in flight -- a clock step, a partial fix, a power cut -- which
is exactly why they survived until someone went looking.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.constants import FixType, PacketType


# --------------------------------------------------------------------------
# GPS: a 2D fix was being reported as 3D
# --------------------------------------------------------------------------

def nmea(body: str) -> bytes:
    """Wrap an NMEA body in its '$' and correct checksum."""
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"${body}*{checksum:02X}\r\n".encode("ascii")


GGA_QUALITY_1 = "GNGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
GSA_3D = "GNGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1"
GSA_2D = "GNGSA,A,2,04,05,,09,12,,,24,,,,,2.5,1.3,2.1"
GSA_NONE = "GNGSA,A,1,,,,,,,,,,,,,99.9,99.9,99.9"


@pytest.fixture
def gps():
    from common.gps import GPS
    return GPS(simulate=False)


def test_gsa_2d_is_not_reported_as_3d(gps):
    """
    The defect: GGA field 6 is fix *quality*, not dimensionality. Reading
    `FIX_3D if quality >= 1` promoted every 2D fix to 3D, and everything
    downstream gates on `fix_type >= 2` -- including the region manager, which
    changes transmit frequency and power.
    """
    gps._process_data(nmea(GSA_2D))
    gps._process_data(nmea(GGA_QUALITY_1))
    assert gps.get_data().fix_type == FixType.FIX_2D
    assert gps.get_data().position_valid is True


def test_gsa_3d_is_reported_as_3d(gps):
    gps._process_data(nmea(GSA_3D))
    gps._process_data(nmea(GGA_QUALITY_1))
    assert gps.get_data().fix_type == FixType.FIX_3D


def test_gsa_downgrade_takes_effect_on_the_next_fix(gps):
    """A fix that degrades mid-flight must not stay 3D."""
    gps._process_data(nmea(GSA_3D))
    gps._process_data(nmea(GGA_QUALITY_1))
    assert gps.get_data().fix_type == FixType.FIX_3D

    gps._process_data(nmea(GSA_2D))
    gps._process_data(nmea(GGA_QUALITY_1))
    assert gps.get_data().fix_type == FixType.FIX_2D


def test_a_cycles_gsa_does_not_leak_into_the_next(gps):
    """
    Each fix is judged on its own cycle. If the accumulated best were not
    cleared, one good cycle would make every later fix look 3D forever.
    """
    gps._process_data(nmea(GSA_3D))
    gps._process_data(nmea(GGA_QUALITY_1))
    assert gps.get_data().fix_type == FixType.FIX_3D

    # Next cycle: only a 2D report from one constellation.
    gps._process_data(nmea(GSA_2D))
    gps._process_data(nmea(GGA_QUALITY_1))
    assert gps.get_data().fix_type == FixType.FIX_2D


def test_gsa_split_across_a_slow_serial_burst_still_groups(gps, monkeypatch):
    """
    At 9600 baud a full NMEA burst takes about half a second to arrive, so
    sentences from one cycle can be widely separated in time. Cycles are
    delimited by the position sentence for exactly this reason -- a time-based
    window narrow enough to separate cycles would split them.
    """
    from common import gps as gps_mod

    clock = [1000.0]
    monkeypatch.setattr(gps_mod.time, "monotonic", lambda: clock[0])

    gps._process_data(nmea(GSA_3D))
    clock[0] += 0.45                       # rest of the burst trickles in
    gps._process_data(nmea(GSA_NONE))
    clock[0] += 0.45
    gps._process_data(nmea(GGA_QUALITY_1))

    assert gps.get_data().fix_type == FixType.FIX_3D


def test_multi_constellation_gsa_takes_the_best_mode(gps):
    """
    A GNSS receiver sends one GSA per constellation. GLONASS reporting no fix
    in the same cycle GPS reports 3D must not drag the answer down.
    """
    gps._process_data(nmea(GSA_3D))
    gps._process_data(nmea(GSA_NONE))
    gps._process_data(nmea(GGA_QUALITY_1))
    assert gps.get_data().fix_type == FixType.FIX_3D


def test_no_gsa_falls_back_to_the_old_assumption(gps):
    """A receiver that emits no GSA must keep working, not lose its fix."""
    gps._process_data(nmea(GGA_QUALITY_1))
    assert gps.get_data().fix_type == FixType.FIX_3D


def test_stale_gsa_is_not_trusted(gps, monkeypatch):
    """A GSA from a minute ago says nothing about the current fix."""
    from common import gps as gps_mod

    gps._process_data(nmea(GSA_2D))
    real = time.monotonic
    monkeypatch.setattr(
        gps_mod.time, "monotonic", lambda: real() + gps_mod.GSA_STALE_SEC + 1
    )
    gps._process_data(nmea(GGA_QUALITY_1))
    assert gps.get_data().fix_type == FixType.FIX_3D


def test_no_gga_fix_is_none_regardless_of_gsa(gps):
    gps._process_data(nmea(GSA_3D))
    gps._process_data(
        nmea("GNGGA,123519,4807.038,N,01131.000,E,0,00,99.9,545.4,M,46.9,M,,")
    )
    assert gps.get_data().fix_type == FixType.NONE


# --------------------------------------------------------------------------
# Clocks: elapsed time must not come from the wall clock
# --------------------------------------------------------------------------

def test_watchdog_survives_a_wall_clock_step(monkeypatch):
    """
    The defect: the watchdog measured elapsed time with time.time(). The Pi has
    no RTC, so systemd-timesyncd steps the clock after boot -- by months on a
    card that has been on the shelf. That step read as a months-long gap since
    the last feed and rebooted a payload that was working perfectly.
    """
    from airborne.utils import Watchdog

    fired = []
    dog = Watchdog(timeout_sec=30, callback=lambda: fired.append(True))
    dog.feed()

    # Two months forward, the way timesyncd would do it.
    monkeypatch.setattr(time, "time", lambda: time.monotonic() + 60 * 86400)

    elapsed = time.monotonic() - dog.last_feed
    assert elapsed < 30, "watchdog measured a clock step as elapsed time"
    assert not fired


def test_watchdog_still_fires_on_a_real_hang():
    """The fix must not have disarmed the watchdog."""
    from airborne.utils import Watchdog

    fired = []
    dog = Watchdog(timeout_sec=30, callback=lambda: fired.append(True))
    dog.last_feed = time.monotonic() - 31
    assert time.monotonic() - dog.last_feed > dog.timeout_sec


def test_flight_modules_use_monotonic_for_elapsed_time():
    """
    Guards the whole class of defect rather than one instance: no module on the
    flight path may compute an interval from the wall clock.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for folder in ("airborne", "common"):
        directory = os.path.join(root, folder)
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(directory, name)
            with open(path) as handle:
                for number, line in enumerate(handle, 1):
                    stripped = line.split("#")[0]
                    # An interval is time.time() with arithmetic around it.
                    if re.search(r"time\.time\(\)\s*-", stripped) or re.search(
                        r"-\s*time\.time\(\)", stripped
                    ):
                        offenders.append(f"{folder}/{name}:{number}")
    assert not offenders, "wall clock used for elapsed time: " + ", ".join(offenders)


def test_repeater_uses_one_clock_throughout():
    """
    should_repeat() reads the spacing state that build_repeat() writes. If they
    disagreed about which clock they were on, _last_repeat would land about 1.7
    billion seconds in the future and the repeater would go silent for the rest
    of the flight.
    """
    import inspect

    from airborne.repeater import MeshtasticRepeater

    for method in (MeshtasticRepeater.should_repeat, MeshtasticRepeater.build_repeat):
        source = inspect.getsource(method)
        assert "time.monotonic() if now is None" in source, method.__name__
        assert "time.time() if now is None" not in source, method.__name__


# --------------------------------------------------------------------------
# Recordings: a power cut must not cost more than it has to
# --------------------------------------------------------------------------

def test_append_line_falls_back_to_plaintext_when_sealing_fails(tmp_path, monkeypatch):
    """
    The class promises "a sealing failure never loses data". write() honoured
    that; append_line() called seal() outside any try and raised into its
    caller, which is a GPS callback.
    """
    from common import sealedwriter

    writer = sealedwriter.SealedWriter(enabled=False)
    writer._key = b"\x01" * 32  # force the sealed path

    def explode(*args, **kwargs):
        raise ValueError("simulated crypto failure")

    monkeypatch.setattr(sealedwriter, "seal", explode)

    path = str(tmp_path / "telemetry.csv")
    result = writer.append_line(path, "1,2,3\n")   # must not raise

    assert result == path
    assert open(path).read() == "1,2,3\n"


def test_write_is_atomic(tmp_path, monkeypatch):
    """
    A truncated sealed file is not "most of an image", it is nothing: the
    authentication tag is at the end, so a partial box will not open.
    """
    from common.sealedwriter import SealedWriter

    writer = SealedWriter(enabled=False)
    path = str(tmp_path / "image.jpg")
    writer.write(path, b"\xff\xd8payload")

    assert open(path, "rb").read() == b"\xff\xd8payload"
    assert not os.path.exists(path + ".part"), "temp file left behind"


def test_write_leaves_no_partial_file_when_it_fails(tmp_path, monkeypatch):
    from common import sealedwriter

    writer = sealedwriter.SealedWriter(enabled=False)
    path = str(tmp_path / "image.jpg")

    original = os.replace
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        writer.write(path, b"data")
    monkeypatch.setattr(os, "replace", original)

    assert not os.path.exists(path), "a failed write left a file behind"


def test_appended_log_reaches_the_card(tmp_path):
    """Records written but never flushed are records a power cut discards."""
    from common.sealedwriter import SealedWriter

    writer = SealedWriter(enabled=False, sync_interval_sec=0.0)
    path = str(tmp_path / "log.csv")
    writer.append_line(path, "row\n")
    assert writer._last_sync > 0, "nothing was ever fsynced"


def test_sealed_telemetry_goes_to_one_file(tmp_path):
    """
    _write_header assigned the writer's return value back to self.filepath.
    With sealing on that return value already ends in ".rhs", so every later
    log() handed an ".rhs" path to a writer that appends ".rhs": the header
    landed in telemetry.csv.rhs, the actual flight data in
    telemetry.csv.rhs.rhs, an empty telemetry.csv sat beside them, and the
    path the payload reported was the one with no data in it.
    """
    from airborne.telemetry import TelemetryLogger
    from common import sealedbox
    from common.sealedwriter import SealedWriter

    private, public = sealedbox.generate_keypair()
    writer = SealedWriter(enabled=False)
    writer._key = public

    log = TelemetryLogger(str(tmp_path), callsign="TEST", sealed_writer=writer)
    for index in range(5):
        log.log(latitude=1.0 + index, longitude=2.0)

    files = sorted(os.listdir(tmp_path))
    assert files == [os.path.basename(log.filepath)], files
    assert not log.filepath.endswith(".rhs.rhs")

    # And the one file must hold the header and every row.
    raw = open(log.filepath, "rb").read()
    records, offset = [], 0
    while offset < len(raw):
        length = int.from_bytes(raw[offset:offset + 4], "big")
        offset += 4
        records.append(sealedbox.open_sealed(raw[offset:offset + length], private))
        offset += length

    assert len(records) == 6, "header plus five rows"
    assert records[0].startswith(b"timestamp,")
    assert b"5.0000000" in records[5]


def test_plaintext_telemetry_still_goes_to_one_file(tmp_path):
    from airborne.telemetry import TelemetryLogger

    log = TelemetryLogger(str(tmp_path), callsign="TEST")
    log.log(latitude=1.0, longitude=2.0)

    files = sorted(os.listdir(tmp_path))
    assert files == [os.path.basename(log.filepath)], files
    assert open(log.filepath).read().count("\n") == 2


def test_restart_does_not_append_to_the_previous_log(tmp_path):
    """
    A payload that reboots inside the same second -- which is what happens when
    the clock is unset -- generates the same filename again.
    """
    from airborne.telemetry import TelemetryLogger
    from common import sealedbox
    from common.sealedwriter import SealedWriter

    _, public = sealedbox.generate_keypair()
    writer = SealedWriter(enabled=False)
    writer._key = public

    first = TelemetryLogger(str(tmp_path), callsign="TEST", sealed_writer=writer)
    for _ in range(20):
        first.log(latitude=1.0, longitude=2.0)
    size_after_first = os.path.getsize(first.filepath)

    second = TelemetryLogger(str(tmp_path), callsign="TEST", sealed_writer=writer)
    if second.filepath != first.filepath:
        pytest.skip("clock ticked over; no collision to test")

    assert os.path.getsize(second.filepath) < size_after_first
    assert sorted(os.listdir(tmp_path)) == [os.path.basename(second.filepath)]


# --------------------------------------------------------------------------
# Uplink command replay
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "last, seq, duplicate, label",
    [
        (100, 101, False, "next in sequence"),
        (100, 100, True, "exact repeat"),
        (100, 99, True, "one behind"),
        (100, 200, False, "edge of the window"),
        (100, 500, True, "beyond the window"),
        (100, 65000, True, "far replay -- previously accepted"),
        (65530, 3, False, "genuine wraparound"),
        (65530, 65529, True, "behind, across the wrap"),
    ],
)
def test_command_replay_window(last, seq, duplicate, label):
    """
    The old test was `abs(seq - last) < window`, with anything outside treated
    as a wraparound and accepted. That accepted every sequence number more than
    `window` away, which is most of the 16-bit space.
    """
    from airborne.commands import CommandHandler

    handler = object.__new__(CommandHandler)
    handler._seq_window = 100
    handler._last_cmd_seq = {PacketType.CMD_PING: last}

    assert handler._is_duplicate(PacketType.CMD_PING, seq) is duplicate, label


# --------------------------------------------------------------------------
# Launch reference taken from the worst fix the receiver produces
# --------------------------------------------------------------------------

def zone_manager(**kwargs):
    from airborne.zone_manager import ZoneManager

    defaults = dict(launch_latitude=0.0, launch_longitude=0.0, radius_m=8000.0)
    defaults.update(kwargs)
    return ZoneManager(**defaults)


# Measured on the bench with a real L76K and antenna, stationary throughout.
# Altitude MSL as the satellite count climbed from 6 to 10, then holding once
# converged -- which is what the receiver does for as long as it is left alone.
BENCH_ALTITUDES = [
    202.0, 201.4, 200.5, 199.5, 198.4, 195.2, 191.0, 186.3,
    182.1, 179.0, 176.4, 174.8, 173.9, 173.5, 172.9,
] + [173.0, 172.8, 173.1, 172.9, 173.2, 172.7, 173.0, 172.9]


def test_launch_altitude_is_not_taken_from_the_first_fix():
    """
    The defect: _capture_launch_point fired on the very first 3D fix, which is
    the least accurate one a receiver produces. Every AGL figure is measured
    against it, so the convergence error is baked into the whole flight.

    Replays real bench data: 202 m at 6 satellites settling to 173 m at 10,
    without the payload moving an inch.
    """
    manager = zone_manager(launch_settle_sec=180.0)

    for index, altitude in enumerate(BENCH_ALTITUDES):
        manager.update(
            latitude=51.5011077,
            longitude=-0.1087385,
            altitude_m=altitude,
            fix_type=2,
            now=1000.0 + index * 10.0,
        )

    assert manager.launch_altitude_m is not None
    error = abs(manager.launch_altitude_m - BENCH_ALTITUDES[0])
    assert error > 10.0, "still pinned to the first fix"
    assert min(BENCH_ALTITUDES) <= manager.launch_altitude_m <= max(BENCH_ALTITUDES)


def test_settled_reference_beats_the_first_fix_on_real_data():
    """The refinement has to actually be closer to the truth, not just different."""
    settled_truth = BENCH_ALTITUDES[-1]

    manager = zone_manager(launch_settle_sec=180.0)
    for index, altitude in enumerate(BENCH_ALTITUDES):
        manager.update(
            latitude=51.5011077, longitude=-0.1087385, altitude_m=altitude,
            fix_type=2, now=1000.0 + index * 10.0,
        )

    first_fix_error = abs(BENCH_ALTITUDES[0] - settled_truth)
    settled_error = abs(manager.launch_altitude_m - settled_truth)
    assert settled_error < first_fix_error


def test_refinement_stops_once_the_payload_moves():
    """
    Movement means it launched. Refining then would drag the launch reference
    along with the balloon, which is far worse than a slightly wrong one.
    """
    manager = zone_manager(launch_settle_sec=600.0, launch_settle_max_drift_m=50.0)

    manager.update(latitude=51.50110, longitude=-0.10873, altitude_m=200.0,
                   fix_type=2, now=1000.0)
    captured_lat = manager.launch_latitude

    # Half a kilometre downrange, climbing.
    manager.update(latitude=51.50560, longitude=-0.10873, altitude_m=800.0,
                   fix_type=2, now=1030.0)
    manager.update(latitude=51.51000, longitude=-0.10873, altitude_m=1600.0,
                   fix_type=2, now=1060.0)

    assert manager.launch_latitude == captured_lat
    assert manager._launch_settled


def test_configured_launch_point_is_never_refined():
    """An operator who typed in coordinates meant them."""
    manager = zone_manager(launch_latitude=51.5, launch_longitude=-0.1,
                           launch_altitude_m=25.0, launch_settle_sec=90.0)

    for index in range(15):
        manager.update(latitude=51.5, longitude=-0.1, altitude_m=200.0 + index,
                       fix_type=2, now=1000.0 + index * 10.0)

    assert manager.launch_latitude == 51.5
    assert manager.launch_altitude_m == 25.0


def test_settling_can_be_disabled():
    """Zero restores the previous behaviour exactly."""
    manager = zone_manager(launch_settle_sec=0.0)

    for index, altitude in enumerate(BENCH_ALTITUDES):
        manager.update(latitude=51.5011077, longitude=-0.1087385,
                       altitude_m=altitude, fix_type=2, now=1000.0 + index * 10.0)

    assert manager.launch_altitude_m == BENCH_ALTITUDES[0]


def test_zone_logic_works_during_the_settling_window():
    """The provisional point is used immediately; nothing waits on settling."""
    manager = zone_manager(launch_settle_sec=90.0)
    state = manager.update(latitude=51.50110, longitude=-0.10873,
                           altitude_m=200.0, fix_type=2, now=1000.0)

    assert state.zone.value == "launch"
    assert state.distance_from_launch_m == pytest.approx(0.0, abs=1.0)


def test_a_two_d_fix_does_not_capture_the_launch_point():
    """Ties the two fixes together: D1 is what keeps a 2D altitude out of here."""
    from common.constants import FixType

    manager = zone_manager(launch_settle_sec=90.0)
    manager.update(latitude=51.50110, longitude=-0.10873, altitude_m=202.0,
                   fix_type=int(FixType.FIX_2D), now=1000.0)

    assert not manager._launch_point_captured
    assert manager.launch_altitude_m is None
