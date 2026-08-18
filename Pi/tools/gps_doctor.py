#!/usr/bin/env python3
"""
Diagnose a silent GPS on the Waveshare SX1262 LoRaWAN/GNSS HAT.

The L76K emits NMEA continuously from power-on, with or without a fix and
with or without an antenna. Silence therefore means it is not running, not
that it cannot see the sky -- which is the single most useful thing to know
before going looking for the problem.

    sudo python3 tools/gps_doctor.py
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Confirmed against the Waveshare pinout: the L76K's TXD goes to header pin
# 10, which is BCM 15, the Pi's RX. Nothing else on the board carries NMEA.
UART_CANDIDATES = ["/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0"]
BAUD_CANDIDATES = [9600, 38400, 115200, 4800, 57600]


def listen(device: str, baud: int, seconds: float = 3.0) -> bytes:
    try:
        import serial
        port = serial.Serial(device, baud, timeout=0.4)
        port.reset_input_buffer()
        end = time.time() + seconds
        data = b""
        while time.time() < end:
            data += port.read(1024)
        port.close()
        return data
    except Exception:
        return b""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=3.0)
    args = parser.parse_args()

    print("\nRaptorHab GPS doctor\n" + "-" * 60)

    # 1. Is the UART even free and pointing at the right hardware?
    print("\nUART")
    for device in UART_CANDIDATES:
        if os.path.exists(device):
            target = os.path.realpath(device)
            print(f"  {device:16} -> {target}")
        else:
            print(f"  {device:16} missing")

    if os.path.realpath("/dev/serial0").endswith("ttyS0"):
        print("  WARNING: serial0 points at the mini-UART, which is unreliable.")
        print("           Add dtoverlay=disable-bt and reboot.")

    # 2. Is anything else holding the port?
    try:
        holders = subprocess.run(["fuser", "-v", "/dev/ttyAMA0"],
                                 capture_output=True, text=True, timeout=5)
        if holders.stderr.strip().count("\n") > 0:
            print("  NOTE: another process has the port open:")
            print("        " + holders.stderr.strip().replace("\n", "\n        "))
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    # 3. Listen.
    print("\nListening for NMEA")
    found = False
    for device in UART_CANDIDATES:
        if not os.path.exists(device):
            continue
        for baud in BAUD_CANDIDATES:
            data = listen(device, baud, args.seconds)
            if not data:
                continue
            nmea = b"$G" in data or b"$B" in data
            print(f"  {device} @{baud}: {len(data)} bytes"
                  f"{'  NMEA' if nmea else '  (not NMEA)'}")
            if nmea:
                found = True
                for line in data.decode("ascii", "replace").splitlines()[:4]:
                    if line.startswith("$"):
                        print(f"      {line}")
                break
        if found:
            break

    if found:
        print("\nThe GPS is alive. If there is no fix, that is an antenna or "
              "sky-view problem\nand a cold start can take several minutes.")
        return 0

    print("  nothing, on any port at any baud rate")

    print("""
The GPS is not transmitting at all. That rules out the antenna and the sky:
the L76K talks from power-on regardless of either. Check, in this order:

  1. The STANDBY switch on the HAT. There is a small ON/OFF slide switch
     next to the SET button. In standby the L76K is held quiet and looks
     exactly like this.

  2. Whether the L76K is actually populated. Waveshare sells this HAT with
     and without the GNSS module. The GPS variant has a square Quectel L76K
     can next to the SX1262; the non-GPS variant has empty pads there.

  3. That the HAT is fully seated on the 40-pin header.

Everything on the Pi side already checks out: the pin map matches the
published pinout, the UART is enabled, the serial console has been removed
from it, and Bluetooth has been detached so serial0 is the good PL011.
""")
    return 1


if __name__ == "__main__":
    sys.exit(main())
