"""
Selective repeating and uplink commands.

The expensive mistakes here are repeating traffic nobody asked the balloon to
carry -- which flattens the battery and congests a regional mesh from an
altitude where the balloon is heard for hundreds of miles -- and accepting a
command from someone who simply transmitted on a public channel.
"""

import pytest

from airborne.meshtastic_beacon import ChannelConfig
from airborne.repeater import DropReason, HeardPacket, MeshtasticRepeater, SeenCache
from common.meshtastic import (
    BROADCAST_ADDR,
    PortNum,
    build_data,
    build_packet,
    build_text_message,
    generate_psk,
    parse_data,
    parse_packet,
)
from common.meshtastic.messages import parse_text_message

BALLOON = 0xEFCAB5AC
STRANGER = 0x0A3139A5


@pytest.fixture
def primary():
    return ChannelConfig(name="LongFast", psk=b"\x01")


@pytest.fixture
def private():
    return ChannelConfig(name="RaptorHAB", psk=generate_psk(32))


@pytest.fixture
def repeater(primary, private):
    return MeshtasticRepeater(
        node_id=BALLOON, primary_channel=primary, private_channel=private,
        tag="!RPT ", max_per_hour=20, min_spacing_sec=30.0,
        commands_enabled=True,
        command_handlers={"ping": lambda a: "pong", "echo": lambda a: " ".join(a)},
    )


def text_packet(text, sender=STRANGER, destination=BROADCAST_ADDR,
                channel=None, packet_id=1000):
    return HeardPacket(
        sender=sender, destination=destination, packet_id=packet_id,
        port=PortNum.TEXT_MESSAGE_APP, payload=build_text_message(text),
        channel_hash=channel.hash if channel else 0x08,
    )


# --- what gets repeated ---------------------------------------------------


def test_untagged_traffic_is_not_repeated(repeater):
    """
    The default and by far the most common case. From altitude the balloon
    hears an enormous amount of traffic that is nothing to do with it.
    """
    allowed, reason = repeater.should_repeat(text_packet("just chatting"), now=1000)
    assert not allowed
    assert reason is DropReason.NOT_TAGGED


def test_a_tagged_broadcast_is_repeated(repeater):
    allowed, reason = repeater.should_repeat(text_packet("!RPT help needed"), now=1000)
    assert allowed
    assert reason is None


def test_a_message_addressed_to_the_balloon_is_repeated(repeater):
    packet = text_packet("relay this", destination=BALLOON)
    allowed, _ = repeater.should_repeat(packet, now=1000)
    assert allowed


def test_our_own_transmissions_are_never_repeated(repeater):
    """Otherwise the balloon would echo its own beacons forever."""
    packet = text_packet("!RPT loop", sender=BALLOON)
    allowed, reason = repeater.should_repeat(packet, now=1000)
    assert not allowed
    assert reason is DropReason.OWN_TRANSMISSION


def test_non_text_traffic_is_not_repeated_unless_addressed(repeater):
    """A stranger's position beacon is not a repeat request."""
    packet = HeardPacket(sender=STRANGER, destination=BROADCAST_ADDR, packet_id=1,
                         port=PortNum.POSITION_APP, payload=b"\x01\x02",
                         channel_hash=0x08)
    allowed, reason = repeater.should_repeat(packet, now=1000)
    assert not allowed
    assert reason is DropReason.NOT_TAGGED


def test_repeating_only_happens_in_cruise(repeater):
    """
    Near the launch site repeating adds nothing and competes with imagery;
    on the ground the battery is better spent beaconing.
    """
    packet = text_packet("!RPT hello")
    allowed, reason = repeater.should_repeat(packet, now=1000, in_cruise=False)
    assert not allowed
    assert reason is DropReason.WRONG_ZONE


def test_a_disabled_repeater_repeats_nothing(primary):
    off = MeshtasticRepeater(node_id=BALLOON, primary_channel=primary, enabled=False)
    allowed, reason = off.should_repeat(text_packet("!RPT hello"), now=1000)
    assert not allowed
    assert reason is DropReason.DISABLED


# --- guard rails ----------------------------------------------------------


def test_the_same_packet_is_not_repeated_twice(repeater):
    """A mesh delivers the same packet by several paths."""
    packet = text_packet("!RPT once", packet_id=4242)

    assert repeater.should_repeat(packet, now=1000)[0]
    repeater.build_repeat(packet, now=1000)

    allowed, reason = repeater.should_repeat(packet, now=1100)
    assert not allowed
    assert reason is DropReason.ALREADY_SEEN


def test_minimum_spacing_is_enforced(repeater):
    first = text_packet("!RPT one", packet_id=1)
    repeater.should_repeat(first, now=1000)
    repeater.build_repeat(first, now=1000)

    second = text_packet("!RPT two", packet_id=2)
    allowed, reason = repeater.should_repeat(second, now=1005)
    assert not allowed
    assert reason is DropReason.TOO_SOON

    assert repeater.should_repeat(second, now=1035)[0]


def test_the_hourly_ceiling_is_enforced(primary):
    """A hard limit, whatever is asked of it."""
    limited = MeshtasticRepeater(node_id=BALLOON, primary_channel=primary,
                                 max_per_hour=3, min_spacing_sec=0)
    now = 1000.0
    for i in range(3):
        packet = text_packet("!RPT x", packet_id=i)
        assert limited.should_repeat(packet, now=now)[0]
        limited.build_repeat(packet, now=now)
        now += 10

    allowed, reason = limited.should_repeat(text_packet("!RPT x", packet_id=99), now=now)
    assert not allowed
    assert reason is DropReason.RATE_LIMITED


def test_the_hourly_ceiling_rolls_forward(primary):
    limited = MeshtasticRepeater(node_id=BALLOON, primary_channel=primary,
                                 max_per_hour=2, min_spacing_sec=0)
    for i in range(2):
        packet = text_packet("!RPT x", packet_id=i)
        limited.should_repeat(packet, now=1000)
        limited.build_repeat(packet, now=1000)

    assert not limited.should_repeat(text_packet("!RPT y", packet_id=50), now=1100)[0]
    # An hour later the window has rolled.
    assert limited.should_repeat(text_packet("!RPT y", packet_id=50), now=1000 + 3700)[0]


def test_the_seen_cache_is_bounded():
    cache = SeenCache(capacity=10, ttl_sec=100)
    for i in range(50):
        cache.add(i, now=1000)
    assert len(cache) <= 10


def test_the_seen_cache_expires_old_entries():
    cache = SeenCache(capacity=100, ttl_sec=60)
    cache.add(1, now=1000)
    assert cache.seen(1, now=1030)
    assert not cache.seen(1, now=1100)


# --- the rebroadcast itself -----------------------------------------------


def test_a_rebroadcast_goes_out_with_hop_limit_zero(repeater, primary):
    """
    From 30 km the balloon is heard across hundreds of miles. If everything
    that hears it forwards it onward, one balloon congests a whole region.
    """
    packet = text_packet("!RPT mayday")
    raw = repeater.build_repeat(packet, now=1000)

    parsed = parse_packet(raw, channel_key=primary.key)
    assert parsed.header.hop_limit == 0
    assert parsed.header.hop_start == 0
    assert parsed.header.is_broadcast


def test_the_tag_is_stripped_from_the_relayed_text(repeater, primary):
    raw = repeater.build_repeat(text_packet("!RPT actual message"), now=1000)
    parsed = parse_packet(raw, channel_key=primary.key)
    assert parse_text_message(parse_data(parsed.payload).payload) == "actual message"


def test_the_rebroadcast_is_sent_as_the_balloon(repeater, primary):
    """The receiving mesh should see who actually put it on the air."""
    raw = repeater.build_repeat(text_packet("!RPT hello"), now=1000)
    assert parse_packet(raw, channel_key=primary.key).header.sender == BALLOON


def test_a_private_channel_repeat_stays_on_that_channel(repeater, private):
    packet = text_packet("!RPT private", channel=private)
    raw = repeater.build_repeat(packet, now=1000)
    parsed = parse_packet(raw, channel_key=private.key)
    assert parsed.header.channel_hash == private.hash


# --- uplink commands ------------------------------------------------------


def test_a_command_on_the_private_channel_runs(repeater, private):
    packet = text_packet("!ping", destination=BALLOON, channel=private)
    reply = repeater.handle_command(packet)

    assert reply is not None
    parsed = parse_packet(reply, channel_key=private.key)
    assert parse_text_message(parse_data(parsed.payload).payload) == "pong"
    assert parsed.header.destination == STRANGER
    assert repeater.stats.commands_run == 1


def test_a_command_on_the_public_channel_is_ignored(repeater, primary):
    """
    The critical one. Anyone can transmit on the public channel, so nothing
    arriving there may ever command the balloon, however it is worded.
    """
    packet = text_packet("!ping", destination=BALLOON, channel=primary)
    assert repeater.handle_command(packet) is None
    assert repeater.stats.commands_run == 0


def test_a_broadcast_command_is_ignored_even_on_the_private_channel(repeater, private):
    """A command must be addressed to this balloon, not shouted at the mesh."""
    packet = text_packet("!ping", destination=BROADCAST_ADDR, channel=private)
    assert repeater.handle_command(packet) is None


def test_an_unknown_command_is_refused_with_a_reply(repeater, private):
    packet = text_packet("!selfdestruct", destination=BALLOON, channel=private)
    reply = repeater.handle_command(packet)

    parsed = parse_packet(reply, channel_key=private.key)
    assert "unknown command" in parse_text_message(parse_data(parsed.payload).payload)
    assert repeater.stats.commands_refused == 1


def test_a_failing_command_replies_rather_than_crashing(private, primary):
    def explode(_args):
        raise RuntimeError("nope")

    repeater = MeshtasticRepeater(
        node_id=BALLOON, primary_channel=primary, private_channel=private,
        command_handlers={"boom": explode})

    reply = repeater.handle_command(
        text_packet("!boom", destination=BALLOON, channel=private))
    parsed = parse_packet(reply, channel_key=private.key)
    assert "failed" in parse_text_message(parse_data(parsed.payload).payload)


def test_command_arguments_are_passed_through(repeater, private):
    packet = text_packet("!echo hello there", destination=BALLOON, channel=private)
    reply = repeater.handle_command(packet)
    parsed = parse_packet(reply, channel_key=private.key)
    assert parse_text_message(parse_data(parsed.payload).payload) == "hello there"


def test_commands_can_be_disabled_entirely(primary, private):
    off = MeshtasticRepeater(node_id=BALLOON, primary_channel=primary,
                             private_channel=private, commands_enabled=False,
                             command_handlers={"ping": lambda a: "pong"})
    assert off.handle_command(
        text_packet("!ping", destination=BALLOON, channel=private)) is None


def test_ordinary_text_is_not_treated_as_a_command(repeater, private):
    packet = text_packet("hello balloon", destination=BALLOON, channel=private)
    assert repeater.handle_command(packet) is None


def test_no_private_channel_means_no_commands(primary):
    solo = MeshtasticRepeater(node_id=BALLOON, primary_channel=primary,
                              command_handlers={"ping": lambda a: "pong"})
    assert solo.handle_command(text_packet("!ping", destination=BALLOON)) is None


# --- decoding -------------------------------------------------------------


def test_a_real_packet_decodes_on_the_right_channel(repeater, private):
    raw = build_packet(
        build_data(PortNum.TEXT_MESSAGE_APP, build_text_message("!RPT over the air")),
        sender=STRANGER, destination=BROADCAST_ADDR,
        channel_key=private.key, channel_hash=private.hash, hop_limit=3)

    packet = repeater.decode(raw, rssi=-95, snr=4.5)
    assert packet is not None
    assert packet.sender == STRANGER
    assert packet.channel_hash == private.hash
    assert packet.rssi == -95
    assert repeater.should_repeat(packet, now=1000)[0]


def test_a_packet_on_an_unknown_channel_does_not_decode(repeater):
    raw = build_packet(
        build_data(PortNum.TEXT_MESSAGE_APP, build_text_message("!RPT nope")),
        sender=STRANGER, channel_key=generate_psk(32), channel_hash=0x77)
    assert repeater.decode(raw) is None


def test_noise_does_not_decode(repeater):
    import os
    assert repeater.decode(os.urandom(48)) is None


# --- reporting ------------------------------------------------------------


def test_status_is_json_friendly(repeater):
    import json

    packet = text_packet("!RPT hi")
    repeater.note_heard(True)
    repeater.should_repeat(packet, now=1000)
    repeater.build_repeat(packet, now=1000)
    repeater.stats.drop(DropReason.NOT_TAGGED)

    status = repeater.get_status()
    json.dumps(status)
    assert status["repeated"] == 1
    assert status["drops"]["not_tagged"] == 1
    assert "ping" in status["commands"]


def test_drop_reasons_are_counted_for_diagnosis(repeater):
    """
    Mostly-not_tagged is the healthy picture. A flight reporting zero heard
    would mean the receive path is not working.
    """
    for i in range(5):
        packet = text_packet("chatter", packet_id=i)
        repeater.note_heard(True)
        _, reason = repeater.should_repeat(packet, now=1000 + i)
        repeater.stats.drop(reason)

    assert repeater.stats.heard == 5
    assert repeater.stats.drops["not_tagged"] == 5
    assert repeater.stats.repeated == 0


def test_a_command_attempt_is_not_repeated(repeater, private):
    """
    Regression from the bench: a command addressed to the balloon was being
    rebroadcast, putting somebody's command text on the air for the whole mesh
    -- including commands refused for arriving on a public channel.
    """
    packet = text_packet("!status", destination=BALLOON, channel=private)
    allowed, reason = repeater.should_repeat(packet, now=1000)
    assert not allowed
    assert reason is DropReason.NOT_TAGGED


def test_a_refused_public_command_is_not_repeated_either(repeater, primary):
    """The case that actually leaked: refused as a command, then relayed."""
    packet = text_packet("!status", destination=BALLOON, channel=primary)
    assert repeater.handle_command(packet) is None
    assert not repeater.should_repeat(packet, now=1000)[0]


def test_an_ordinary_direct_message_is_still_repeated(repeater):
    """Addressing the balloon remains the way to ask for a relay."""
    packet = text_packet("please relay this to the mesh", destination=BALLOON)
    assert repeater.should_repeat(packet, now=1000)[0]
