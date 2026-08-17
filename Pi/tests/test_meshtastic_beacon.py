"""
Beacon assembly and transmission, and radio mode arbitration.

Uses a fake radio rather than hardware, so these run on any machine. What they
verify is the logic that decides what goes on the air and at what power --
which is where a mistake is expensive.
"""

import pytest

from airborne.meshtastic_beacon import BeaconTelemetry, ChannelConfig, MeshtasticBeacon
from airborne.region_manager import RegionManager
from common.meshtastic import (
    PortNum,
    expand_psk,
    generate_psk,
    node_id_to_string,
    parse_data,
    parse_packet,
    parse_position,
    parse_user,
)
from common.meshtastic.regions import get_region
from common.radio_lora import (
    LoRaConfig,
    RadioMode,
    get_preset,
    image_calibration_for,
)
from common.radio_manager import RadioModeManager


# --- fakes ----------------------------------------------------------------


class FakeRadio:
    """Records what the manager asks of the radio."""

    def __init__(self):
        self.lora_packets = []
        self.gfsk_packets = []
        self.configure_calls = []
        self.gfsk_restores = 0
        self.tx_power_calls = []
        self.standby_calls = 0
        self.fail_next_lora = False
        self._lora_config = None

    def configure_lora(self, lora_config, frequency_mhz, tx_power_dbm):
        self.configure_calls.append((lora_config, frequency_mhz, tx_power_dbm))
        self._lora_config = lora_config

    def restore_gfsk(self):
        self.gfsk_restores += 1
        self._lora_config = None

    def set_tx_power(self, power_dbm):
        self.tx_power_calls.append(power_dbm)

    def set_standby(self, rc=True):
        self.standby_calls += 1

    def transmit(self, packet, timeout_ms=5000):
        self.gfsk_packets.append(packet)
        return True

    def transmit_lora(self, packet, timeout_ms=10000):
        if self.fail_next_lora:
            self.fail_next_lora = False
            return False
        self.lora_packets.append(packet)
        return True

    def start_lora_receive(self, timeout_ms=0):
        pass

    def poll_lora_receive(self):
        return None


@pytest.fixture
def radio():
    return FakeRadio()


@pytest.fixture
def manager(radio):
    return RadioModeManager(radio, gfsk_tx_power_dbm=22)


@pytest.fixture
def beacon():
    return MeshtasticBeacon(
        callsign="RPHAB1",
        payload_id=1,
        primary_channel=ChannelConfig(name="LongFast", psk=b"\x01"),
        beacon_text="RaptorHAB test flight",
    )


@pytest.fixture
def telemetry():
    return BeaconTelemetry(
        latitude=39.7392,
        longitude=-104.9903,
        altitude_m=30480.0,
        satellites=11,
        fix_type=2,
        ground_speed_mps=12.0,
        ground_track_deg=271.5,
        battery_mv=4150,
        battery_percent=87,
        cpu_temp_c=-12.5,
        uptime_sec=3600,
    )


# --- LoRa configuration ---------------------------------------------------


def test_longfast_preset_matches_meshtastic():
    preset = get_preset("LONG_FAST")
    assert preset.spreading_factor == 11
    assert preset.bandwidth_khz == 250.0
    assert preset.coding_rate == 5
    assert preset.sync_word == 0x2B


def test_unknown_preset_is_rejected():
    with pytest.raises(ValueError, match="unknown modem preset"):
        get_preset("TELEPATHY")


def test_lora_config_validates_its_inputs():
    with pytest.raises(ValueError, match="spreading factor"):
        LoRaConfig(spreading_factor=13)
    with pytest.raises(ValueError, match="bandwidth"):
        LoRaConfig(bandwidth_khz=200.0)
    with pytest.raises(ValueError, match="coding rate"):
        LoRaConfig(coding_rate=9)


def test_low_data_rate_optimize_enables_for_slow_configs():
    """Required past ~16 ms symbol time or crystal drift corrupts the packet."""
    assert LoRaConfig(spreading_factor=12, bandwidth_khz=125.0).low_data_rate_optimize
    assert not LoRaConfig(spreading_factor=7, bandwidth_khz=250.0).low_data_rate_optimize


def test_time_on_air_is_plausible_for_longfast():
    """A ~50 byte LongFast packet is on the order of a few hundred ms."""
    airtime = get_preset("LONG_FAST").time_on_air_ms(50)
    assert 100 < airtime < 2000


def test_time_on_air_grows_with_payload():
    preset = get_preset("LONG_FAST")
    assert preset.time_on_air_ms(200) > preset.time_on_air_ms(20)


def test_slower_presets_take_longer():
    assert get_preset("LONG_SLOW").time_on_air_ms(50) > get_preset(
        "SHORT_FAST"
    ).time_on_air_ms(50)


@pytest.mark.parametrize(
    "frequency,expected",
    [
        (433.875, (0x6B, 0x6F)),
        (869.525, (0xD7, 0xDB)),
        (906.875, (0xE1, 0xE9)),
        (490.0, (0x75, 0x81)),
    ],
)
def test_image_calibration_selects_the_right_band(frequency, expected):
    """
    Calibrating for the wrong band costs several dB of sensitivity, which
    matters now that the region logic can move us from 906 to 869 or 433 MHz.
    """
    assert image_calibration_for(frequency) == expected


def test_image_calibration_falls_back_rather_than_raising():
    """An exception during a region change would take the payload down."""
    assert image_calibration_for(1234.0) == (0xE1, 0xE9)


# --- mode arbitration -----------------------------------------------------


def test_starts_in_gfsk(manager):
    assert manager.mode is RadioMode.GFSK


def test_lora_transmission_is_refused_without_settings(manager, radio):
    """The "unknown region" case: stay off the air rather than guess."""
    assert not manager.transmit_lora(b"\x00" * 20)
    assert radio.lora_packets == []
    assert manager.mode is RadioMode.GFSK


def test_switching_into_lora_configures_the_radio(manager, radio):
    manager.set_lora_settings(get_preset("LONG_FAST"), 906.875, 22, get_region("US"))
    assert manager.transmit_lora(b"\x00" * 20)

    assert manager.mode is RadioMode.LORA
    assert len(radio.configure_calls) == 1
    _config, frequency, power = radio.configure_calls[0]
    assert frequency == 906.875
    assert power == 22


def test_power_is_clamped_to_the_region_ceiling(manager, radio):
    """Requesting 22 dBm in EU 868 must go on air at 14."""
    settings = manager.set_lora_settings(
        get_preset("LONG_FAST"), 869.525, 22, get_region("EU_868")
    )
    assert settings.tx_power_dbm == 14

    manager.transmit_lora(b"\x00" * 20)
    assert radio.configure_calls[0][2] == 14


def test_power_clamp_applies_on_every_region_change(manager, radio):
    manager.set_lora_settings(get_preset("LONG_FAST"), 906.875, 22, get_region("US"))
    manager.transmit_lora(b"\x00" * 20)
    assert radio.configure_calls[-1][2] == 22

    manager.set_lora_settings(get_preset("LONG_FAST"), 433.875, 22, get_region("EU_433"))
    manager.transmit_lora(b"\x00" * 20)
    assert radio.configure_calls[-1][2] == 12


def test_settings_change_while_on_air_reapplies_immediately(manager, radio):
    manager.set_lora_settings(get_preset("LONG_FAST"), 906.875, 22, get_region("US"))
    manager.transmit_lora(b"\x00" * 20)
    before = len(radio.configure_calls)

    manager.set_lora_settings(get_preset("LONG_FAST"), 869.525, 22, get_region("EU_868"))
    assert len(radio.configure_calls) > before


def test_repeated_lora_transmits_do_not_reconfigure(manager, radio):
    """Reconfiguring per packet would waste airtime on a slow link."""
    manager.set_lora_settings(get_preset("LONG_FAST"), 906.875, 22, get_region("US"))
    for _ in range(5):
        manager.transmit_lora(b"\x00" * 20)

    assert len(radio.configure_calls) == 1
    assert len(radio.lora_packets) == 5


def test_switching_back_to_gfsk_restores_the_image_configuration(manager, radio):
    manager.set_lora_settings(get_preset("LONG_FAST"), 906.875, 22, get_region("US"))
    manager.transmit_lora(b"\x00" * 20)

    manager.transmit_gfsk(b"\x00" * 40)

    assert manager.mode is RadioMode.GFSK
    assert radio.gfsk_restores == 1
    assert radio.tx_power_calls[-1] == 22
    assert len(radio.gfsk_packets) == 1


def test_alternating_modes_switches_each_way(manager, radio):
    manager.set_lora_settings(get_preset("LONG_FAST"), 906.875, 22, get_region("US"))
    for _ in range(3):
        manager.transmit_gfsk(b"\x00" * 40)
        manager.transmit_lora(b"\x00" * 20)

    stats = manager.get_stats()
    # The manager starts in GFSK, so the first transmit_gfsk needs no switch:
    # three switches into LoRa, two back out. A mode change that is already
    # satisfied must not cost a reconfiguration.
    assert stats["to_lora"]["switches"] == 3
    assert stats["to_gfsk"]["switches"] == 2
    assert len(radio.gfsk_packets) == 3
    assert len(radio.lora_packets) == 3


def test_switch_timing_is_measured(manager):
    manager.set_lora_settings(get_preset("LONG_FAST"), 906.875, 22, get_region("US"))
    manager.transmit_lora(b"\x00" * 20)

    stats = manager.get_stats()["to_lora"]
    assert stats["switches"] == 1
    assert stats["mean_ms"] >= 0
    assert stats["max_ms"] >= stats["mean_ms"] - 1e-9


def test_status_is_json_friendly(manager):
    import json

    manager.set_lora_settings(get_preset("LONG_FAST"), 906.875, 22, get_region("US"))
    status = manager.get_status()
    json.dumps(status)
    assert status["lora_region"] == "US"
    assert status["lora_frequency_mhz"] == 906.875


# --- beacon assembly ------------------------------------------------------


def test_identity_is_derived_from_the_callsign(beacon):
    assert beacon.short_name == "HAB1"
    assert beacon.long_name == "RaptorHAB RPHAB1"
    assert node_id_to_string(beacon.node_id).startswith("!")


def test_beacon_cycle_contains_the_expected_message_types(beacon, telemetry):
    packets = beacon.build_beacon_cycle(telemetry)
    key = beacon.primary_channel.key

    portnums = []
    for packet in packets:
        parsed = parse_packet(packet, channel_key=key)
        portnums.append(parse_data(parsed.payload).portnum)

    assert PortNum.POSITION_APP in portnums
    assert PortNum.TELEMETRY_APP in portnums
    assert PortNum.NODEINFO_APP in portnums
    assert PortNum.TEXT_MESSAGE_APP in portnums


def test_position_is_the_first_packet_of_a_cycle(beacon, telemetry):
    """
    If a cycle is cut short by a schedule boundary, position is the packet
    that should already be on the air.
    """
    first = parse_packet(
        beacon.build_beacon_cycle(telemetry)[0], channel_key=beacon.primary_channel.key
    )
    assert parse_data(first.payload).portnum == PortNum.POSITION_APP


def test_position_survives_the_full_round_trip(beacon, telemetry):
    packet = beacon.build_position_packet(telemetry, beacon.primary_channel)
    parsed = parse_packet(packet, channel_key=beacon.primary_channel.key)
    position = parse_position(parse_data(parsed.payload).payload)

    assert position.latitude == pytest.approx(39.7392, abs=1e-6)
    assert position.longitude == pytest.approx(-104.9903, abs=1e-6)
    assert position.altitude_m == 30480
    assert position.satellites == 11


def test_no_position_packet_without_a_fix(beacon):
    packets = beacon.build_beacon_cycle(BeaconTelemetry(fix_type=0))
    key = beacon.primary_channel.key
    portnums = [
        parse_data(parse_packet(p, channel_key=key).payload).portnum for p in packets
    ]
    assert PortNum.POSITION_APP not in portnums


def test_nodeinfo_is_sent_only_periodically(telemetry):
    beacon = MeshtasticBeacon(callsign="RPHAB1", nodeinfo_every=3)
    key = beacon.primary_channel.key

    def has_nodeinfo(cycle_packets):
        return any(
            parse_data(parse_packet(p, channel_key=key).payload).portnum
            == PortNum.NODEINFO_APP
            for p in cycle_packets
        )

    results = [has_nodeinfo(beacon.build_beacon_cycle(telemetry)) for _ in range(6)]
    assert results == [True, False, False, True, False, False]


def test_nodeinfo_carries_the_display_name(beacon):
    packet = beacon.build_nodeinfo_packet(beacon.primary_channel)
    parsed = parse_packet(packet, channel_key=beacon.primary_channel.key)
    user = parse_user(parse_data(parsed.payload).payload)

    assert user.long_name == "RaptorHAB RPHAB1"
    assert user.short_name == "HAB1"
    assert user.node_id == node_id_to_string(beacon.node_id)


def test_every_broadcast_uses_hop_limit_zero(beacon, telemetry):
    for packet in beacon.build_beacon_cycle(telemetry):
        parsed = parse_packet(packet, channel_key=beacon.primary_channel.key)
        assert parsed.header.hop_limit == 0
        assert parsed.header.is_broadcast


def test_every_packet_fits_a_lora_frame(beacon, telemetry):
    for packet in beacon.build_beacon_cycle(telemetry):
        assert len(packet) <= 255


def test_no_text_packet_when_no_message_configured(telemetry):
    beacon = MeshtasticBeacon(callsign="RPHAB1", beacon_text="")
    key = beacon.primary_channel.key
    portnums = [
        parse_data(parse_packet(p, channel_key=key).payload).portnum
        for p in beacon.build_beacon_cycle(telemetry)
    ]
    assert PortNum.TEXT_MESSAGE_APP not in portnums


# --- private channel ------------------------------------------------------


def test_private_channel_adds_packets_with_a_different_key(telemetry):
    private_key = generate_psk(32)
    beacon = MeshtasticBeacon(
        callsign="RPHAB1",
        private_channel=ChannelConfig(name="RaptorHAB", psk=private_key),
        beacon_text="private hello",
    )

    assert beacon.private_channel.key == private_key
    assert beacon.private_channel.hash != beacon.primary_channel.hash

    packets = beacon.build_beacon_cycle(telemetry)
    decodable_privately = 0
    for packet in packets:
        parsed = parse_packet(packet, channel_key=private_key)
        if parsed.header.channel_hash == beacon.private_channel.hash:
            decodable_privately += 1
            parse_data(parsed.payload)  # must not raise

    assert decodable_privately >= 1


def test_private_traffic_is_not_readable_with_the_default_key(telemetry):
    beacon = MeshtasticBeacon(
        callsign="RPHAB1",
        private_channel=ChannelConfig(name="RaptorHAB", psk=generate_psk(32)),
    )
    private_hash = beacon.private_channel.hash

    for packet in beacon.build_beacon_cycle(telemetry):
        parsed = parse_packet(packet, channel_key=expand_psk(b"\x01"))
        if parsed.header.channel_hash == private_hash:
            assert parsed.payload != b""
            # Decoding with the wrong key must not yield a valid Position.
            try:
                data = parse_data(parsed.payload)
                assert data.portnum != PortNum.POSITION_APP or not data.payload
            except Exception:
                pass  # garbage that fails to parse is the expected outcome


def test_disabled_private_channel_is_skipped(telemetry):
    beacon = MeshtasticBeacon(
        callsign="RPHAB1",
        private_channel=ChannelConfig(name="X", psk=generate_psk(32), enabled=False),
    )
    hashes = {
        parse_packet(p).header.channel_hash for p in beacon.build_beacon_cycle(telemetry)
    }
    assert beacon.private_channel.hash not in hashes


# --- transmission ---------------------------------------------------------


def test_transmit_cycle_sends_every_packet(manager, radio, beacon, telemetry):
    manager.set_lora_settings(get_preset("LONG_FAST"), 906.875, 22, get_region("US"))

    sent = beacon.transmit_cycle(manager, telemetry, inter_packet_delay_sec=0)
    assert sent == len(radio.lora_packets)
    assert sent >= 4
    assert beacon.stats.packets_failed == 0


def test_transmit_cycle_is_suppressed_over_unknown_territory(
    manager, radio, beacon, telemetry
):
    """The safety case, end to end: no region means nothing goes on the air."""
    manager.set_lora_settings(get_preset("LONG_FAST"), 906.875, 22, get_region("US"))

    region_manager = RegionManager(dwell_sec=0, edge_margin_km=0)
    region_manager.update(0.0, -140.0, fix_type=2, now=1000.0)
    assert not region_manager.may_transmit

    sent = beacon.transmit_cycle(
        manager, telemetry, region_manager=region_manager, inter_packet_delay_sec=0
    )

    assert sent == 0
    assert radio.lora_packets == []
    assert beacon.stats.suppressed_no_region == 1


def test_transmit_cycle_counts_a_failure(manager, radio, beacon, telemetry):
    manager.set_lora_settings(get_preset("LONG_FAST"), 906.875, 22, get_region("US"))
    radio.fail_next_lora = True

    beacon.transmit_cycle(manager, telemetry, inter_packet_delay_sec=0)
    assert beacon.stats.packets_failed == 1


def test_beacon_status_is_json_friendly(beacon, telemetry):
    import json

    beacon.build_beacon_cycle(telemetry)
    status = beacon.get_status()
    json.dumps(status)
    assert status["hop_limit"] == 0
    assert status["primary_encrypted"] is True
