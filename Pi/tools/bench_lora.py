#!/usr/bin/env python3
"""
Bench validation for the SX1262 LoRa path on the Waveshare HAT.

Answers the question that gates the repeater and uplink work: can this board
actually receive? Nothing in the flight software has ever called
`radio.receive()`, and the Waveshare HAT drives both DIO2-as-RF-switch and a
separate TXEN GPIO. That combination is fine in theory and worth proving
before anything depends on it.

Run against a second Meshtastic node, or a second Pi running this same tool.

    # Listen for real Meshtastic traffic (US LongFast by default)
    sudo python3 tools/bench_lora.py rx

    # Transmit beacons a handheld should decode and display
    sudo python3 tools/bench_lora.py tx --count 10

    # Time GFSK <-> LoRa mode switching
    sudo python3 tools/bench_lora.py switch --count 50

    # Full loopback between two boards running this tool
    sudo python3 tools/bench_lora.py rx --region EU_868
    sudo python3 tools/bench_lora.py tx --region EU_868

Run as root, or with SPI and GPIO access.
"""

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from airborne.config import Config
from airborne.meshtastic_beacon import BeaconTelemetry, ChannelConfig, MeshtasticBeacon
from common.meshtastic import (
    PortNum,
    frequency_for_channel,
    get_region,
    node_id_to_string,
    parse_data,
    parse_packet,
    parse_position,
    parse_user,
)
from common.meshtastic.crypto import expand_psk, parse_psk
from common.meshtastic.messages import parse_text_message
from common.radio import SX1262
from common.radio_lora import get_preset
from common.radio_manager import RadioModeManager

logger = logging.getLogger("bench")

_stop = False


def _handle_sigint(signum, frame):
    global _stop
    _stop = True
    print("\nStopping...")


def build_radio(config: Config, simulate: bool) -> SX1262:
    radio = SX1262(
        frequency_mhz=config.radio_frequency_mhz,
        tx_power_dbm=config.radio_power_dbm,
        bitrate_bps=config.radio_bitrate_bps,
        fdev_hz=config.radio_fdev_hz,
        pin_cs=config.pin_cs,
        pin_busy=config.pin_busy,
        pin_dio1=config.pin_dio1,
        pin_reset=config.pin_rst,
        pin_txen=config.pin_txen,
        simulation=simulate,
    )
    if not radio.init():
        print("ERROR: radio failed to initialise. Check SPI wiring and that "
              "gpio=9=a0, gpio=10=a0, gpio=11=a0 are set in config.txt.")
        sys.exit(1)
    return radio


def resolve_frequency(args) -> tuple:
    region = get_region(args.region)
    if region is None:
        print(f"ERROR: unknown region {args.region!r}")
        sys.exit(2)

    frequency = args.frequency or frequency_for_channel(
        region, args.channel, int(get_preset(args.preset).bandwidth_khz)
    )
    return region, frequency


# --------------------------------------------------------------------------
# Receive
# --------------------------------------------------------------------------


def command_rx(args, config: Config) -> int:
    """Listen for LoRa packets and decode anything on the configured channel."""
    region, frequency = resolve_frequency(args)
    preset = get_preset(args.preset)
    key = expand_psk(parse_psk(args.psk))

    radio = build_radio(config, args.simulate)
    manager = RadioModeManager(radio, gfsk_tx_power_dbm=config.radio_power_dbm)
    manager.set_lora_settings(preset, frequency, args.power, region)

    print(f"Listening: {region.code} {frequency:.4f} MHz "
          f"SF{preset.spreading_factor} BW{preset.bandwidth_khz:g} "
          f"CR4/{preset.coding_rate} sync 0x{preset.sync_word:02X}")
    print(f"Channel {args.channel!r}, "
          f"{'encrypted' if key else 'plaintext'}. Ctrl-C to stop.\n")

    if not manager.ensure_lora():
        print("ERROR: could not enter LoRa mode")
        return 1

    radio.start_lora_receive(timeout_ms=0)

    received = 0
    decoded = 0
    started = time.time()

    while not _stop and (args.duration <= 0 or time.time() - started < args.duration):
        result = radio.poll_lora_receive()
        if result is None:
            time.sleep(0.02)
            continue

        payload, rssi, snr = result
        received += 1
        stamp = time.strftime("%H:%M:%S")

        try:
            packet = parse_packet(payload, channel_key=key)
        except Exception as e:
            print(f"[{stamp}] {len(payload):3d}B  RSSI {rssi:4d}  SNR {snr:5.1f}  "
                  f"unparseable header: {e}")
            radio.start_lora_receive(timeout_ms=0)
            continue

        header = packet.header
        line = (
            f"[{stamp}] {len(payload):3d}B  RSSI {rssi:4d}  SNR {snr:5.1f}  "
            f"from {node_id_to_string(header.sender)} "
            f"ch 0x{header.channel_hash:02X} hop {header.hop_limit}/{header.hop_start}"
        )

        detail = _describe(packet.payload)
        if detail:
            decoded += 1
            line += f"\n           {detail}"
        print(line)

        radio.start_lora_receive(timeout_ms=0)

    elapsed = time.time() - started
    print(f"\n{received} packets in {elapsed:.0f}s, {decoded} decoded on this channel")

    if received == 0:
        print(
            "\nNothing heard. Before concluding the receive path is broken, check:\n"
            "  - the transmitter is on the same frequency, SF, BW, CR and sync word\n"
            "  - an antenna is attached to BOTH boards (transmitting into no\n"
            "    antenna can damage the PA)\n"
            "  - the region matches what local nodes actually use\n"
            "  - DIO1 is wired and matches pin_dio1 in the config"
        )
        return 1

    print("\nRECEIVE PATH CONFIRMED WORKING on this board.")
    return 0


def _describe(plaintext: bytes) -> str:
    """Best-effort decode of a decrypted payload, for human reading."""
    if not plaintext:
        return ""

    try:
        data = parse_data(plaintext)
    except Exception:
        return "(not decodable with this key)"

    try:
        name = PortNum(data.portnum).name
    except ValueError:
        name = f"port {data.portnum}"

    try:
        if data.portnum == PortNum.POSITION_APP:
            position = parse_position(data.payload)
            return (
                f"{name}: {position.latitude:.5f},{position.longitude:.5f} "
                f"{position.altitude_m}m sats={position.satellites}"
            )
        if data.portnum == PortNum.NODEINFO_APP:
            user = parse_user(data.payload)
            return f"{name}: {user.long_name!r} [{user.short_name}] {user.node_id}"
        if data.portnum == PortNum.TEXT_MESSAGE_APP:
            return f"{name}: {parse_text_message(data.payload)!r}"
    except Exception as e:
        return f"{name}: payload did not parse ({e})"

    return f"{name}: {len(data.payload)} bytes"


# --------------------------------------------------------------------------
# Transmit
# --------------------------------------------------------------------------


def command_tx(args, config: Config) -> int:
    """Transmit beacons a stock Meshtastic client should show on its map."""
    region, frequency = resolve_frequency(args)
    preset = get_preset(args.preset)

    radio = build_radio(config, args.simulate)
    manager = RadioModeManager(radio, gfsk_tx_power_dbm=config.radio_power_dbm)
    settings = manager.set_lora_settings(preset, frequency, args.power, region)

    beacon = MeshtasticBeacon(
        callsign=config.callsign,
        payload_id=config.payload_id,
        primary_channel=ChannelConfig(name=args.channel, psk=parse_psk(args.psk)),
        beacon_text=args.text,
        hop_limit=args.hop_limit,
        nodeinfo_every=1,
    )

    print(f"Transmitting: {region.code} {frequency:.4f} MHz "
          f"SF{preset.spreading_factor} BW{preset.bandwidth_khz:g} "
          f"at {settings.tx_power_dbm} dBm")
    if settings.tx_power_dbm != args.power:
        print(f"  (requested {args.power} dBm, clamped to the {region.code} "
              f"limit of {region.power_limit_dbm} dBm)")
    print(f"Identity: {node_id_to_string(beacon.node_id)} "
          f"\"{beacon.long_name}\" [{beacon.short_name}]")
    print(f"Channel {args.channel!r} hash 0x{beacon.primary_channel.hash:02X}, "
          f"hop_limit {args.hop_limit}\n")

    telemetry = BeaconTelemetry(
        latitude=args.lat,
        longitude=args.lon,
        altitude_m=args.alt,
        satellites=9,
        fix_type=2,
        battery_mv=4100,
        battery_percent=85,
        cpu_temp_c=21.0,
        uptime_sec=int(time.time()) % 100000,
    )

    total = 0
    for index in range(args.count):
        if _stop:
            break
        telemetry.uptime_sec += args.interval
        sent = beacon.transmit_cycle(manager, telemetry, inter_packet_delay_sec=0.3)
        total += sent
        print(f"  cycle {index + 1}/{args.count}: {sent} packets")

        if index < args.count - 1:
            for _ in range(int(args.interval * 10)):
                if _stop:
                    break
                time.sleep(0.1)

    print(f"\n{total} packets transmitted. Check the receiving node for "
          f"{node_id_to_string(beacon.node_id)}.")
    return 0 if total else 1


# --------------------------------------------------------------------------
# Mode switching
# --------------------------------------------------------------------------


def command_switch(args, config: Config) -> int:
    """
    Measure GFSK <-> LoRa switch latency.

    This number sets how finely the Phase 4 scheduler can interleave image
    downlink with Meshtastic beacons, so it is worth having a real measurement
    rather than an estimate.
    """
    region, frequency = resolve_frequency(args)

    radio = build_radio(config, args.simulate)
    manager = RadioModeManager(radio, gfsk_tx_power_dbm=config.radio_power_dbm)
    manager.set_lora_settings(get_preset(args.preset), frequency, args.power, region)

    print(f"Timing {args.count} GFSK <-> LoRa round trips...\n")

    for index in range(args.count):
        if _stop:
            break
        manager.ensure_lora()
        manager.ensure_gfsk()
        if (index + 1) % 10 == 0:
            print(f"  {index + 1}/{args.count}")

    stats = manager.get_stats()
    print("\nMode switch timing:")
    for direction in ("to_lora", "to_gfsk"):
        entry = stats[direction]
        print(f"  {direction:8}  n={entry['switches']:4d}  "
              f"mean {entry['mean_ms']:6.2f} ms  max {entry['max_ms']:6.2f} ms")

    round_trip = stats["to_lora"]["mean_ms"] + stats["to_gfsk"]["mean_ms"]
    print(f"\n  round trip: {round_trip:.2f} ms mean")
    print(f"  At a 2s TX cycle that is {round_trip / 20:.2f}% of airtime lost "
          f"to switching.")
    return 0


# --------------------------------------------------------------------------


def main() -> int:
    signal.signal(signal.SIGINT, _handle_sigint)

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["rx", "tx", "switch"])
    parser.add_argument("--region", default="US", help="Meshtastic region code")
    parser.add_argument("--preset", default="LONG_FAST", help="Modem preset")
    parser.add_argument("--channel", default="LongFast", help="Channel name")
    parser.add_argument("--psk", default="AQ==", help="Channel key, base64 or hex")
    parser.add_argument("--frequency", type=float, default=None,
                        help="Override the derived frequency, in MHz")
    parser.add_argument("--power", type=int, default=17,
                        help="Requested TX power in dBm; clamped to the region limit")
    parser.add_argument("--hop-limit", type=int, default=0)
    parser.add_argument("--count", type=int, default=5,
                        help="Beacon cycles (tx) or switch iterations (switch)")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="Seconds between tx cycles")
    parser.add_argument("--duration", type=float, default=0,
                        help="Seconds to listen in rx mode; 0 means until Ctrl-C")
    parser.add_argument("--text", default="RaptorHAB bench test")
    parser.add_argument("--lat", type=float, default=39.7392)
    parser.add_argument("--lon", type=float, default=-104.9903)
    parser.add_argument("--alt", type=float, default=1609.0)
    parser.add_argument("--simulate", action="store_true",
                        help="Run without hardware, to check the tool itself")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = Config.load()

    handlers = {"rx": command_rx, "tx": command_tx, "switch": command_switch}
    return handlers[args.command](args, config)


if __name__ == "__main__":
    sys.exit(main())
