# Payload

The airborne half: flight software for a Raspberry Pi Zero 2 W, its installer,
and the tools that go with it.

For what the system *is* — the radio design, the flight zones, what has been
measured on hardware — see the [project README](../README.md). This file used
to repeat that, and the copies drifted: it was still claiming 89 parameters and
559 tests when the real figures were 113 and 789. A single source is worth more
than two that agree only when someone remembers.

## Layout

```
airborne/     the flight software — main loop, camera, zones, scheduling
common/       shared with the ground station: radio, protocol, crypto, GPS
ground/       receiver-side code that runs on a Pi rather than a laptop
setup/        installer, systemd units, USB gadget and power helpers
tools/        things you run by hand: provisioning, keys, diagnostics
tests/        789 tests, no hardware required
```

## Running the tests

```bash
cd payload && python -m pytest tests/ -q
```

They need no radio, no GPS and no Pi. The hardware-dependent parts are covered
by [the test plan](../docs/TESTPLAN.md), which says plainly which checks a
machine can make and which need a person.

## Installing onto a Pi

Card preparation, first boot and the installer are in
[docs/INSTALL.md](../docs/INSTALL.md). The short version, from a checkout:

```bash
sudo ./setup/install.sh --usb-ethernet
```

## Getting into a running payload

```bash
ssh <your-username>@raptorhab.local
```

Works over WiFi or over the USB cable, with nothing to configure. If mDNS is
unavailable, `./tools/find_payload.sh <your-username>` prints the exact command.

Note that `ping raptorhab.local` fails while `ssh` succeeds — ping asks for an
IPv4 address and only the IPv6 one is usable over the cable. The payload is
not down.
