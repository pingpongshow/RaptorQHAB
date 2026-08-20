"""The bench flight simulator must speak NMEA the real parser accepts.

The whole value of flightsim is that nothing is stubbed: if its sentences do
not parse in common.gps byte for byte, it is testing a pipeline that does not
exist. So these tests feed the generator's output through the real GPSReader
parser and assert the fix comes out the other side.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.gps import GPSReader, FixType

_spec = importlib.util.spec_from_file_location(
    "flightsim",
    os.path.join(os.path.dirname(__file__), "..", "tools", "flightsim.py"),
)
flightsim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flightsim)


def feed(reader: GPSReader, raw: bytes):
    for byte in raw:
        reader._parse_nmea_byte(chr(byte))


def fresh_reader():
    return GPSReader(simulate=False)


def test_checksums_are_correct():
    for line in flightsim.nmea_cycle(40.0, -100.0, 1234.5, 12.0, 90.0).split(b"\r\n"):
        if not line:
            continue
        body, cs = line[1:].rsplit(b"*", 1)
        assert flightsim.checksum(body.decode()) == cs.decode()


def test_a_3d_fix_round_trips_through_the_real_parser():
    r = fresh_reader()
    feed(r, flightsim.nmea_cycle(40.0, -100.0, 1234.5, 12.0, 90.0,
                                 fix_quality=1, gsa_mode=3))
    d = r.get_data()
    assert d.fix_type == FixType.FIX_3D
    assert abs(d.latitude - 40.0) < 1e-4
    assert abs(d.longitude - (-100.0)) < 1e-4
    assert abs(d.altitude - 1234.5) < 0.1
    assert d.position_valid


def test_southern_western_hemispheres_parse():
    r = fresh_reader()
    feed(r, flightsim.nmea_cycle(-33.8688, 151.2093, 50.0, 0.0, 0.0))
    d = r.get_data()
    assert abs(d.latitude - (-33.8688)) < 1e-4
    assert abs(d.longitude - 151.2093) < 1e-4


def test_a_2d_fix_is_reported_as_2d_not_3d():
    """The distinction the payload uses to refuse a launch point."""
    r = fresh_reader()
    feed(r, flightsim.nmea_cycle(40.0, -100.0, 0.0, 0.0, 0.0,
                                 fix_quality=1, gsa_mode=2))
    assert r.get_data().fix_type == FixType.FIX_2D


def test_gps_loss_reports_no_fix():
    r = fresh_reader()
    feed(r, flightsim.nmea_cycle(40.0, -100.0, 4000.0, 12.0, 90.0,
                                 fix_quality=1, gsa_mode=3))
    assert r.get_data().fix_type == FixType.FIX_3D
    feed(r, flightsim.nmea_cycle(40.0, -100.0, 4000.0, 12.0, 90.0,
                                 fix_quality=0, gsa_mode=1))
    d = r.get_data()
    assert d.fix_type == FixType.NONE
    assert not d.position_valid


def test_flight_profile_reaches_every_phase():
    p = flightsim.profile_flight
    assert p(0)[0] == 0.0                       # pad
    assert p(200)[0] == 4000.0                  # cruise
    assert 0 < p(300)[0] < 4000.0               # descending
    alt, speed, q, gsa = p(500)
    assert alt < 10 and speed == 0.0            # landed and still


def test_pad_loss_profile_stays_on_the_ground():
    """Loss on the pad: the scenario for watching fix_type over the air.
    Altitude must never rise -- a climb would flip zones, quiet the
    telemetry, and fire launch detection, all of which bury the signal
    this scenario exists to expose."""
    p = flightsim.profile_pad_loss
    assert all(p(t)[0] == 0.0 for t in (0, 45, 120))
    assert p(10)[2] == 1 and p(45)[2] == 0 and p(120)[2] == 1
