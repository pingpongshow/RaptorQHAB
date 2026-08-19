"""
Fountain code encode/decode round-trips.

Runs the airborne encoder against the ground station's decoder -- the actual
air-to-ground image path -- so that an image which goes up comes back down
byte-identical.
"""

import random

import pytest

from airborne.fountain import (
    FountainEncoder,
    IncompatibleEncoderError,
    LTEncoder,
    raptorq_available,
)

requires_raptorq = pytest.mark.skipif(
    not raptorq_available(), reason="raptorq wheel not installed"
)


def _random_bytes(size, seed):
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(size))


def _round_trip(data, symbol_size=200, loss_rate=0.0, max_symbols=20000, seed=1):
    """
    Encode `data`, feed symbols to the ground decoder (optionally dropping
    some), and return the reconstructed bytes, or None if never completed.
    """
    from ground.decoder import RaptorQDecoder

    encoder = FountainEncoder(data, symbol_size=symbol_size)
    decoder = RaptorQDecoder(
        num_source_symbols=encoder.num_source_symbols,
        symbol_size=symbol_size,
        total_size=len(data),
    )
    rng = random.Random(seed)

    for _ in range(max_symbols):
        symbol_id, symbol_data = encoder.generate_symbol()
        if loss_rate and rng.random() < loss_rate:
            continue
        if decoder.add_symbol(symbol_id, symbol_data):
            return decoder.get_decoded_data()

    return None


# --- the real air-to-ground path -----------------------------------------


@requires_raptorq
@pytest.mark.parametrize("size", [1, 199, 200, 201, 1000, 20000, 60000])
def test_round_trip_recovers_exact_bytes(size):
    data = _random_bytes(size, seed=size)
    recovered = _round_trip(data)
    assert recovered is not None, f"decoding never completed for {size} bytes"
    assert recovered == data


@requires_raptorq
@pytest.mark.parametrize("loss_rate", [0.1, 0.3, 0.5])
def test_round_trip_survives_packet_loss(loss_rate):
    """Fountain codes exist precisely so a lossy link still delivers."""
    data = _random_bytes(20000, seed=7)
    recovered = _round_trip(data, loss_rate=loss_rate)
    assert recovered is not None, f"decoding failed at {loss_rate:.0%} loss"
    assert recovered == data


@requires_raptorq
def test_round_trip_of_a_realistically_sized_image():
    """A 1280x960 WebP at quality 75 lands in this range."""
    data = _random_bytes(45000, seed=99)
    recovered = _round_trip(data)
    assert recovered == data


@requires_raptorq
def test_decoder_reports_incomplete_until_it_has_enough():
    from ground.decoder import RaptorQDecoder

    data = _random_bytes(20000, seed=3)
    encoder = FountainEncoder(data, symbol_size=200)
    decoder = RaptorQDecoder(encoder.num_source_symbols, 200, len(data))

    for _ in range(10):  # far fewer than the ~100 source symbols needed
        symbol_id, symbol_data = encoder.generate_symbol()
        decoder.add_symbol(symbol_id, symbol_data)

    assert not decoder.is_complete()
    assert decoder.get_decoded_data() is None


# --- symbol sizing on the wire -------------------------------------------


@requires_raptorq
def test_raptorq_symbols_carry_a_four_byte_payload_id():
    """
    Regression: the receive path assumed IMAGE_DATA symbols were exactly
    symbol_size bytes, but RaptorQ prefixes a 4-byte payload ID.
    """
    from common.constants import FOUNTAIN_SYMBOL_SIZE, RAPTORQ_PAYLOAD_ID_SIZE

    encoder = FountainEncoder(_random_bytes(20000, seed=5), symbol_size=200)
    for _ in range(20):
        _, symbol_data = encoder.generate_symbol()
        assert len(symbol_data) == FOUNTAIN_SYMBOL_SIZE + RAPTORQ_PAYLOAD_ID_SIZE


@requires_raptorq
def test_image_data_packets_fit_the_radio_payload():
    from common.constants import IMAGE_DATA_HEADER_SIZE, MAX_PAYLOAD_SIZE

    encoder = FountainEncoder(_random_bytes(20000, seed=5), symbol_size=200)
    for _ in range(20):
        _, symbol_data = encoder.generate_symbol()
        assert len(symbol_data) + IMAGE_DATA_HEADER_SIZE <= MAX_PAYLOAD_SIZE


@requires_raptorq
def test_num_source_symbols_matches_data_length():
    """PacketScheduler puts this in IMAGE_META; the receiver sizes on it."""
    for size, expected in ((200, 1), (201, 2), (1000, 5), (1001, 6)):
        encoder = FountainEncoder(b"\x00" * size, symbol_size=200)
        assert encoder.num_source_symbols == expected


@requires_raptorq
def test_recommended_symbol_count_includes_overhead():
    encoder = FountainEncoder(b"\x00" * 20000, symbol_size=200)
    assert encoder.get_recommended_symbol_count(0) >= 100
    assert encoder.get_recommended_symbol_count(25) > encoder.get_recommended_symbol_count(0)


@requires_raptorq
def test_encoder_keeps_producing_symbols_past_the_pregenerated_batch():
    """A very lossy link needs more repair symbols than were pre-generated."""
    encoder = FountainEncoder(b"\xA5" * 20000, symbol_size=200)
    seen = {encoder.generate_symbol()[0] for _ in range(5000)}
    assert len(seen) == 5000


# --- the LT fallback guard -----------------------------------------------


def test_lt_fallback_is_refused_by_default():
    """
    Regression: FountainEncoder used to fall back to LT silently. The ground
    station has no LT decoding path, so that cost an entire flight's imagery
    while logging one INFO line.
    """
    with pytest.raises(IncompatibleEncoderError) as excinfo:
        FountainEncoder(b"\xA5" * 5000, symbol_size=200, prefer_raptorq=False)

    assert "cannot decode" in str(excinfo.value)


def test_lt_fallback_requires_explicit_opt_in():
    encoder = FountainEncoder(
        b"\xA5" * 5000, symbol_size=200, prefer_raptorq=False, allow_lt_fallback=True
    )
    assert encoder.encoder_type == "LT"


def test_scheduler_refuses_to_queue_images_it_cannot_encode(monkeypatch):
    import airborne.fountain as fountain_module
    from airborne.packets import PacketScheduler

    monkeypatch.setattr(fountain_module, "RAPTORQ_AVAILABLE", False)

    sched = PacketScheduler(allow_lt_fallback=False)
    assert not sched.queue_image(1, b"\xA5" * 5000, 320, 240, 0)
    assert not sched.has_pending_data()


# --- LT encoder structure (used only on the bench) ------------------------


def test_lt_encoder_symbol_size_is_exact():
    encoder = LTEncoder(b"\xA5" * 5000, symbol_size=200)
    for _ in range(50):
        _, symbol_data = encoder.generate_symbol()
        assert len(symbol_data) == 200


def test_lt_encoder_symbol_ids_are_unique():
    encoder = LTEncoder(b"\xA5" * 20000, symbol_size=200)
    ids = [encoder.generate_symbol()[0] for _ in range(2000)]
    assert len(set(ids)) == len(ids)


def test_lt_encoder_pads_to_whole_symbols():
    for size, expected in ((200, 1), (201, 2), (1000, 5), (1001, 6)):
        assert LTEncoder(b"\x00" * size, symbol_size=200).num_source_symbols == expected
