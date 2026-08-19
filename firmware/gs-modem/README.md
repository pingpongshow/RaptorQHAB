# RaptorHAB firmware/gs-modem

The radio that hears the balloon. It receives the RAPTOR GFSK downlink —
telemetry and fountain-coded image packets — validates each packet, and
forwards it to the companion apps over USB serial.

Runs on seven boards from one source tree. See **[BOARDS.md](BOARDS.md)** for
the pin maps and per-board behaviour.

| Board | Display | Environment |
|---|---|---|
| Heltec WiFi LoRa 32 V3 | 0.96" OLED | `heltec_wifi_lora_32_v3` |
| Heltec WiFi LoRa 32 V4 | 0.96" OLED | `heltec_wifi_lora_32_v4` |
| Heltec WiFi LoRa 32 V4 | none | `heltec_wifi_lora_32_v4_headless` |
| Heltec Wireless Stick Lite V3 | none | `heltec_wireless_stick_lite_v3` |
| Heltec Vision Master T190 | 1.9" colour TFT | `heltec_vision_master_t190` |
| LilyGO T3-S3 | 0.96" OLED | `lilygo_t3s3` |
| Seeed XIAO ESP32-S3 + Wio-SX1262 | none | `xiao_s3_wio_sx1262` |

**Every one of these needs an SX1262.** The downlink is 96 kbps GFSK with a
234 kHz receive bandwidth and the SX1276 generation cannot do it — so the
original T-Beam, the T3 v1.6, and the many 433/915 boards built around an
SX1276 will not work here whatever the pins are set to.

```bash
pio run -e heltec_wifi_lora_32_v3 -t upload
pio run                                          # build all seven
```

## What it does

- **SX1262 in GFSK**, 96 kbps at 915 MHz by default, matching the payload
- **Validates before forwarding**: the `RAPT` sync word, then a CRC-32 over the
  whole packet. Bad packets are counted, not passed on
- **Settings persist in flash**, so the modem comes up listening after a power
  cycle instead of waiting for a host to configure it
- **Reconfigurable at any time** over USB, not only during a boot window
- **Battery voltage** on the display, where there is one

## Configuration

Send over USB at 921600 baud:

```
CFG:<freq_mhz>,<bitrate_kbps>,<deviation_khz>,<bandwidth_khz>,<preamble_bits>
```

The default, which matches the payload:

```
CFG:915.0,96.0,50.0,234.3,32
```

It replies `CFG_OK:...`, saves to flash, and reconfigures the radio. Both
ground stations send this automatically on connect.

Two values are worth understanding rather than copying:

- **Bandwidth 234.3 kHz** comes from Carson's rule for 96 kbps at 50 kHz
  deviation: 2 × (50 + 48) = 196 kHz, so the next available step up. The 467 kHz
  this firmware once used was inherited from a 200 kbps profile and let in 2.4×
  more noise than the signal occupies.
- **Preamble 32 bits** here is the *receiver's* setting, which sizes its
  preamble detector. The payload transmits 128 so the detector has margin to
  settle. Raising this to match the payload removes that margin, and the
  receiver detects nothing at all — measured, not theorised.

## Packet framing to the host

```
0x7E [LEN_HI][LEN_LO][RSSI_I][RSSI_F][SNR_I][SNR_F][DATA...][CKSUM] 0x7E
```

Byte stuffing: `0x7E → 0x7D 0x5E`, `0x7D → 0x7D 0x5D`, applied to every byte
including the header.

## Status line

Printed every ten seconds:

```
[STATS] Total:412 Fwd:408 NoRAPT:0 BadCRC:4 Err:0 Rate:99.0% Batt:3.87V(72%)
```

`NoRAPT` counts packets that carried no sync word — usually another transmitter
on the frequency. `BadCRC` counts ours that arrived damaged.

## What it does not do

There is **no Bluetooth**. An earlier revision of this document claimed BLE
packet forwarding; the firmware has never contained it. Everything goes over
USB serial.
