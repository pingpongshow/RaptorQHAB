# Supported boards

One source tree, seven builds. Pick the environment for the board you have:

```bash
pio run -e heltec_wifi_lora_32_v3 -t upload
```

| Board | Display | Battery sense | Environment |
|---|---|---|---|
| Heltec WiFi LoRa 32 V3 | 0.96" OLED | yes | `heltec_wifi_lora_32_v3` |
| Heltec WiFi LoRa 32 V4 | 0.96" OLED | yes | `heltec_wifi_lora_32_v4` |
| Heltec WiFi LoRa 32 V4 | none | yes | `heltec_wifi_lora_32_v4_headless` |
| Heltec Wireless Stick Lite V3 | none | yes | `heltec_wireless_stick_lite_v3` |
| Heltec Vision Master T190 | 1.9" colour TFT | yes | `heltec_vision_master_t190` |
| LilyGO T3-S3 (868/915) | 0.96" OLED | yes | `lilygo_t3s3` |
| Seeed XIAO ESP32-S3 + Wio-SX1262 | none | no | `xiao_s3_wio_sx1262` |

## It has to be an SX1262

Not a preference. The downlink is 96 kbps GFSK with 50 kHz deviation and a
234 kHz receive bandwidth; the SX1276 generation cannot reach those numbers.
A board with an SX1276 will not work here no matter how it is wired.

This rules out some very common hardware — the original T-Beam, the T3 v1.6,
and most of the cheap 433/915 modules sold for LoRa work. Check the chip, not
the board name: LilyGO in particular ships visually identical T3-S3 boards with
an SX1262, an SX1276 and an SX1280.

## Where the pin maps come from

Each is taken from the corresponding variant header in the **Meshtastic
firmware**, which ships on these boards in volume. They are not read off
photographs and not inferred.

Where a value looks wrong it is usually right, and commented in `boards.h`:

- **The ADC enable polarity differs between the V3 and the V4.** Active LOW on
  the V3 and the Wireless Stick Lite; active HIGH on the V4 and the T190.
  Heltec changed it between revisions. The firmware used to write `HIGH`
  unconditionally, which would have read the battery as flat on every V3.
- **VEXT is active LOW** on the Heltec boards that have it, and the T3-S3 does
  not have the rail at all.
- **The XIAO has an external RX switch on GPIO 38** rather than one driven from
  DIO2 like every other board here. `RF_SWITCH_RX_PIN` is defined for it and
  the firmware calls `setRfSwitchPins()` before `begin()`. Without that the
  radio appears to initialise fine and hears almost nothing, which looks
  exactly like a bad antenna.

## Which one should you buy

**Heltec WiFi LoRa 32 V3** if you have no strong opinion. It is the most widely
stocked SX1262 board, it has a display, and it is cheap.

**Wireless Stick Lite V3** for a ground station that lives on a shelf. No
display to burn in, nothing drawing current for a screen nobody is watching.

**Vision Master T190** if you want to read RSSI and packet counts across a
field. The colour TFT is genuinely the nicest of these to use.

**XIAO + Wio-SX1262** if size or price dominates. No battery divider, so it
reports "USB powered" rather than inventing a voltage.

## No display is fine

The modem's job is the radio link. Every headless environment is a first-class
build, not a degraded one, and a board whose OLED has failed can be flashed
with the headless build and keep working.

## What was removed

**Vision Master E290 (2.9" e-ink).** Dropped: the panel refreshes far too
slowly to show a live link. It was limited to one update a minute, which is the
wrong instrument for watching RSSI while you aim an antenna, and the partial
refresh needed to do better leaves ghosting that makes small text unreadable.
The T190 does the same job properly.
