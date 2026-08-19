# Outstanding — blocked or unverified

Live list. Each item says what is known, what is not, and what unblocks it.
Delete an item when it is closed, do not mark it done.

---

## 1. The payload intermittently unreachable over WiFi — cause unknown

**Four hypotheses tested, all four wrong. Now instrumented and waiting.**

The fault: the payload stays associated, holds its lease, and can reach the
gateway and the Mac — but nothing can reach *it*. It clears on its own, and
transmitting appears to help.

### What it is not

| Hypothesis | Ruled out by |
|---|---|
| AP client isolation | The payload can ping the Mac that cannot reach it. Isolation blocks both directions. **This was my own earlier diagnosis and it was wrong.** |
| 802.11 power save | `iw dev wlan0 get power_save` → **off**, and NetworkManager resolves `wifi.powersave=2` from the drop-in. The earlier fix did work. |
| SDIO runtime suspend | `/sys/class/net/wlan0/device/power/runtime_status` → `unsupported`. |
| Regulatory misconfiguration | 4,612 `CTRL-EVENT-REGDOM-CHANGE ... type=WORLD` messages looked damning, and the global domain really is `country 00`. But `iw phy0 reg get` reports **`country 99`** — the device is *self-managed*, so it owns its own regulatory domain and the global one is meaningless for it. `iw reg set` is ignored by design. The messages are cosmetic. |

### What is known

- The radio is healthy while it happens: `signal -24 dBm`, `tx failed 0`,
  `inactive time 0 ms`, one association lasting hours.
- The payload sends **zero bytes** on `wlan0` when idle — measured, 10 seconds,
  counters unmoved. Everything it does is LoRa and the USB gadget. So the
  interface is genuinely silent for long stretches.
- During one failure, 5 pings in a row were lost with the payload idle and 5 of
  8 succeeded while it was transmitting continuously — which is what pointed at
  power save before the driver contradicted it.
- It would **not reproduce** across eight minutes of deliberate silence, and is
  behaving perfectly now.

### Instrumented

`raptorhab-wifi-watch.service` records every 20 s, passively — it sends
nothing, because generating traffic is exactly what makes the payload reachable
again and a probe would paper over the thing being investigated:

```
2026-08-19T17:45:20+00:00 rx=7493481 tx=482483 inactive_ms=0 signal_dbm=-24 \
    tx_failed=0 assoc_age_s=15271 bssid=ba:28:aa:94:dd:3b ps=off ip=10.1.1.75/24
```

`inactive_ms` is the number to watch: how long since the AP last heard a frame
from us. `assoc_age_s` resets on reassociation, so a silent relink shows up even
when nothing logs a disconnect.

**Next time it happens, read
`/var/lib/raptorhab/wifi_watch.log`** rather than hypothesising. That is the
whole point of it.

### Meanwhile

The USB gadget is unaffected and needs no privileged setup on the Mac:

```bash
ping6 -c 3 -I en15 ff02::1
ssh stephen@fe80::1a:11ff:fe00:2%en15
```

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

## 3. Hygiene, not bugs

What is left after the dead-code removal. None of these affect a flight.

- **`ContentView.swift` is 2,154 lines** and holds six top-level tab views.
  A refactor, not a defect, and not worth the churn risk mid-project.
- **The macOS port list accepts any `cu.*`**, which includes Bluetooth ports.
  Auto-connect verifies the device before settling on it, so this is now
  cosmetic: the list is noisier than it needs to be.

## 4. Protocol quirks, deliberately left

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
