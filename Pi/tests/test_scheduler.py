"""
Packet scheduler slot allocation.

The regression these tests exist for: the scheduler used to derive both the
telemetry and the image-metadata slot from `packet_counter % interval` in an
if/elif chain. With the shipped defaults (telemetry 5, metadata 10) every
metadata slot was also a telemetry slot, telemetry always won, and image
metadata was NEVER transmitted on schedule. A receiver that dropped the single
metadata packet sent at image start could not decode that image at all.
"""

import pytest

from common.constants import PacketType, SYNC_WORD
from common.protocol import parse_packet
from airborne.packets import PacketScheduler, PacketPriority


def _drain(scheduler, count, telemetry_bytes=b"\x00" * 36):
    """Pull `count` packets and return their parsed packet types."""
    types = []
    for _ in range(count):
        packet = scheduler.get_next_packet(telemetry_bytes)
        if packet is None:
            types.append(None)
            continue
        parsed = parse_packet(packet)
        assert parsed is not None, "scheduler produced an unparseable packet"
        types.append(parsed[0])
    return types


def _counts(types):
    return {t: types.count(t) for t in set(types)}


# Large enough that a 1000-packet sample stays inside a single image:
# 400 kB / 200-byte symbols = 2000 source symbols, ~2500 with 25% overhead.
BIG_IMAGE = b"\xA5" * 400_000


@pytest.fixture
def scheduler():
    sched = PacketScheduler(telemetry_interval=5, image_meta_interval=23)
    sched.queue_image(
        image_id=1,
        image_data=BIG_IMAGE,
        width=1280,
        height=960,
        timestamp=1755400000,
    )
    return sched


def test_image_metadata_is_actually_scheduled(scheduler):
    """Regression for the shadowed-slot bug."""
    types = _drain(scheduler, 1000)
    counts = _counts(types)
    assert counts.get(PacketType.IMAGE_META, 0) > 0, (
        "image metadata was never scheduled; a receiver joining mid-image "
        "could not decode it"
    )


def test_metadata_still_scheduled_when_intervals_are_multiples():
    """
    The old defaults (5 and 10) are the exact pathological case. Independent
    countdowns must keep metadata flowing even for these values.
    """
    sched = PacketScheduler(telemetry_interval=5, image_meta_interval=10)
    sched.queue_image(1, BIG_IMAGE, 1280, 960, 0)

    counts = _counts(_drain(sched, 1000))
    assert counts.get(PacketType.IMAGE_META, 0) > 0
    assert counts.get(PacketType.TELEMETRY, 0) > 0
    assert counts.get(PacketType.IMAGE_DATA, 0) > 0


def test_telemetry_rate_is_close_to_configured_interval(scheduler):
    total = 1000
    counts = _counts(_drain(scheduler, total))
    telemetry = counts.get(PacketType.TELEMETRY, 0)

    # One in five, allowing slack for slots ceded to the metadata packet.
    assert 0.15 * total <= telemetry <= 0.22 * total


def test_metadata_rate_is_close_to_configured_interval(scheduler):
    total = 1000
    counts = _counts(_drain(scheduler, total))
    meta = counts.get(PacketType.IMAGE_META, 0)

    # One in 23, plus one extra emitted when the image transmission starts.
    expected = total / 23
    assert expected * 0.7 <= meta <= expected * 1.4


def test_image_data_dominates_when_an_image_is_queued(scheduler):
    counts = _counts(_drain(scheduler, 1000))
    assert counts.get(PacketType.IMAGE_DATA, 0) > 700


def test_metadata_precedes_first_image_data():
    """A receiver must learn symbol_size before the symbols arrive."""
    sched = PacketScheduler(telemetry_interval=1000, image_meta_interval=1000)
    sched.queue_image(1, b"\xA5" * 5000, 640, 480, 0)

    types = _drain(sched, 10)
    first_meta = types.index(PacketType.IMAGE_META)
    first_data = types.index(PacketType.IMAGE_DATA)
    assert first_meta < first_data


def test_falls_back_to_telemetry_with_no_image_queued():
    sched = PacketScheduler(telemetry_interval=5, image_meta_interval=23)
    types = _drain(sched, 50)
    assert set(types) == {PacketType.TELEMETRY}


def test_priority_packets_jump_the_queue(scheduler):
    scheduler.queue_text_message("BURST DETECTED", priority=PacketPriority.URGENT)
    packet = scheduler.get_next_packet(b"\x00" * 36)
    parsed = parse_packet(packet)
    assert parsed is not None
    assert parsed[0] == PacketType.TEXT_MSG


def test_command_ack_uses_correct_payload_fields(scheduler):
    """Regression: queue_command_ack passed keyword names the payload lacks."""
    scheduler.queue_command_ack(PacketType.CMD_PING, command_seq=42, status=0)
    packet = scheduler.get_next_packet(b"\x00" * 36)
    parsed = parse_packet(packet)
    assert parsed is not None
    assert parsed[0] == PacketType.CMD_ACK


def test_sequence_numbers_are_monotonic_then_wrap(scheduler):
    sequences = []
    for _ in range(20):
        parsed = parse_packet(scheduler.get_next_packet(b"\x00" * 36))
        sequences.append(parsed[1])
    assert sequences == list(range(20))


def test_sequence_wraps_at_65536():
    sched = PacketScheduler(telemetry_interval=1, image_meta_interval=1000)
    sched._sequence = 65534
    seen = [parse_packet(sched.get_next_packet(b"\x00" * 36))[1] for _ in range(4)]
    assert seen == [65534, 65535, 0, 1]


def test_every_packet_carries_the_sync_word(scheduler):
    for _ in range(100):
        packet = scheduler.get_next_packet(b"\x00" * 36)
        assert packet.startswith(SYNC_WORD)


def test_clear_queues_resets_slot_counters(scheduler):
    _drain(scheduler, 50)
    scheduler.clear_queues()
    assert scheduler._since_telemetry == 0
    assert scheduler._since_image_meta == 0
    assert not scheduler.has_pending_data()


def test_queue_image_reports_failure_when_full():
    """
    main.py relies on the return value to decide whether to defer an image;
    the queue is bounded at 5.
    """
    sched = PacketScheduler()
    results = [
        sched.queue_image(i, b"\xA5" * 1000, 320, 240, 0) for i in range(8)
    ]
    assert results[:5] == [True] * 5
    assert False in results, "a full scheduler queue must report failure"


def test_image_progress_advances(scheduler):
    _drain(scheduler, 200)
    progress = scheduler.get_image_progress()
    assert progress["image_id"] == 1
    assert 0 < progress["progress"] <= 100


def test_scheduler_falls_back_to_telemetry_after_image_completes():
    """
    Once the last symbol of the only queued image is sent, the scheduler must
    keep producing telemetry rather than returning None and stalling the
    transmit loop.
    """
    sched = PacketScheduler(telemetry_interval=5, image_meta_interval=23)
    sched.queue_image(1, b"\xA5" * 4000, 320, 240, 0)  # ~25 symbols

    types = _drain(sched, 400)
    assert None not in types
    # The tail of the run, well past image completion, is telemetry only.
    assert set(types[-50:]) == {PacketType.TELEMETRY}
