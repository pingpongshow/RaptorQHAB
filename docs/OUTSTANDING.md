# Outstanding — blocked or unverified

Live list. Each item says what is known, what is not, and what unblocks it.
Delete an item when it is closed, do not mark it done.

---

## 1. The modem's CRC error rate — root cause not confirmed

**Status: diagnosed, one contributing factor removed, root cause untested.**

The `ERR` on the T190 display is entirely CRC failures — measured on the bench
unit, `NoRAPT:3 BadCRC:14996 Err:0` against `Total:955128`. The radio hears a
well-formed RAPTOR packet and the checksum over the body fails.

| | Rate |
|---|---|
| Before | 1.201% over 7412 packets |
| After removing `getSNR()` from the RX path | 0.916% over 14951 packets |

Real, and far too small to be the whole story. **~0.9% of packets are still
being corrupted.**

### What the evidence already rules out

The hardware sync word is `RAPT`, so only clean-sync packets are counted, and
the software then checks the *next* four bytes. Uniform bit errors — noise, a
weak signal, front-end overload — would hit those four bytes about 4/44 of the
time: roughly 1360 of the 14996 failures. **Observed: 3.** Errors are ~450×
rarer at the start of a packet than random noise would produce, so this
accumulates *through* the packet. It is not ordinary RF noise.

### The two candidates

- **Sampling-clock drift from missing bit transitions.** Whitening is disabled
  on both ends (`whitening = 0x00` in `Pi/common/radio.py`; RadioLib defaults
  it off). 94% of traffic is 48-byte telemetry with a **median longest
  identical-byte run of 9 bytes** — about 72 bit periods with nothing for the
  receiver's clock recovery to lock to. `REVIEW.md` already predicted this
  under "the all-zeros test artifact".
- **Buffer overwrite during read.** The radio is in continuous receive, so a
  packet arriving while `readData()` runs can overwrite the tail of the one
  being read. This is consistent with the same "errors later in the packet"
  signature, and with `getSNR()`'s removal helping slightly by shortening the
  window.

### The test that settles it

Enable whitening on both ends and re-measure. It was set up and reflashed, then
the payload went off the network before it could be updated. **A whitening
mismatch breaks the link completely** (`Fwd 0, NoRAPT 264`) — which at least
confirms the setting bites. Both ends were reverted; the link is back at 99.1%.

**Blocked on:** the Pi being reachable. Both ends must change together.

**Roughly five minutes of work** once it is: set `whitening = 0x01` in
`radio.py` and `radio->setWhitening(true, 0x01FF)` in both firmwares, flash,
restart, measure for three minutes, compare against 0.916%.

If whitening does not move it, the next step is the buffer-overwrite
hypothesis: read the packet before anything else touches SPI, and consider
single-packet receive with an explicit re-arm.

---

## 2. The Pi is off the network

Unreachable since roughly 14:20 — no ping, ARP entry incomplete. It is still
transmitting (the modem is receiving its packets), so the payload software is
running; only the network is gone.

Not yet established whether this is the same WiFi-drop seen earlier in the
session or something new. **It is not the in-flight WiFi cutoff** — that needs
300 m AGL on three consecutive 3D fixes, and the payload is on a desk.

**Blocked on:** physical access, or it coming back on its own.

Worth checking when it does: `journalctl -b -u raptorhab-airborne` around the
drop, and whether `wpa_supplicant`/NetworkManager logged a disassociation.

---

## 3. macOS app: Meshtastic over the dual-E22 modem

The de-stuffing fix is in, so the app decodes a dual-radio modem's **image**
traffic correctly. It still does not do the Meshtastic half:

- does not parse the `0x7B` frames, so packets the board hears are discarded
- has no `MTX:` transmit path, so it cannot uplink to the balloon
- has no `MCFG:` path to point the mesh slot at a region

The Python ground station has all three (`ModemMeshtasticLink`), and the Swift
Meshtastic implementation already exists for the parity tests, so this is
plumbing rather than new protocol work.

**Not blocked.** Just not done.

---

## 4. Dual-E22 board: never tested on hardware

The board does not exist yet. Everything about it — the pin map, the TX
arbiter, the slot-A filter bypass, both radios running at once — is reasoned
from the KiCad netlist and the datasheets, and compiles, and has never received
a packet.

The slot A rework (seven parts) has to happen before the Meshtastic radio can
hear anything at all: the stock 433 MHz filter puts about 60 dB of rejection in
front of a 900 MHz module.

---

## Closed recently, for context

- **SNR was an error code.** `getSNR()` returns `RADIOLIB_ERR_WRONG_MODEM`
  (−20) on a non-LoRa modem; that was displayed as "−20.0 dB" in red and
  forwarded in every frame. 2281 frames of 2281 reported exactly −20.0. Now
  −128 with the display showing `n/a`, on all eight firmware builds.
- **A failed `CFG:` left the modem deaf while answering `CFG_OK`.** Now rolls
  back and reports `CFG_ERR`, on all eight builds.
- **`0x7D 0x5B` de-stuffing.** Cost 55% of image packets from a dual-radio
  modem. Fixed in the Python ground station and the macOS app.
