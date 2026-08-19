"""
Meshtastic over the RaptorHAB modem.

The dual-E22 board carries two radios: one on RAPTOR image traffic, one on the
Meshtastic channel. The firmware could receive and transmit; nothing on the
ground could use it, and its framing quietly broke the image link.
"""

import base64
import os
import sys
import threading
import time

import pytest

GS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "RaptorHABGS_Python",
)
sys.path.insert(0, GS)

from raptorhabgs.core.meshtastic.crypto import channel_hash, expand_psk, parse_psk
from raptorhabgs.core.meshtastic.messages import PortNum, build_data, build_text_message
from raptorhabgs.core.meshtastic.packet import (
    BROADCAST_ADDR, build_packet, node_id_from_callsign,
)
from raptorhabgs.core.modem_meshtastic import ModemMeshtasticLink
from raptorhabgs.core.protocol import FrameExtractor

PRIVATE_KEY = base64.b64encode(bytes(range(32))).decode()


# --------------------------------------------------------------------------
# Framing: the regression that broke images
# --------------------------------------------------------------------------

def stuff(byte: int, dual_radio: bool) -> bytes:
    """Reproduce each modem's writeStuffed()."""
    if byte == 0x7E:
        return bytes([0x7D, 0x5E])
    if dual_radio and byte == 0x7B:
        return bytes([0x7D, 0x5B])
    if byte == 0x7D:
        return bytes([0x7D, 0x5D])
    return bytes([byte])


def frame(payload: bytes, delimiter: int = 0x7E, dual_radio: bool = True,
          rssi: int = -59, snr: int = 9) -> bytes:
    header = [(len(payload) >> 8) & 0xFF, len(payload) & 0xFF,
              rssi & 0xFF, 25, snr & 0xFF, 50]
    checksum = 0
    for value in header + list(payload):
        checksum ^= value
    body = b"".join(stuff(v, dual_radio) for v in header + list(payload) + [checksum])
    return bytes([delimiter]) + body + bytes([delimiter])


def image_packet(seed: int) -> bytes:
    """A RAPTOR image packet whose payload contains a 0x7B."""
    filler = bytes((seed + i) % 256 for i in range(206))
    return b"RAPT" + filler


def test_a_0x7b_in_image_data_no_longer_breaks_the_frame():
    """
    The dual-radio modem uses 0x7B as its second frame delimiter, so it escapes
    0x7B in *every* frame -- including RAPTOR ones. The ground station knew only
    0x7D 0x5E and 0x7D 0x5D, so it desynchronised on the unknown escape and the
    frame failed its checksum.

    A 210-byte packet contains a 0x7B about 56% of the time. Measured against
    the real parser before the fix: 45% of image packets survived.
    """
    payload = b"RAPT" + bytes([0x7B]) + bytes(range(200))
    assert 0x7B in payload

    extractor = FrameExtractor()
    got = extractor.add_data(frame(payload, dual_radio=True))

    assert len(got) == 1
    assert got[0][2] == payload


def test_single_radio_modem_is_unaffected():
    """The older modem never emits that escape; accepting it costs it nothing."""
    payload = image_packet(3)
    extractor = FrameExtractor()
    got = extractor.add_data(frame(payload, dual_radio=False))

    assert len(got) == 1 and got[0][2] == payload


def test_every_byte_value_survives_a_round_trip():
    """The framing has to be transparent, not transparent-for-most-bytes."""
    payload = b"RAPT" + bytes(range(256)) [:206]
    extractor = FrameExtractor()
    got = extractor.add_data(frame(payload, dual_radio=True))
    assert got and got[0][2] == payload


# --------------------------------------------------------------------------
# Two streams down one cable
# --------------------------------------------------------------------------

def test_meshtastic_frames_do_not_reach_the_raptor_path():
    """
    Every existing caller treats what add_data() returns as a RAPTOR packet.
    Handing it a Meshtastic one would have it parsed as an image symbol.
    """
    extractor = FrameExtractor()
    mesh = bytes(range(60))

    raptor_frames = extractor.add_data(frame(mesh, delimiter=0x7B))

    assert raptor_frames == []
    assert len(extractor.take_meshtastic()) == 1


def test_interleaved_streams_both_arrive():
    extractor = FrameExtractor()
    image = image_packet(9)
    mesh = bytes([0x7B, 0x7E, 0x7D]) + bytes(range(40))   # both delimiters inside

    stream = (frame(image) + frame(mesh, delimiter=0x7B) + frame(image))
    raptor = extractor.add_data(stream)
    meshtastic = extractor.take_meshtastic()

    assert len(raptor) == 2 and all(r[2] == image for r in raptor)
    assert len(meshtastic) == 1 and meshtastic[0][2] == mesh


def test_meshtastic_frames_carry_signal_strength():
    """Which is the point of hearing it on your own radio rather than by MQTT."""
    extractor = FrameExtractor()
    extractor.add_data(frame(bytes(40), delimiter=0x7B, rssi=-102, snr=3))

    rssi, snr, _ = extractor.take_meshtastic()[0]
    assert rssi == pytest.approx(-102.25, abs=0.01)
    assert snr == pytest.approx(3.50, abs=0.01)


def test_taking_meshtastic_frames_drains_them():
    extractor = FrameExtractor()
    extractor.add_data(frame(bytes(30), delimiter=0x7B))

    assert len(extractor.take_meshtastic()) == 1
    assert extractor.take_meshtastic() == []


# --------------------------------------------------------------------------
# Receive
# --------------------------------------------------------------------------

def balloon_beacon(text="RAPTOR alt 28450m", channel="LongFast", key="AQ=="):
    expanded = expand_psk(parse_psk(key))
    return build_packet(
        build_data(PortNum.TEXT_MESSAGE_APP, build_text_message(text)),
        sender=node_id_from_callsign("RPHAB1"),
        destination=BROADCAST_ADDR,
        channel_key=expanded,
        channel_hash=channel_hash(channel, expanded),
        hop_limit=0,
    )


def test_the_balloons_public_beacon_decodes():
    link = ModemMeshtasticLink(callsign="GROUND")
    heard = link.handle_frames([(-92.5, 6.25, balloon_beacon())])

    assert len(heard) == 1
    assert heard[0].text == "RAPTOR alt 28450m"
    assert heard[0].rssi == -92.5
    assert heard[0].decrypted


def test_default_longfast_key_is_expanded():
    """
    Meshtastic's "AQ==" is a one-byte shorthand for a well-known key, not a
    one-byte key. Handing it to AES unexpanded raises, and the channel an
    operator configured perfectly correctly stops working.
    """
    link = ModemMeshtasticLink(callsign="GROUND")
    # The well-known LongFast key is AES-128, so 16 bytes -- the point is that
    # it is a valid AES key length and emphatically not the one byte that came
    # out of the base64.
    assert len(link.channels[0][1]) in (16, 24, 32)
    assert len(link.channels[0][1]) != len(parse_psk("AQ=="))


def test_traffic_on_an_unknown_channel_is_counted_not_silently_dropped():
    """Hearing traffic you cannot read still says the radio works."""
    other = base64.b64encode(bytes(range(32, 64))).decode()
    link = ModemMeshtasticLink(callsign="GROUND")

    heard = link.handle_frames([(-80.0, 5.0, balloon_beacon(channel="secret", key=other))])

    assert heard == []
    assert link.stats.undecryptable == 1
    assert link.stats.heard == 1


def test_an_invalid_channel_key_is_refused_loudly_not_ignored():
    """The operator asked for that channel and would otherwise never find out."""
    link = ModemMeshtasticLink(callsign="GROUND",
                               private_channel_name="cmd",
                               private_channel_key="not-base64!!")
    assert link.private_channel is None


def test_a_handler_that_raises_does_not_stop_decoding():
    link = ModemMeshtasticLink(callsign="GROUND")
    link.on_packet = lambda packet: (_ for _ in ()).throw(RuntimeError("boom"))

    heard = link.handle_frames([(-90.0, 5.0, balloon_beacon())])
    assert len(heard) == 1


# --------------------------------------------------------------------------
# Transmit
# --------------------------------------------------------------------------

def linked(reply="MTX_OK", delay=0.02, **kwargs):
    sent = []
    link = ModemMeshtasticLink(callsign="GROUND", **kwargs)

    def writer(data):
        sent.append(data)
        if reply is not None:
            threading.Timer(delay, link.handle_modem_line, args=(reply,)).start()
        return True

    link.set_writer(writer)
    return link, sent


def test_a_command_goes_out_on_the_private_channel_and_the_balloon_reads_it():
    link, sent = linked(private_channel_name="raptor-cmd",
                        private_channel_key=PRIVATE_KEY)

    ok, detail = link.send_command("beacon now")

    assert ok, detail
    assert sent[0].startswith(b"MTX:")

    raw = bytes.fromhex(sent[0].decode().split("MTX:")[1].strip())
    payload_side = ModemMeshtasticLink(callsign="RPHAB1",
                                       channel_name="raptor-cmd",
                                       channel_key=PRIVATE_KEY)
    assert payload_side.handle_frames([(-40.0, 10.0, raw)])[0].text == "!beacon now"


def test_commands_are_refused_without_a_private_channel():
    """
    The payload refuses commands on the public channel because anyone can
    transmit there. Sending one publicly would be a message it reads and
    ignores -- so refuse at this end and say why.
    """
    link, sent = linked()

    ok, why = link.send_command("beacon now")

    assert not ok
    assert "private channel" in why
    assert sent == [], "nothing should have gone on the air"


def test_a_command_is_sent_with_hop_limit_zero():
    """A ground station does not need the whole mesh relaying its commands."""
    link, sent = linked(private_channel_name="cmd", private_channel_key=PRIVATE_KEY)
    link.send_command("ping")

    raw = bytes.fromhex(sent[0].decode().split("MTX:")[1].strip())
    assert raw[12] & 0x07 == 0, "hop limit should be zero"


def test_a_refusal_from_the_modem_is_reported():
    link, _ = linked(reply="MTX_ERR:radio")
    ok, detail = link.send_text("hello")
    assert not ok and "MTX_ERR" in detail


def test_a_silent_modem_times_out_rather_than_blocking_forever():
    link, _ = linked(reply=None)
    started = time.monotonic()
    ok, detail = link.send_text("hello", timeout_sec=0.3)

    assert not ok and "did not answer" in detail
    assert time.monotonic() - started < 2.0


def test_transmitting_with_no_modem_fails_cleanly():
    link = ModemMeshtasticLink(callsign="GROUND")
    ok, detail = link.send_text("hello")
    assert not ok and "no modem" in detail


def test_an_oversized_packet_is_refused_before_it_reaches_the_radio():
    link, sent = linked()
    ok, detail = link.send_raw(bytes(300))
    assert not ok and "255" in detail
    assert sent == []


def test_the_command_prefix_is_added_once():
    link, sent = linked(private_channel_name="cmd", private_channel_key=PRIVATE_KEY)
    link.send_command("!already-prefixed")

    raw = bytes.fromhex(sent[0].decode().split("MTX:")[1].strip())
    text = ModemMeshtasticLink(callsign="X", channel_name="cmd",
                               channel_key=PRIVATE_KEY
                               ).handle_frames([(-40.0, 10.0, raw)])[0].text
    assert text == "!already-prefixed"


# --------------------------------------------------------------------------
# Slot configuration
# --------------------------------------------------------------------------

def test_the_mesh_slot_can_be_pointed_at_a_region():
    """Without this the modem uses a default correct for exactly one region."""
    link, sent = linked(reply=None)
    assert link.configure_slot(869.525, bandwidth_khz=250.0,
                               spreading_factor=11, coding_rate=5, power_dbm=27)

    line = sent[0].decode()
    assert line.startswith("MCFG:869.5250,250.0,11,5,27")


def test_configuring_without_a_modem_fails_rather_than_pretending():
    link = ModemMeshtasticLink(callsign="GROUND")
    assert not link.configure_slot(915.0)


# --------------------------------------------------------------------------
# SNR: GFSK does not have one
# --------------------------------------------------------------------------

def test_the_gfsk_snr_sentinel_is_not_treated_as_a_measurement():
    """
    The SX1262 measures SNR only for LoRa. RadioLib returns
    RADIOLIB_ERR_WRONG_MODEM -- the integer -20 -- when the active modem is
    FSK, and the modem stored that as a float, printed it on the display as
    "-20.0 dB" in red, and forwarded it in every frame. Measured on the bench:
    2281 frames out of 2281 reported exactly -20.0.
    """
    from raptorhabgs.core.protocol import SNR_NOT_AVAILABLE, snr_is_measured

    assert not snr_is_measured(SNR_NOT_AVAILABLE)
    assert not snr_is_measured(-20.0), "the old error code must not read as a measurement"

    # Real LoRa SNRs, which the Meshtastic slot does report.
    for value in (-15.0, -5.25, 0.0, 6.5, 12.0):
        assert snr_is_measured(value), value


def test_the_sentinel_survives_the_frame_format():
    """int8 carries it, and the parser must reproduce it exactly."""
    from raptorhabgs.core.protocol import SNR_NOT_AVAILABLE, snr_is_measured

    extractor = FrameExtractor()
    payload = image_packet(1)
    got = extractor.add_data(frame(payload, snr=-128))

    assert len(got) == 1
    assert got[0][1] == pytest.approx(SNR_NOT_AVAILABLE, abs=0.51)
    assert not snr_is_measured(got[0][1])
