"""
Returning from a Meshtastic beacon must put the downlink back on its own
frequency.

This is a regression test for a bug that cost the entire image downlink in
flight. set_frequency() used to assign to config.frequency_mhz, so tuning to a
Meshtastic channel redefined what "home" meant. restore_gfsk() then re-tuned to
the Meshtastic frequency, and the payload spent the rest of the flight
transmitting imagery on a frequency the ground station was not listening to --
while reporting every packet as sent successfully.
"""

import pytest

from common.radio import SX1262Radio
from common.radio_lora import LoRaConfig
from common.radio_manager import RadioModeManager


GFSK_HOME_MHZ = 915.0
MESHTASTIC_MHZ = 906.875


@pytest.fixture
def radio():
    r = SX1262Radio(frequency_mhz=GFSK_HOME_MHZ, tx_power_dbm=22, simulation=True)
    r.init()
    return r


def test_home_frequency_survives_a_lora_excursion(radio):
    radio.configure_lora(
        lora_config=LoRaConfig(),
        frequency_mhz=MESHTASTIC_MHZ,
        tx_power_dbm=14,
    )

    assert radio.config.frequency_mhz == GFSK_HOME_MHZ, (
        "tuning away for a beacon must not redefine the downlink frequency"
    )
    assert radio.current_frequency_mhz == MESHTASTIC_MHZ

    radio.restore_gfsk()

    assert radio.current_frequency_mhz == GFSK_HOME_MHZ, (
        "restore_gfsk must return the radio to the ground station's frequency"
    )


def test_repeated_beacons_do_not_drift_the_downlink(radio):
    """A real flight beacons hundreds of times; the error must not accumulate."""
    for _ in range(25):
        radio.configure_lora(
            lora_config=LoRaConfig(),
            frequency_mhz=MESHTASTIC_MHZ,
            tx_power_dbm=14,
        )
        radio.restore_gfsk()

    assert radio.config.frequency_mhz == GFSK_HOME_MHZ
    assert radio.current_frequency_mhz == GFSK_HOME_MHZ


def test_manager_restores_home_frequency(radio):
    """The same guarantee through the manager the payload actually uses."""
    manager = RadioModeManager(radio, gfsk_tx_power_dbm=22)
    manager.set_lora_settings(
        config=LoRaConfig(),
        frequency_mhz=MESHTASTIC_MHZ,
        requested_power_dbm=14,
    )
    manager.ensure_lora()
    assert radio.current_frequency_mhz == MESHTASTIC_MHZ

    manager.ensure_gfsk()
    assert radio.current_frequency_mhz == GFSK_HOME_MHZ


def test_transmit_preamble_exceeds_receiver_detector(radio):
    """
    The ground station's preamble detector is sized from its own configured
    preamble. Transmitting exactly that many bits leaves nothing for AGC
    settling -- measured as zero packets detected out of twenty.
    """
    assert radio.config.preamble_length >= 64, (
        "transmit preamble must leave margin above the receiver's detector"
    )


def test_rx_bandwidth_covers_the_modulation(radio):
    """Carson's rule: the filter must pass the signal it is trying to hear."""
    carson_hz = 2 * (radio.config.fdev_hz + radio.config.bitrate_bps / 2)
    assert radio.config.rx_bandwidth_hz >= carson_hz, "bandwidth truncates the signal"
    assert radio.config.rx_bandwidth_hz <= carson_hz * 1.5, (
        "excess bandwidth admits noise the signal does not occupy"
    )
