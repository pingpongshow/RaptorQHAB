# Outstanding — blocked or unverified

Live list. Each item says what is known, what is not, and what unblocks it.
Delete an item when it is closed, do not mark it done.

---

## 1. The Pi is unreachable over WiFi (workaround in place)

**Diagnosed, not fixed. Does not block anything.**

The payload is healthy and was never the problem. Reached over the USB gadget:

- `wlan0` is associated, holds 10.1.1.75, and **can ping the gateway and this
  Mac** — outbound works fine
- rfkill is clear, so the in-flight WiFi cutoff did not fire (it needs 300 m
  AGL; the payload is on a desk)
- the payload service is up and transmitting throughout

What fails is **inbound**: the Mac's ARP entry for 10.1.1.75 goes
`(incomplete)`. Outbound working while inbound fails, with ARP unresolved, is
the signature of **AP client isolation** rather than anything on the Pi.

**Workaround, and it is a good one:** the USB gadget gives a reliable path that
does not depend on WiFi at all, with no privileged setup on the Mac:

```bash
ping6 -c 3 -I en15 ff02::1     # discover it
ssh stephen@fe80::1a:11ff:fe00:2%en15
```

The Mac names the interface "RaptorHab Payload". IPv4 over the gadget would
need `10.55.0.2/24` added to `en15`, which needs an admin password; IPv6
link-local needs nothing.

**To fix properly:** check for client isolation / AP isolation on the
PingPongShow SSID, or give the Pi a static reservation on a network without it.

---

## 2. Dual-E22 board: never tested on hardware

The board does not exist yet. Everything about it — the pin map, the TX
arbiter, the slot-A filter bypass, both radios running at once — is reasoned
from the KiCad netlist and the datasheets, and compiles, and has never received
a packet.

The slot A rework (seven parts) has to happen before the Meshtastic radio can
hear anything at all: the stock 433 MHz filter puts about 60 dB of rejection in
front of a 900 MHz module.

---

## Closed recently, for context

- **The modem's CRC error rate is zero.** The `ERR` on the display was entirely
  bad-CRC, and the cause was whitening being disabled while 94% of the traffic
  was telemetry carrying ~72-bit constant runs. Measured back to back on the
  same hardware: **137 bad CRC in 14951 packets (0.916%) without whitening, 0
  in 16690 (0.000%) with it.** This is a wire-format change — every modem must
  match the payload or the link does not work at all.
- **The reason the first whitening test appeared to fail:** the payload had
  **two** `SetPacketParams` call sites, and the transmit path carried its own
  hardcoded copy with a comment reading "must match _set_fsk_packet_params". It
  did not match, and it overwrote the configured value before every packet — so
  the setting never reached the air. One definition now.
- **macOS Meshtastic over the modem.** `ModemMeshtasticLink.swift`: parses the
  `0x7B` stream, decrypts on the configured channels, and transmits via `MTX:`
  with `MCFG:` for the slot. Commands go on the private channel only, hop limit
  zero, refused with a reason when no private channel is set.

- **SNR was an error code.** `getSNR()` returns `RADIOLIB_ERR_WRONG_MODEM`
  (−20) on a non-LoRa modem; that was displayed as "−20.0 dB" in red and
  forwarded in every frame. 2281 frames of 2281 reported exactly −20.0. Now
  −128 with the display showing `n/a`, on all eight firmware builds.
- **A failed `CFG:` left the modem deaf while answering `CFG_OK`.** Now rolls
  back and reports `CFG_ERR`, on all eight builds.
- **`0x7D 0x5B` de-stuffing.** Cost 55% of image packets from a dual-radio
  modem. Fixed in the Python ground station and the macOS app.
