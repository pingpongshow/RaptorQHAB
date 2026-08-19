"""
LoRa mode for the SX1262, alongside the existing GFSK image downlink.

The payload's RAPTOR link runs GFSK at 96 kbps because that is what makes
image downlink practical. Meshtastic requires LoRa. The SX1262 can do both but
only one at a time, so this module adds the LoRa half and `RadioModeManager`
serialises switching between them.

Implemented as a mixin on the existing SX1262 driver rather than a second
driver, so there is exactly one object owning the SPI bus and the GPIO lines.
Two drivers contending for the same chip select would be a very hard bug to
find in flight.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Meshtastic's LoRa sync word. Not the same thing as a GFSK sync word: on the
# SX1262 this is a single register pair that gates which network's packets the
# receiver will even look at.
MESHTASTIC_SYNC_WORD = 0x2B

PACKET_TYPE_GFSK = 0x00
PACKET_TYPE_LORA = 0x01

# SetModulationParams bandwidth codes (datasheet Table 13-47)
LORA_BW_CODES = {
    7.8: 0x00, 10.4: 0x08, 15.6: 0x01, 20.8: 0x09, 31.25: 0x02,
    41.7: 0x0A, 62.5: 0x03, 125.0: 0x04, 250.0: 0x05, 500.0: 0x06,
}

# CalibrateImage arguments per band (datasheet Table 9-2). Calibrating for the
# wrong band costs several dB of sensitivity, which matters a great deal when
# the region logic can move the balloon from 906 MHz to 869 or 433 MHz.
IMAGE_CAL_BANDS: Tuple[Tuple[float, float, int, int], ...] = (
    (430.0, 440.0, 0x6B, 0x6F),
    (470.0, 510.0, 0x75, 0x81),
    (779.0, 787.0, 0xC1, 0xC5),
    (863.0, 870.0, 0xD7, 0xDB),
    (902.0, 928.0, 0xE1, 0xE9),
)


class RadioMode(Enum):
    """Which modulation the radio is currently configured for."""

    UNCONFIGURED = "unconfigured"
    GFSK = "gfsk"
    LORA = "lora"


@dataclass(frozen=True)
class LoRaConfig:
    """LoRa modem settings."""

    spreading_factor: int = 11
    bandwidth_khz: float = 250.0
    coding_rate: int = 5           # 4/5 .. 4/8, expressed as 5..8
    preamble_length: int = 16
    sync_word: int = MESHTASTIC_SYNC_WORD
    explicit_header: bool = True
    crc_on: bool = True
    invert_iq: bool = False

    def __post_init__(self):
        if not 5 <= self.spreading_factor <= 12:
            raise ValueError(f"spreading factor must be 5-12, got {self.spreading_factor}")
        if self.bandwidth_khz not in LORA_BW_CODES:
            raise ValueError(
                f"bandwidth {self.bandwidth_khz} kHz is not an SX1262 option; "
                f"choose from {sorted(LORA_BW_CODES)}"
            )
        if not 5 <= self.coding_rate <= 8:
            raise ValueError(f"coding rate must be 5-8 (4/5..4/8), got {self.coding_rate}")
        if self.preamble_length < 1:
            raise ValueError("preamble length must be at least 1")

    @property
    def bandwidth_code(self) -> int:
        return LORA_BW_CODES[self.bandwidth_khz]

    @property
    def low_data_rate_optimize(self) -> bool:
        """
        Whether to enable LDRO.

        Required when a symbol lasts longer than about 16 ms, or crystal drift
        across a long symbol corrupts the packet. Meshtastic's own rule is
        symbol time >= 16 ms.
        """
        return self.symbol_time_ms >= 16.0

    @property
    def symbol_time_ms(self) -> float:
        return (2 ** self.spreading_factor) / self.bandwidth_khz

    def time_on_air_ms(self, payload_bytes: int) -> float:
        """
        Estimated time on air, per the SX1262 datasheet §6.1.4.

        Used to budget the transmit schedule and to honour regional duty-cycle
        limits, so it needs to be a real calculation rather than a guess.
        """
        sf = self.spreading_factor
        symbol_ms = self.symbol_time_ms
        preamble_ms = (self.preamble_length + 4.25) * symbol_ms

        de = 1 if self.low_data_rate_optimize else 0
        ih = 0 if self.explicit_header else 1
        crc = 1 if self.crc_on else 0

        numerator = 8 * payload_bytes - 4 * sf + 28 + 16 * crc - 20 * ih
        denominator = 4 * (sf - 2 * de)
        payload_symbols = 8 + max(
            0, -(-numerator // denominator) * (self.coding_rate - 4 + 4)
        )

        return preamble_ms + payload_symbols * symbol_ms


# Meshtastic's named modem presets (LoRaConfig in config.proto).
MESHTASTIC_PRESETS = {
    "LONG_FAST": LoRaConfig(spreading_factor=11, bandwidth_khz=250.0, coding_rate=5),
    "LONG_SLOW": LoRaConfig(spreading_factor=12, bandwidth_khz=125.0, coding_rate=8),
    "MEDIUM_SLOW": LoRaConfig(spreading_factor=10, bandwidth_khz=250.0, coding_rate=5),
    "MEDIUM_FAST": LoRaConfig(spreading_factor=9, bandwidth_khz=250.0, coding_rate=5),
    "SHORT_SLOW": LoRaConfig(spreading_factor=8, bandwidth_khz=250.0, coding_rate=5),
    "SHORT_FAST": LoRaConfig(spreading_factor=7, bandwidth_khz=250.0, coding_rate=5),
    "SHORT_TURBO": LoRaConfig(spreading_factor=7, bandwidth_khz=500.0, coding_rate=5),
    "VERY_LONG_SLOW": LoRaConfig(spreading_factor=12, bandwidth_khz=62.5, coding_rate=8),
}

DEFAULT_PRESET = "LONG_FAST"


def get_preset(name: str) -> LoRaConfig:
    """Look up a Meshtastic modem preset by name."""
    key = (name or DEFAULT_PRESET).upper()
    if key not in MESHTASTIC_PRESETS:
        raise ValueError(
            f"unknown modem preset {name!r}; choose from "
            f"{sorted(MESHTASTIC_PRESETS)}"
        )
    return MESHTASTIC_PRESETS[key]


def image_calibration_for(frequency_mhz: float) -> Tuple[int, int]:
    """
    CalibrateImage arguments for the band containing a frequency.

    Falls back to the widest sensible calibration rather than raising: a
    slightly mis-calibrated radio still transmits, whereas an exception during
    a region change would take the payload down.
    """
    for low, high, arg1, arg2 in IMAGE_CAL_BANDS:
        if low <= frequency_mhz <= high:
            return arg1, arg2

    logger.warning(
        f"{frequency_mhz} MHz is outside every documented image calibration "
        f"band; using the 902-928 MHz calibration"
    )
    return 0xE1, 0xE9


class LoRaModeMixin:
    """
    LoRa configuration and transfer for the SX1262 driver.

    Mixed into `SX1262`, so it uses that class's `_spi_command`,
    `_write_register`, `_wait_for_irq` and GPIO handling directly.
    """

    def configure_lora(
        self,
        lora_config: LoRaConfig,
        frequency_mhz: float,
        tx_power_dbm: int,
    ) -> None:
        """
        Switch the radio into LoRa mode and apply a full configuration.

        Order matters. SetPacketType must precede the modulation and packet
        parameters, because the SX1262 interprets those registers differently
        per packet type.
        """
        from common.radio import SX1262Cmd, SX1262Reg

        self.set_standby()

        self._spi_command(SX1262Cmd.SET_PACKET_TYPE, bytes([PACKET_TYPE_LORA]))

        self.set_frequency(frequency_mhz)
        self._calibrate_image_for(frequency_mhz)

        self._spi_command(SX1262Cmd.SET_PA_CONFIG, bytes([0x04, 0x07, 0x00, 0x01]))
        self.set_tx_power(tx_power_dbm)

        self._set_lora_modulation(lora_config)
        self._set_lora_packet_params(lora_config, payload_length=255)
        self._set_lora_sync_word(lora_config.sync_word)

        self._spi_command(SX1262Cmd.SET_BUFFER_BASE_ADDRESS, bytes([0x00, 0x00]))
        self._configure_irq()
        self._spi_command(SX1262Cmd.SET_DIO2_AS_RF_SWITCH_CTRL, bytes([0x01]))

        # RX boosted gain: worth the extra current for a receiver listening for
        # handheld nodes tens of kilometres below.
        self._write_register(SX1262Reg.REG_RX_GAIN, bytes([0x96]))

        self._lora_config = lora_config
        logger.info(
            f"LoRa configured: {frequency_mhz:.4f} MHz SF{lora_config.spreading_factor} "
            f"BW{lora_config.bandwidth_khz:g} CR4/{lora_config.coding_rate} "
            f"sync 0x{lora_config.sync_word:02X} at {tx_power_dbm} dBm"
        )

    def _calibrate_image_for(self, frequency_mhz: float) -> None:
        from common.radio import SX1262Cmd

        arg1, arg2 = image_calibration_for(frequency_mhz)
        self._spi_command(SX1262Cmd.CALIBRATE_IMAGE, bytes([arg1, arg2]))

    def _set_lora_modulation(self, cfg: LoRaConfig) -> None:
        """SetModulationParams for LoRa: SF, BW, CR, LDRO."""
        from common.radio import SX1262Cmd

        self._spi_command(
            SX1262Cmd.SET_MODULATION_PARAMS,
            bytes([
                cfg.spreading_factor,
                cfg.bandwidth_code,
                cfg.coding_rate - 4,               # register wants 1..4
                0x01 if cfg.low_data_rate_optimize else 0x00,
            ]),
        )

    def _set_lora_packet_params(self, cfg: LoRaConfig, payload_length: int) -> None:
        """SetPacketParams for LoRa."""
        from common.radio import SX1262Cmd

        self._spi_command(
            SX1262Cmd.SET_PACKET_PARAMS,
            bytes([
                (cfg.preamble_length >> 8) & 0xFF,
                cfg.preamble_length & 0xFF,
                0x00 if cfg.explicit_header else 0x01,
                payload_length & 0xFF,
                0x01 if cfg.crc_on else 0x00,
                0x01 if cfg.invert_iq else 0x00,
            ]),
        )

    def _set_lora_sync_word(self, sync_word: int) -> None:
        """
        Write the LoRa sync word.

        The SX1262 stores it as two nibbles spread across a register pair:
        0x0740 holds the high nibble in its upper half, 0x0741 the low nibble.
        Writing the raw byte to one register, which is the obvious-looking
        thing to do, silently puts the radio on a different network.
        """
        high = ((sync_word & 0xF0) >> 4) | 0x50
        low = ((sync_word & 0x0F) << 4) | 0x04
        self._write_register(0x0740, bytes([high]))
        self._write_register(0x0741, bytes([low]))

    def transmit_lora(self, data: bytes, timeout_ms: int = 10000) -> bool:
        """Transmit one LoRa packet. Blocks until done or timed out."""
        from common.radio import GPIO, SX1262Cmd, SX1262IRQ

        cfg = getattr(self, "_lora_config", None)
        if cfg is None:
            logger.error("transmit_lora called before configure_lora")
            return False

        if len(data) > 255:
            logger.error(f"LoRa packet too large: {len(data)} > 255")
            return False

        self.set_standby()
        self._clear_irq()
        self._write_buffer(0, data)
        self._set_lora_packet_params(cfg, payload_length=len(data))

        if GPIO and not self._simulation:
            GPIO.output(self.config.pin_txen, GPIO.HIGH)

        try:
            steps = int((timeout_ms * 1000) / 15.625) if timeout_ms > 0 else 0
            self._spi_command(
                SX1262Cmd.SET_TX,
                bytes([(steps >> 16) & 0xFF, (steps >> 8) & 0xFF, steps & 0xFF]),
            )
            success = self._wait_for_irq(SX1262IRQ.TX_DONE, timeout_ms + 100)
        finally:
            # Always drop TXEN. Leaving the PA keyed after a failed transmit
            # would both jam the band and cook the amplifier.
            if GPIO and not self._simulation:
                GPIO.output(self.config.pin_txen, GPIO.LOW)
            self.set_standby()

        if success:
            self._packet_count += 1
            logger.debug(
                f"LoRa TX complete: {len(data)} bytes, "
                f"~{cfg.time_on_air_ms(len(data)):.0f} ms on air"
            )
        else:
            logger.warning(f"LoRa TX failed or timed out ({len(data)} bytes)")

        return success

    def start_lora_receive(self, timeout_ms: int = 0) -> None:
        """
        Put the radio into LoRa receive.

        Non-blocking: call `poll_lora_receive` to collect anything that
        arrives. Splitting it this way lets the scheduler open a short receive
        window without a thread blocking inside the driver.
        """
        from common.radio import GPIO, SX1262Cmd

        cfg = getattr(self, "_lora_config", None)
        if cfg is None:
            raise RuntimeError("start_lora_receive called before configure_lora")

        self.set_standby()
        self._clear_irq()
        self._set_lora_packet_params(cfg, payload_length=255)

        if GPIO and not self._simulation:
            GPIO.output(self.config.pin_txen, GPIO.LOW)

        steps = int((timeout_ms * 1000) / 15.625) if timeout_ms > 0 else 0
        self._spi_command(
            SX1262Cmd.SET_RX,
            bytes([(steps >> 16) & 0xFF, (steps >> 8) & 0xFF, steps & 0xFF]),
        )

    def poll_lora_receive(self) -> Optional[Tuple[bytes, int, float]]:
        """
        Collect a received LoRa packet if one is waiting.

        Returns:
            (payload, rssi_dbm, snr_db), or None if nothing has arrived.
        """
        from common.radio import SX1262Cmd, SX1262IRQ

        irq = self._get_irq_status()

        if irq & SX1262IRQ.CRC_ERR:
            logger.debug("LoRa RX: CRC error, discarding")
            self._clear_irq()
            return None

        if not irq & SX1262IRQ.RX_DONE:
            return None

        self._clear_irq()

        status = self._get_rx_buffer_status()
        if not status:
            return None

        length, offset = status
        if length == 0:
            return None

        payload = self._read_buffer(offset, length)

        rssi, snr = self._get_lora_packet_status()
        self._last_rssi = rssi

        logger.debug(f"LoRa RX: {length} bytes, RSSI {rssi} dBm, SNR {snr:.1f} dB")
        return payload, rssi, snr

    def _get_lora_packet_status(self) -> Tuple[int, float]:
        """
        Read RSSI and SNR for the last LoRa packet.

        GetPacketStatus returns different fields per packet type; in LoRa mode
        it is rssiPkt, snrPkt, signalRssiPkt.
        """
        from common.radio import SX1262Cmd

        response = self._spi_command(SX1262Cmd.GET_PACKET_STATUS, b"", response_len=4)
        if len(response) < 4:
            return 0, 0.0

        rssi_pkt = response[1]
        snr_pkt = response[2]

        rssi = -rssi_pkt // 2
        snr = (snr_pkt - 256 if snr_pkt > 127 else snr_pkt) / 4.0
        return rssi, snr

    def restore_gfsk(self) -> None:
        """
        Return the radio to the GFSK configuration used by the image downlink.

        Re-runs the full GFSK setup rather than only flipping the packet type,
        because SetPacketType leaves the modulation and packet parameter
        registers holding LoRa values.
        """
        self._configure_fsk()
        self._lora_config = None
        logger.debug("Radio restored to GFSK mode")
