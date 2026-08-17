"""
Single-owner arbitration of the SX1262 between GFSK and LoRa.

The chip cannot be in both modes at once, so every mode change has to be
serialised and every caller has to go through here. Two things this guarantees
that ad-hoc calls into the driver would not:

  - A mode switch never interrupts a packet that is mid-transmission.
  - Transmit power is clamped to the active region's ceiling on every switch,
    so moving the Meshtastic frequency across a border can never leave the
    radio transmitting above what that jurisdiction permits.

Mode switch cost is measured rather than assumed; call `get_stats()` to see
the real numbers from the bench.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from common.meshtastic.regions import Region, clamp_power_to_region
from common.radio_lora import LoRaConfig, RadioMode

logger = logging.getLogger(__name__)


@dataclass
class ModeSwitchStats:
    """Measured cost of switching modes."""

    switches: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    last_ms: float = 0.0

    def record(self, elapsed_ms: float) -> None:
        self.switches += 1
        self.total_ms += elapsed_ms
        self.last_ms = elapsed_ms
        self.max_ms = max(self.max_ms, elapsed_ms)

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.switches if self.switches else 0.0


@dataclass
class LoRaSettings:
    """Everything needed to put the radio on air for Meshtastic."""

    config: LoRaConfig
    frequency_mhz: float
    tx_power_dbm: int
    region_code: str = ""

    def differs_from(self, other: Optional["LoRaSettings"]) -> bool:
        if other is None:
            return True
        return (
            self.config != other.config
            or abs(self.frequency_mhz - other.frequency_mhz) > 1e-6
            or self.tx_power_dbm != other.tx_power_dbm
        )


class RadioModeManager:
    """
    Owns the SX1262 and arbitrates GFSK versus LoRa.

    Thread-safe: the transmit loop, the beacon scheduler, and any receive
    window all funnel through the same lock, so a mode switch can never land
    in the middle of someone else's packet.
    """

    def __init__(self, radio, gfsk_tx_power_dbm: int):
        """
        Args:
            radio: An initialised SX1262 instance.
            gfsk_tx_power_dbm: Power to restore when returning to image mode.
        """
        self._radio = radio
        self._gfsk_tx_power_dbm = gfsk_tx_power_dbm

        self._mode = RadioMode.GFSK
        self._lock = threading.RLock()
        self._lora_settings: Optional[LoRaSettings] = None
        self._applied_lora: Optional[LoRaSettings] = None

        self._stats: Dict[str, ModeSwitchStats] = {
            "to_lora": ModeSwitchStats(),
            "to_gfsk": ModeSwitchStats(),
        }

    @property
    def mode(self) -> RadioMode:
        with self._lock:
            return self._mode

    @property
    def lora_settings(self) -> Optional[LoRaSettings]:
        with self._lock:
            return self._lora_settings

    # -- configuration -----------------------------------------------------

    def set_lora_settings(
        self,
        config: LoRaConfig,
        frequency_mhz: float,
        requested_power_dbm: int,
        region: Optional[Region] = None,
    ) -> LoRaSettings:
        """
        Set the LoRa parameters to use on the next switch into LoRa mode.

        Power is clamped to the region ceiling here rather than at transmit
        time, so there is no window in which a stale higher power could be
        applied after a region change.

        If the radio is already in LoRa mode the new settings are applied
        immediately.
        """
        power = requested_power_dbm
        region_code = ""
        if region is not None:
            power = clamp_power_to_region(requested_power_dbm, region)
            region_code = region.code

        settings = LoRaSettings(
            config=config,
            frequency_mhz=frequency_mhz,
            tx_power_dbm=power,
            region_code=region_code,
        )

        with self._lock:
            changed = settings.differs_from(self._lora_settings)
            self._lora_settings = settings

            if changed and self._mode is RadioMode.LORA:
                logger.info(
                    f"LoRa settings changed while on air; reapplying "
                    f"({region_code or 'no region'} {frequency_mhz:.4f} MHz "
                    f"{power} dBm)"
                )
                self._apply_lora_settings(settings)

        return settings

    def clear_lora_settings(self) -> None:
        """
        Forget the LoRa configuration, so LoRa transmission is refused.

        This is how "the balloon is over territory with no known band plan"
        is expressed: there is no correct frequency, so there must be no way
        to transmit. If the radio is currently in LoRa mode it is returned to
        GFSK, which keeps the image downlink running.
        """
        with self._lock:
            if self._lora_settings is None:
                return

            self._lora_settings = None
            logger.info("LoRa settings cleared; Meshtastic transmission disabled")

            if self._mode is RadioMode.LORA:
                self.ensure_gfsk()

    # -- mode switching ----------------------------------------------------

    def ensure_lora(self) -> bool:
        """
        Put the radio into LoRa mode, if it is not already.

        Returns False when no LoRa settings have been supplied -- which is the
        correct state when the balloon is over a region it has no band plan
        for, and must stay off the Meshtastic air entirely.
        """
        with self._lock:
            if self._lora_settings is None:
                logger.debug("No LoRa settings configured; staying in GFSK")
                return False

            if self._mode is RadioMode.LORA:
                if self._lora_settings.differs_from(self._applied_lora):
                    self._apply_lora_settings(self._lora_settings)
                return True

            start = time.monotonic()
            self._apply_lora_settings(self._lora_settings)
            self._mode = RadioMode.LORA
            elapsed_ms = (time.monotonic() - start) * 1000.0
            self._stats["to_lora"].record(elapsed_ms)

            logger.debug(f"Switched GFSK -> LoRa in {elapsed_ms:.1f} ms")
            return True

    def ensure_gfsk(self) -> bool:
        """Return the radio to the GFSK image downlink configuration."""
        with self._lock:
            if self._mode is RadioMode.GFSK:
                return True

            start = time.monotonic()
            self._radio.restore_gfsk()
            self._radio.set_tx_power(self._gfsk_tx_power_dbm)
            self._mode = RadioMode.GFSK
            self._applied_lora = None
            elapsed_ms = (time.monotonic() - start) * 1000.0
            self._stats["to_gfsk"].record(elapsed_ms)

            logger.debug(f"Switched LoRa -> GFSK in {elapsed_ms:.1f} ms")
            return True

    def _apply_lora_settings(self, settings: LoRaSettings) -> None:
        """Caller must hold the lock."""
        self._radio.configure_lora(
            lora_config=settings.config,
            frequency_mhz=settings.frequency_mhz,
            tx_power_dbm=settings.tx_power_dbm,
        )
        self._applied_lora = settings

    # -- transmit ----------------------------------------------------------

    def transmit_gfsk(self, packet: bytes, timeout_ms: int = 5000) -> bool:
        """Transmit a RAPTOR packet, switching to GFSK first if needed."""
        with self._lock:
            self.ensure_gfsk()
            return self._radio.transmit(packet, timeout_ms=timeout_ms)

    def transmit_lora(self, packet: bytes, timeout_ms: int = 10000) -> bool:
        """
        Transmit a Meshtastic packet, switching to LoRa first if needed.

        Returns False without transmitting when no LoRa settings are set --
        the "unknown region, stay off the air" case.
        """
        with self._lock:
            if not self.ensure_lora():
                return False
            return self._radio.transmit_lora(packet, timeout_ms=timeout_ms)

    def receive_lora_window(
        self, duration_sec: float, poll_interval_sec: float = 0.05
    ) -> list:
        """
        Listen for LoRa packets for a fixed period.

        Returns:
            A list of (payload, rssi, snr) tuples, possibly empty.

        Holding the lock for the whole window is deliberate: the transmit loop
        must not retune the radio out from under an open receive window.
        """
        received = []

        with self._lock:
            if not self.ensure_lora():
                return received

            self._radio.start_lora_receive(timeout_ms=0)
            deadline = time.monotonic() + duration_sec

            while time.monotonic() < deadline:
                result = self._radio.poll_lora_receive()
                if result is not None:
                    received.append(result)
                    # Re-arm for the rest of the window.
                    self._radio.start_lora_receive(timeout_ms=0)
                else:
                    time.sleep(poll_interval_sec)

            self._radio.set_standby()

        return received

    # -- introspection -----------------------------------------------------

    def get_stats(self) -> Dict[str, Dict[str, float]]:
        """Measured mode-switch costs, for the bench and for telemetry."""
        with self._lock:
            return {
                name: {
                    "switches": stats.switches,
                    "mean_ms": round(stats.mean_ms, 2),
                    "max_ms": round(stats.max_ms, 2),
                    "last_ms": round(stats.last_ms, 2),
                }
                for name, stats in self._stats.items()
            }

    def get_status(self) -> Dict[str, object]:
        with self._lock:
            settings = self._lora_settings
            return {
                "mode": self._mode.value,
                "lora_ready": settings is not None,
                "lora_region": settings.region_code if settings else None,
                "lora_frequency_mhz": settings.frequency_mhz if settings else None,
                "lora_power_dbm": settings.tx_power_dbm if settings else None,
                "gfsk_power_dbm": self._gfsk_tx_power_dbm,
            }
