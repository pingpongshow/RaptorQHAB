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

## 2. Whitening is a wire-format change — every modem must be reflashed

Not a defect, but the thing most likely to waste an afternoon.

Whitening is now on in the payload and in all eight firmware builds. A modem
running older firmware **will not work at all** with a current payload — not
degraded, nothing. The sync word is not whitened, so packets arrive cleanly and
then fail every content check: `Fwd 0, NoRAPT <climbing>`.

The bench T190 is flashed. Any other board — a spare, a second ground station,
anything flashed before this — needs `pio run -e <env> -t upload`.

---

## 3. Dual-E22 board: never tested on hardware

The board does not exist yet. Everything about it — the pin map, the TX
arbiter, the slot-A filter bypass, both radios running at once — is reasoned
from the KiCad netlist and the datasheets, and compiles, and has never received
a packet.

The slot A rework (seven parts) has to happen before the Meshtastic radio can
hear anything at all: the stock 433 MHz filter puts about 60 dB of rejection in
front of a 900 MHz module.

---

## 4. Hygiene, not bugs

Carried from the first review and still true. None of these affect a flight.

- **`Pi/airborne/commands.py` is unreachable** — 620 lines implementing an RF
  command path nothing constructs. Its replay window was fixed and it now says
  at the top that it is unwired and authenticates nothing, so it is safe to
  leave; it should eventually be wired up or deleted rather than left ambiguous.
- **`PacketScheduler`'s `telemetry_callback`** is accepted and never passed, so
  that branch is dead code.
- **`Pi/common/__pycache__/radio_hardware_spi.cpython-313.pyc`** references a
  module that no longer exists — a refactor left it behind.
- **`ContentView.swift` is 2,154 lines** and holds six top-level tab views.
- **The macOS port list accepts any `cu.*`**, which includes Bluetooth ports.
  Auto-connect now verifies the device before settling on it, so this is
  cosmetic — the list is noisier than it should be.
- **`RateLimiter` in `Pi/airborne/utils.py` is unused.**

## 5. Protocol quirks, deliberately left

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
