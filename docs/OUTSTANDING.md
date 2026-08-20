# Outstanding — blocked or unverified

Live list. Each item says what is known, what is not, and what unblocks it.
Delete an item when it is closed, do not mark it done.

---

## 1. Dual-E22 board: never tested on hardware

The board does not exist yet. Everything about it — the pin map, the TX
arbiter, the slot-A filter bypass, both radios running at once — is reasoned
from the KiCad netlist and the datasheets, and compiles, and has never received
a packet.

The slot A rework (seven parts) has to happen before the Meshtastic radio can
hear anything at all: the stock 433 MHz filter puts about 60 dB of rejection in
front of a 900 MHz module.

---

## 2. Hygiene, not bugs

What is left after the dead-code removal. None of these affect a flight.

- **`ContentView.swift` is 2,154 lines** and holds six top-level tab views.
  A refactor, not a defect, and not worth the churn risk mid-project.
- **The macOS port list accepts any `cu.*`**, which includes Bluetooth ports.
  Auto-connect verifies the device before settling on it, so this is now
  cosmetic: the list is noisier than it needs to be.

## 3. Protocol quirks, deliberately left

- **Negative altitudes clamp to zero.** Altitude is an unsigned millimetre
  count, so a launch below sea level reports 0 m. Fixing it means changing the
  wire format in Python, Swift and the firmware together.
- **`RAPT` is transmitted twice** — once as the hardware sync word, once as the
  first four bytes of the body. Four bytes a packet, and it is what makes the
  `NoRAPT` counter such a useful diagnostic.
- **The payload enables a hardware CRC the modem does not check.** Harmless,
  and it is what makes corrupted packets countable in software.
- **`PRE: 32 bits` on the modem display** is the *transmit* preamble length,
  which means nothing on a receive-only modem.

## Closed recently, for context

- **The intermittent WiFi unreachability is understood and mitigated.** Seven
  hours of watcher data settled it: the radio was in perfect health throughout
  — signal −24 dBm, `tx_failed` **0** for the entire log, one unbroken
  45,560-second association, power save off in 1527 of 1528 samples — while not
  one inbound packet arrived, not even the broadcast ARP that precedes a ping.
  The access point had stopped delivering to a station it was still acking.

  What made the payload a candidate: **4,101 bytes transmitted in seven hours,
  with 97% of samples showing zero**. Everything it does is LoRa and the USB
  gadget, so `wlan0` sits silent for hours and an access point that ages out
  quiet stations ages this one out every time.

  `raptorhab-wifi-keepalive` sends one packet to the gateway every 30 seconds.
  Measured after enabling: **340 bytes/minute against 11 before**, with the
  gateway replying. It is a mitigation, not a cure — the fault is at the access
  point, and a Pi that is associated and acking ought to be reachable — but a
  payload whose purpose is to be found should not depend on someone else's
  bridge table remembering it. It costs nothing in flight, where the radio is
  off.

  Four earlier hypotheses were wrong and are recorded in REVIEW.md, including
  my own.

- **USB DHCP works.** `ping 10.55.0.1` and `ssh 10.55.0.1` now succeed with
  nothing configured on the host, which never worked before. Verified that it
  does not hijack the host: default route still via WiFi, no route via the
  gadget, no DNS offered, internet unaffected. The first attempt at this left
  the distribution's dnsmasq enabled and answering DNS on every interface — it
  was still doing so after the reboot, and is now disabled in favour of a
  private instance that reads one config file and binds nothing but usb0.

- **USB access was easy, and documented as hard.** `ssh raptorhab.local` works
  over the cable with nothing configured — verified going over `usb0` with WiFi
  simultaneously up. The instructions told the operator to open System Settings
  and hand-enter `10.55.0.2/24`, which is the fallback, not the method. Two
  genuine traps are now written down: `ping raptorhab.local` fails while `ssh`
  succeeds, because ping wants IPv4 and only IPv6 is usable on that link; and
  `ssh 10.55.0.1` cannot work without hand-configuring the host, because
  nothing on the cable hands out addresses. `payload/tools/find_payload.sh` covers
  the case where mDNS is not available.

- **1,407 lines of dead command protocol removed.** `payload/airborne/commands.py`
  and `payload/ground/commands.py` implemented a CMD_PING / CMD_SETPARAM /
  CMD_CAPTURE / CMD_REBOOT path over the radio. Neither half was ever
  constructed — not the payload's handler, not the ground station's
  transmitter — and it had been superseded twice over, by the USB console for
  configuration and the Meshtastic uplink for over-the-air commands. Deleting
  both halves together kept the tree self-consistent; deleting one would have
  been worse than leaving both. `PacketType.CMD_*` stays in `common/protocol.py`
  as wire-format definition.

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
