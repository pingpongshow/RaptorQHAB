"""
The Meshtastic path as wired into the payload controller.

These cover the seams between components -- region changes reaching the radio,
beacons yielding the radio back to the image downlink, and the refusals that
keep the balloon off the air when it should be.
"""

import pytest

from airborne.config import Config
from airborne.main import RaptorHabAirborne
from common.meshtastic.crypto import generate_psk
from common.radio_lora import RadioMode
from common.radio_manager import RadioModeManager


class FakeRadio:
    def __init__(self):
        self.lora_packets = []
        self.gfsk_packets = []
        self.configure_calls = []
        self.gfsk_restores = 0

    def configure_lora(self, lora_config, frequency_mhz, tx_power_dbm):
        self.configure_calls.append((frequency_mhz, tx_power_dbm))

    def restore_gfsk(self):
        self.gfsk_restores += 1

    def set_tx_power(self, power_dbm):
        pass

    def set_standby(self, rc=True):
        pass

    def transmit(self, packet, timeout_ms=5000):
        self.gfsk_packets.append(packet)
        return True

    def transmit_lora(self, packet, timeout_ms=10000):
        self.lora_packets.append(packet)
        return True


class FakeGPS:
    def __init__(self, latitude, longitude, altitude=30000.0, fix_type=2, satellites=11):
        self.latitude = latitude
        self.longitude = longitude
        self.altitude = altitude
        self.fix_type = fix_type
        self.satellites = satellites
        self.speed = 12.0
        self.heading = 271.0
        self.time_utc = 0


def make_controller(tmp_path, **overrides):
    config = Config()
    config.image_storage_path = str(tmp_path / "images")
    config.log_path = str(tmp_path / "logs")
    config.meshtastic_enabled = True
    config.meshtastic_beacon_interval_sec = 30
    config.meshtastic_region_dwell_sec = 0
    config.meshtastic_region_edge_margin_km = 0
    config.meshtastic_inter_packet_delay_ms = 0
    for key, value in overrides.items():
        setattr(config, key, value)

    controller = RaptorHabAirborne(config, debug=True)
    radio = FakeRadio()
    controller._radio = radio
    controller._radio_manager = RadioModeManager(
        radio, gfsk_tx_power_dbm=config.radio_power_dbm
    )
    controller._initialize_meshtastic()
    return controller, radio


# --- startup --------------------------------------------------------------


def test_meshtastic_starts_on_the_home_region(tmp_path):
    controller, radio = make_controller(tmp_path, meshtastic_region="US")

    assert controller._beacon is not None
    assert controller._region_manager.region.code == "US"

    settings = controller._radio_manager.lora_settings
    assert settings.frequency_mhz == pytest.approx(906.875, abs=0.0005)
    assert settings.tx_power_dbm == 22


def test_home_region_sets_the_matching_frequency(tmp_path):
    controller, _radio = make_controller(tmp_path, meshtastic_region="JP")
    settings = controller._radio_manager.lora_settings
    assert settings.frequency_mhz == pytest.approx(925.675, abs=0.0005)
    assert settings.tx_power_dbm == 13, "must clamp to the Japan ceiling"


def test_868_board_reaches_europe(tmp_path):
    """The same logic on the 868M board variant."""
    controller, _radio = make_controller(
        tmp_path, radio_hardware_band="868M", meshtastic_region="EU_868",
        radio_frequency_mhz=868.0,
    )
    settings = controller._radio_manager.lora_settings
    assert settings.frequency_mhz == pytest.approx(869.525, abs=0.0005)
    assert settings.tx_power_dbm == 14, "must clamp to the EU 868 ceiling"


def test_home_region_outside_the_hardware_band_refuses_to_transmit(tmp_path):
    """
    A 915M board configured for a 433 MHz region must not key the PA out of
    band; it should refuse and say so.
    """
    controller, radio = make_controller(tmp_path, meshtastic_region="EU_433")

    assert not controller._region_manager.may_transmit
    assert controller._radio_manager.lora_settings is None
    assert not controller._radio_manager.transmit_lora(b"\x00" * 20)


def test_disabled_meshtastic_creates_no_beacon(tmp_path):
    config = Config()
    config.meshtastic_enabled = False
    controller = RaptorHabAirborne(config, debug=True)
    assert controller._beacon is None


# --- key handling ---------------------------------------------------------


def test_private_channel_without_a_key_is_refused(tmp_path):
    """
    Transmitting an unencrypted channel labelled "private" would be worse than
    not having one, so it is disabled rather than silently sent in the clear.
    """
    controller, _radio = make_controller(
        tmp_path, meshtastic_private_enabled=True, meshtastic_private_psk=""
    )
    assert controller._beacon.private_channel is None


def test_private_channel_with_a_key_is_configured(tmp_path):
    key = generate_psk(32)
    controller, _radio = make_controller(
        tmp_path,
        meshtastic_private_enabled=True,
        meshtastic_private_psk=key.hex(),
        meshtastic_private_name="RaptorSecret",
    )

    private = controller._beacon.private_channel
    assert private is not None
    assert private.key == key
    assert private.is_encrypted


def test_malformed_private_key_disables_the_channel(tmp_path):
    controller, _radio = make_controller(
        tmp_path,
        meshtastic_private_enabled=True,
        meshtastic_private_psk="not a key at all",
    )
    assert controller._beacon.private_channel is None


def test_malformed_primary_key_falls_back_to_the_default(tmp_path):
    """The public channel should keep working rather than take the payload down."""
    controller, _radio = make_controller(
        tmp_path, meshtastic_channel_psk="!!! not base64 !!!"
    )
    assert controller._beacon.primary_channel.is_encrypted


# --- region changes reaching the radio ------------------------------------


def test_crossing_into_a_new_region_retunes_the_radio(tmp_path):
    controller, radio = make_controller(tmp_path, meshtastic_region="US")

    with controller._gps_lock:
        controller._current_gps = FakeGPS(35.68, 139.69)  # Tokyo

    controller._region_manager.update(35.68, 139.69, fix_type=2, now=1000.0)
    controller._region_manager.update(35.68, 139.69, fix_type=2, now=1001.0)
    controller._apply_region_to_radio()

    settings = controller._radio_manager.lora_settings
    assert settings.region_code == "JP"
    assert settings.frequency_mhz == pytest.approx(925.675, abs=0.0005)
    assert settings.tx_power_dbm == 13


def test_crossing_into_an_unreachable_region_suspends_transmission(tmp_path):
    """
    Europe on a 915M board. The balloon must go quiet on Meshtastic rather
    than transmit at 869 MHz through a 900 MHz matching network -- and the
    image downlink must keep running.
    """
    controller, radio = make_controller(tmp_path, meshtastic_region="US")

    with controller._gps_lock:
        controller._current_gps = FakeGPS(52.52, 13.40)  # Berlin

    controller._region_manager.update(52.52, 13.40, fix_type=2, now=1000.0)
    controller._apply_region_to_radio()

    assert controller._radio_manager.lora_settings is None
    assert not controller._radio_manager.transmit_lora(b"\x00" * 20)
    assert radio.lora_packets == []

    assert controller._transmit_packet(b"\xAA" * 40)
    assert len(radio.gfsk_packets) == 1


def test_unknown_territory_stops_meshtastic_transmission(tmp_path):
    controller, radio = make_controller(tmp_path, meshtastic_region="US")

    controller._region_manager.update(0.0, -140.0, fix_type=2, now=1000.0)
    controller._apply_region_to_radio()

    assert controller._radio_manager.lora_settings is None
    assert not controller._radio_manager.transmit_lora(b"\x00" * 20)
    assert radio.lora_packets == []


def test_unknown_territory_leaves_the_image_downlink_running(tmp_path):
    """Losing the band plan must not cost the image link."""
    controller, radio = make_controller(tmp_path, meshtastic_region="US")

    controller._region_manager.update(0.0, -140.0, fix_type=2, now=1000.0)
    controller._apply_region_to_radio()

    assert controller._transmit_packet(b"\xAA" * 40)
    assert len(radio.gfsk_packets) == 1


def test_re_entering_known_territory_restores_transmission(tmp_path):
    controller, radio = make_controller(tmp_path, meshtastic_region="US")

    controller._region_manager.update(0.0, -140.0, fix_type=2, now=1000.0)
    controller._apply_region_to_radio()
    assert controller._radio_manager.lora_settings is None

    controller._region_manager.update(39.74, -104.99, fix_type=2, now=1100.0)
    controller._apply_region_to_radio()

    assert controller._radio_manager.lora_settings is not None
    assert controller._radio_manager.transmit_lora(b"\x00" * 20)


def test_gps_loss_holds_the_region_and_keeps_transmitting(tmp_path):
    """Q1: hold last known mode."""
    controller, radio = make_controller(tmp_path, meshtastic_region="US")

    controller._region_manager.update(35.68, 139.69, fix_type=2, now=1000.0)
    controller._region_manager.update(35.68, 139.69, fix_type=2, now=1001.0)
    controller._apply_region_to_radio()
    assert controller._radio_manager.lora_settings.region_code == "JP"

    controller._region_manager.update(fix_type=0, now=1100.0)
    controller._apply_region_to_radio()

    assert controller._radio_manager.lora_settings.region_code == "JP"
    assert controller._radio_manager.transmit_lora(b"\x00" * 20)


# --- beacon cycles --------------------------------------------------------


def test_beacon_runs_when_due_and_yields_the_radio_back(tmp_path):
    controller, radio = make_controller(tmp_path)

    with controller._gps_lock:
        controller._current_gps = FakeGPS(39.7392, -104.9903)

    controller._last_beacon_time = 0.0
    controller._run_beacon_if_due()

    # Position + device telemetry + NodeInfo. No text packet, because no
    # beacon_text is configured, and no environment packet, because a
    # developer machine reports no CPU temperature.
    assert len(radio.lora_packets) == 3
    assert controller._radio_manager.mode is RadioMode.GFSK, (
        "the radio must be handed back to the image downlink"
    )


def test_beacon_does_not_run_before_the_interval(tmp_path):
    import time

    controller, radio = make_controller(tmp_path)
    controller._last_beacon_time = time.time()
    controller._run_beacon_if_due()
    assert radio.lora_packets == []


def test_beacon_yields_the_radio_back_even_when_it_throws(tmp_path):
    """A failure mid-beacon must not strand the radio in LoRa mode."""
    controller, radio = make_controller(tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("beacon exploded")

    controller._beacon.transmit_cycle = explode
    controller._last_beacon_time = 0.0
    controller._run_beacon_if_due()

    assert controller._radio_manager.mode is RadioMode.GFSK


def test_beacon_is_skipped_over_unknown_territory(tmp_path):
    controller, radio = make_controller(tmp_path)

    with controller._gps_lock:
        controller._current_gps = FakeGPS(0.0, -140.0)

    controller._last_beacon_time = 0.0
    controller._run_beacon_if_due()

    assert radio.lora_packets == []


def test_beacon_telemetry_reflects_gps_and_system_state(tmp_path):
    controller, _radio = make_controller(tmp_path)

    with controller._gps_lock:
        controller._current_gps = FakeGPS(39.7392, -104.9903, altitude=30480.0)

    telemetry = controller._collect_beacon_telemetry()

    assert telemetry.latitude == pytest.approx(39.7392)
    assert telemetry.altitude_m == pytest.approx(30480.0)
    assert telemetry.satellites == 11
    assert telemetry.has_position
    assert 0 <= telemetry.battery_percent <= 100


def test_beacon_telemetry_without_a_fix_has_no_position(tmp_path):
    controller, _radio = make_controller(tmp_path)
    telemetry = controller._collect_beacon_telemetry()
    assert not telemetry.has_position


def test_battery_percentage_maps_the_lithium_range(tmp_path):
    controller, _radio = make_controller(tmp_path)

    import airborne.main as main_module

    for millivolts, expected in ((4200, 100), (3750, 50), (3300, 0), (3000, 0)):
        original = main_module.get_battery_voltage
        main_module.get_battery_voltage = lambda mv=millivolts: mv
        try:
            telemetry = controller._collect_beacon_telemetry()
        finally:
            main_module.get_battery_voltage = original
        assert telemetry.battery_percent == pytest.approx(expected, abs=1)


# --- the two links coexisting ---------------------------------------------


def test_image_and_beacon_traffic_interleave_without_losing_either(tmp_path):
    controller, radio = make_controller(tmp_path)

    with controller._gps_lock:
        controller._current_gps = FakeGPS(39.7392, -104.9903)

    for _ in range(3):
        for _ in range(10):
            controller._transmit_packet(b"\xAA" * 40)
        controller._last_beacon_time = 0.0
        controller._run_beacon_if_due()

    assert len(radio.gfsk_packets) == 30
    # Three cycles of position + telemetry, plus NodeInfo on the first only
    # (nodeinfo_every defaults to 6).
    assert len(radio.lora_packets) == 7
    assert controller._radio_manager.mode is RadioMode.GFSK

    stats = controller._radio_manager.get_stats()
    assert stats["to_lora"]["switches"] == 3
    assert stats["to_gfsk"]["switches"] == 3
