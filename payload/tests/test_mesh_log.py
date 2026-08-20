"""
Logging what the balloon hears on the mesh, in cruise.

The interesting part is not the writing, it is the restraint: only in cruise,
sealed because it holds other people's positions, and never able to disturb the
listen window it runs inside.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from airborne.mesh_log import COLUMNS, MeshtasticLog
from airborne.repeater import HeardPacket
from common.meshtastic.messages import PortNum


def packet(sender=0x11223344, packet_id=1000, port=PortNum.POSITION_APP,
           payload=b"\x08\x01", rssi=-97, snr=-3.5):
    return HeardPacket(
        sender=sender, destination=0xFFFFFFFF, packet_id=packet_id,
        port=port, payload=payload, channel_hash=8, rssi=rssi, snr=snr, raw=b"",
    )


def rows(path):
    with open(path) as handle:
        return [line.rstrip("\n") for line in handle if line.strip()]


# -- what gets written ------------------------------------------------------

def test_a_heard_packet_becomes_a_row(tmp_path):
    log = MeshtasticLog(str(tmp_path / "heard.csv"))
    log.record(packet(), altitude_m=28450.0, timestamp=1000.0)

    lines = rows(log.filepath)
    assert lines[0] == ",".join(COLUMNS)
    fields = lines[1].split(",")
    assert fields[1] == "28450.0", "the altitude it was heard from"
    assert fields[2] == "0x11223344"
    assert fields[9] == "1", "decrypted"


def test_altitude_is_recorded_because_that_is_the_point(tmp_path):
    """
    A list of nodes is a curiosity. A list of nodes with the height they were
    heard from is propagation data.
    """
    log = MeshtasticLog(str(tmp_path / "heard.csv"))
    for alt in (1200.0, 15000.0, 29000.0):
        log.record(packet(packet_id=int(alt)), altitude_m=alt, timestamp=alt)

    alts = [r.split(",")[1] for r in rows(log.filepath)[1:]]
    assert alts == ["1200.0", "15000.0", "29000.0"]


def test_traffic_it_cannot_decrypt_is_still_recorded(tmp_path):
    """
    Most of what the balloon hears is on channels it holds no key for. That it
    was heard at all, and from how high, is the measurement.
    """
    log = MeshtasticLog(str(tmp_path / "heard.csv"))
    log.record(None, raw=b"\x00" * 42, rssi=-112, snr=-14.25,
               altitude_m=30100.0, timestamp=1000.0)

    fields = rows(log.filepath)[1].split(",")
    assert fields[1] == "30100.0"
    assert fields[7] == "-112"
    assert fields[9] == "0", "not decrypted"
    assert "42 bytes" in fields[11]
    assert log.stats.undecryptable == 1


def test_duplicates_are_marked_but_kept(tmp_path):
    """
    A mesh redelivers the same packet by several paths. Collapsing them would
    throw away the rebroadcast pattern, which is worth seeing.
    """
    log = MeshtasticLog(str(tmp_path / "heard.csv"))
    for _ in range(3):
        log.record(packet(packet_id=777), altitude_m=20000.0, timestamp=1000.0)

    dup_flags = [r.split(",")[10] for r in rows(log.filepath)[1:]]
    assert dup_flags == ["0", "1", "1"]
    assert log.stats.duplicates == 2
    assert log.stats.heard == 3


def test_text_messages_are_readable_in_the_log(tmp_path):
    from common.meshtastic.messages import build_text_message

    log = MeshtasticLog(str(tmp_path / "heard.csv"))
    log.record(packet(port=PortNum.TEXT_MESSAGE_APP,
                      payload=build_text_message("hello from the ground")),
               altitude_m=25000.0, timestamp=1000.0)

    assert "hello from the ground" in rows(log.filepath)[1]


def test_commas_in_a_message_cannot_break_the_csv(tmp_path):
    from common.meshtastic.messages import build_text_message

    log = MeshtasticLog(str(tmp_path / "heard.csv"))
    log.record(packet(port=PortNum.TEXT_MESSAGE_APP,
                      payload=build_text_message('a,b,"c",d')),
               altitude_m=1.0, timestamp=1000.0)

    assert len(rows(log.filepath)[1].split(",")) == len(COLUMNS)


# -- restraint --------------------------------------------------------------

def test_disabled_writes_nothing(tmp_path):
    log = MeshtasticLog(str(tmp_path / "heard.csv"), enabled=False)
    log.record(packet(), altitude_m=20000.0)
    assert not os.path.exists(log.filepath)
    assert log.stats.heard == 0


def test_a_write_failure_never_reaches_the_caller(tmp_path):
    """
    This runs inside the listen window. A full card must not take down the
    thing the balloon is actually there to do.
    """
    log = MeshtasticLog("/nonexistent-root/heard.csv")
    log.record(packet(), altitude_m=20000.0)          # must not raise
    assert log.stats.write_errors == 1


def test_the_seen_cache_is_bounded(tmp_path):
    """A flight hears far more distinct packets than a Pi Zero should hold."""
    from airborne.mesh_log import SEEN_CAPACITY

    log = MeshtasticLog(str(tmp_path / "heard.csv"))
    for i in range(SEEN_CAPACITY + 500):
        log.record(packet(packet_id=i), altitude_m=20000.0, timestamp=1000.0)

    assert len(log._seen) <= SEEN_CAPACITY


def test_it_is_sealed_when_a_key_is_configured(tmp_path):
    """It holds other people's positions; a finder should not be able to read it."""
    from common import sealedbox
    from common.sealedwriter import SealedWriter

    private, public = sealedbox.generate_keypair()
    writer = SealedWriter(enabled=False)
    writer._key = public

    log = MeshtasticLog(str(tmp_path / "heard.csv"), sealed_writer=writer)
    log.record(packet(port=PortNum.TEXT_MESSAGE_APP,
                      payload=b"\x12\x06secret"), altitude_m=20000.0)

    assert log.filepath.endswith(".rhs")
    assert b"secret" not in open(log.filepath, "rb").read()

    raw = open(log.filepath, "rb").read()
    records, offset = [], 0
    while offset < len(raw):
        length = int.from_bytes(raw[offset:offset + 4], "big")
        offset += 4
        records.append(sealedbox.open_sealed(raw[offset:offset + length], private))
        offset += length
    assert records[0].startswith(b"timestamp,")


# -- cruise only ------------------------------------------------------------

def test_the_listen_budget_appears_only_when_a_feature_wants_it():
    from airborne.config import AirborneConfig
    from airborne.transmit_scheduler import schedules_from_config
    from airborne.zone_manager import Zone

    off = schedules_from_config(AirborneConfig())
    assert off[Zone.CRUISE].listen_percent == 0.0

    on = schedules_from_config(AirborneConfig(mesh_log_enabled=True,
                                              mesh_log_rx_percent=10.0))
    assert on[Zone.CRUISE].listen_percent == 10.0
    assert on[Zone.LAUNCH].listen_percent == 0.0, "the pad belongs to imagery"


def test_the_larger_of_the_two_budgets_wins():
    """The window is shared, so both features get every packet heard."""
    from airborne.config import AirborneConfig
    from airborne.transmit_scheduler import schedules_from_config
    from airborne.zone_manager import Zone

    s = schedules_from_config(AirborneConfig(
        repeater_enabled=True, repeater_rx_percent=5.0,
        mesh_log_enabled=True, mesh_log_rx_percent=15.0))
    assert s[Zone.CRUISE].listen_percent == 15.0
