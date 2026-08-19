# RaptorHAB Dual-900 firmware/gs-modem

Firmware for the PingPong **Dual RF** board (ESP32-S3-WROOM-1U-N16R8) populated
with **two E22-900M30S modules** instead of the stock 900/400 pair.

It replaces the Heltec T190 and adds what a single radio could not do:

| | Heltec T190 | This board |
|---|---|---|
| RAPTOR image downlink | yes | yes (slot B) |
| Meshtastic receive | no | yes (slot A) |
| Meshtastic transmit | no | yes (slot A) |
| Uplink commands to the balloon | no | yes, on the private channel |
| Both at once | impossible — one SX1262 cannot be in GFSK and LoRa simultaneously | yes, two radios |
| Output power | ~22 dBm | ~30 dBm (1 W) per slot via the E22 PA |

The single-radio modem had to choose. Listening for Meshtastic beacons meant
not listening for images. Here the two jobs are separate radios and both run
continuously.

---

## Before you flash: the slot A filter

**Slot A must be reworked.** The stock board carries a 7-element Chebyshev
low-pass filter in slot A's RF path, designed for the 433 MHz PA's harmonics.
Its documented rejection is about **60 dB at 866 MHz**, and 915 MHz is further
into the stopband. A 900 MHz module fitted behind it is not degraded, it is
disconnected — a million-fold down in both directions.

Rework:

| Reference | Stock part | Change to |
|---|---|---|
| L2, L6 | 22 nH | 0 Ω link (0603) |
| L4 | 24 nH (Coilcraft 0603HP-24N) | 0 Ω link (0603) |
| C1, C7 | 7.5 pF | remove |
| C3, C5 | 13 pF | remove |

Seven parts. Then build with `-DSLOT_A_FILTER_BYPASSED=1` (the default in
`platformio.ini`). Set it to `0` and the firmware still runs but prints a
warning on every boot, which is the right behaviour if the code is ready before
the soldering iron is.

Sweep the path with a NanoVNA afterwards if you can: you are looking for a flat
through-path, not a filter.

---

## Pin map

Taken from the KiCad netlist of the manufactured v2 board. Stable across v1, v2
and the Single RF variant.

| Signal | Slot A (Meshtastic) | Slot B (RAPTOR) |
|---|---|---|
| NSS | GPIO38 | GPIO4 |
| NRST | GPIO18 | GPIO5 |
| BUSY | GPIO15 | GPIO7 |
| DIO1 | GPIO6 | GPIO16 |

Shared SPI: SCK **39**, MOSI **8**, MISO **40**.

Two settings are mandatory on this hardware and the firmware applies both:

- **`setDio2AsRfSwitch(true)`** — the RF switch is hardware. DIO2 drives TXEN
  and a transistor inverts it to RXEN. Without this the module idles in RX and
  a transmit drives the PA into a disabled path.
- **`setTCXO(1.8)`** — the E22 modules run a TCXO from DIO3 at 1.8 V. Without
  it the radio never calibrates.

Never key up without an antenna or a 50 Ω load on the relevant SMA.

---

## Why both radios cannot transmit at once

On the stock 900/400 board, arbitration was about thermal and supply limits:
two 1 W PAs stacked back-to-back on a 2-layer board sharing one bulk capacitor.

With two 900 MHz modules there is a third and larger problem. The radios are
centimetres apart, in the same band, with nothing between them. When slot A
transmits at 1 W, slot B's receiver is saturated — not degraded, deaf. Every
image packet arriving in that window is lost.

`TxArbiter` therefore does two things: it serialises transmissions with a 40 ms
recovery guard, and it **accounts for the receive time that costs**. The
statistics line reports it:

```
[STATS] Total:412 Fwd:408 NoRAPT:0 BadCRC:4 Rate:99.0% | Mesh rx:37 tx:2 fail:0 | RX blinded by own TX: 0.4%
```

That last figure is the point. A modem that silently drops image packets
whenever it beacons looks exactly like a bad radio link, and you would spend a
day chasing the antenna instead of the schedule.

The shared SPI bus has its own mutex. Two RadioLib instances on one `SPIClass`
interleave transactions otherwise, and the symptom is not a clean failure — it
is a radio reporting success while holding a half-written configuration.

---

## USB protocol

**RAPTOR frames are byte-for-byte identical to the T190's**, so the macOS app
and the Python ground station decode them with no changes:

```
0x7E [LEN_HI][LEN_LO][RSSI_I][RSSI_F][SNR_I][SNR_F][DATA...][CKSUM] 0x7E
```

**Meshtastic frames use delimiter `0x7B`** with the same internal structure. An
older ground station splits its input on `0x7E` and never sees them; a newer one
parses both. Putting a type byte inside the existing frame would have been
tidier and would have broken every existing parser.

Byte stuffing applies to both: `0x7E → 0x7D 0x5E`, `0x7B → 0x7D 0x5B`,
`0x7D → 0x7D 0x5D`.

Meshtastic packets are forwarded **still encrypted**. The ground station holds
the channel keys; this modem deliberately does not, so a borrowed board never
carries them.

### Commands

| Command | Effect |
|---|---|
| `CFG:<freq>,<bitrate>,<dev>,<bw>,<preamble>` | Configure the RAPTOR slot. Same syntax as the T190. |
| `MCFG:<freq>,<bw>,<sf>,<cr>,<power>` | Configure the Meshtastic slot. |
| `MTX:<hex>` | Transmit a raw, pre-encrypted Meshtastic packet. |
| `STATUS` | Print the statistics line immediately. |

Defaults match the payload: RAPTOR `915.0, 96.0, 50.0, 234.3, 32`; Meshtastic
`906.875, 250, 11, 5, 22` (US LongFast).

Frequencies outside 850–930 MHz are refused — both slots are 900 MHz hardware
now, and a typo should not key a PA into the wrong band. Power above +22 dBm is
clamped with a warning: the E22's PA already adds about 8 dB, and asking the
SX1262 for more overdrives it rather than helping.

`MTX:` takes a fully-formed, already-encrypted Meshtastic packet. That is
deliberate — it is the same division of labour the macOS app already uses with
an attached node, and it means the ground station's channel keys never leave
the ground station.

---

## Build and flash

```bash
pio run -t upload
```

Bootloader entry if needed: hold **BOOT**, tap **RST**, release BOOT.

---

## Status

Compiles clean (349 KB flash, 24 KB RAM). **Not yet tested on hardware** — the
dual-900 board has not been built. What is verified is the pin map (against the
KiCad netlist), the protocol compatibility with the existing ground stations,
and the build.

On first bring-up, check in this order:

1. Both radios report their listening line at boot. A failure here is SPI.
2. Slot B hears the payload — compare `Total:` against the T190's count.
3. Slot A hears any stock Meshtastic node. If slot B works and slot A does not,
   suspect the filter rework before anything else.
4. `MTX:` a beacon and confirm another node receives it.
5. Watch `RX blinded by own TX` across a flight's worth of beaconing.

---

## Meshtastic, end to end

The firmware could always put Meshtastic packets on the air and take them off
it. What was missing was everything at the other end of the USB cable: the
ground station discarded every packet the board forwarded, and had no way to
ask it to transmit. The capability existed and was unreachable.

### What the modem does, and deliberately does not

It forwards whole LoRa packets, **still encrypted**, on frame delimiter `0x7B`,
and transmits whatever it is handed. It holds no channel keys — a borrowed
board never carries them — so decrypting, parsing, building and encrypting all
happen on the ground station.

That split is why the modem needs no configuration to work on a new channel:
change the key on the ground and the modem is unaffected.

### Commands

| Command | Meaning |
|---|---|
| `CFG:<freq>,<bitrate>,<dev>,<bw>,<preamble>` | RAPTOR slot (slot B) |
| `MCFG:<freq>,<bw>,<sf>,<cr>,<power>` | Meshtastic slot (slot A) |
| `MTX:<hex>` | Transmit one raw packet; replies `MTX_OK` or `MTX_ERR:<why>` |

`MTX:` is answered, not fire-and-forget. An uplink to a balloon that never left
the ground station is something the operator needs to see immediately.

### From the ground station

```python
link = serial_manager.meshtastic          # a ModemMeshtasticLink

link.configure_slot(915.0, power_dbm=30)  # point slot A at your region
link.send_text("hello mesh")              # public channel
ok, why = link.send_command("beacon now") # private channel only
```

Commands go on the **private channel only**, and the link refuses to send one
if no private channel is configured. That mirrors the payload, which ignores
commands arriving on the public channel because anyone can transmit there.

### Why this is not the same as the other two Meshtastic sources

The ground station already had a Meshtastic node over USB, and MQTT. This is
different from both:

- A **node** hears what a node on the ground hears.
- **MQTT** hears what the internet has been told, if anything.
- **This** hears the balloon directly, on the ground station's own antenna,
  with no node and no internet in between — and it is the only one of the three
  that can transmit to the balloon on the private channel without a second
  radio.
