# Supported ground station boards

One firmware, four Heltec boards.

| Board | Display | Build environment |
|---|---|---|
| Vision Master T190 | 1.9" colour TFT | `heltec_vision_master_t190` |
| Vision Master E290 | 2.9" e-ink | `heltec_vision_master_e290` |
| WiFi LoRa 32 V4 | 0.96" OLED | `heltec_wifi_lora_32_v4` |
| WiFi LoRa 32 V4 | none fitted | `heltec_wifi_lora_32_v4_headless` |

```bash
pio run -e heltec_vision_master_e290 -t upload
pio run                                   # builds all four
```

## Why this was a small change

Every one of these boards puts the SX1262 on the **same seven pins**:

| Signal | GPIO |
|---|---|
| NSS | 8 |
| SCK | 9 |
| MOSI | 10 |
| MISO | 11 |
| RST | 12 |
| BUSY | 13 |
| DIO1 | 14 |

All four also carry a TCXO on DIO3 at 1.8 V. Heltec standardised this across
the ESP32-S3 range, so the radio half of the firmware — the part that actually
matters — is identical everywhere. Only the display and the power switching
differ, and that is all `boards.h` describes.

Pin maps were not guessed. They come from two independent open-source projects
that ship these boards, Meshtastic's variant headers and the LoRa APRS iGate's
`board_pinout.h` files, and where both cover a board they agree.

## Per-board notes

### Vision Master T190

The original target, unchanged. Its firmware is byte-for-byte the same size
after this refactor as before it, which is the check worth having: the board
that was working still gets exactly the code that was working.

### Vision Master E290 (e-ink)

E-ink is the wrong display for live telemetry and the right one for a ground
station left running. It holds the last reading with the power off, which is
what you want when you come back to a modem that has been sitting in a field
overnight.

It is therefore driven very differently from the TFT:

- **Refreshed at most once a minute**, and only when the packet count has
  actually changed. A refresh that alters no pixels is pure panel wear.
- A full refresh takes around **two seconds during which nothing else runs**.
  Driving it at the TFT's rate would make the modem miss packets.
- `showConfiguredScreen` does refresh immediately, because that is the moment
  the operator is waiting for confirmation.

`VEXT` on this board is **active HIGH** and powers the panel only.

### WiFi LoRa 32 V4 (OLED)

128×64 SSD1306. Small enough that the whole frame is redrawn each time — there
is no partial-update complexity worth the code at that size.

`VEXT` here is **active LOW**, the opposite of the E290. Getting that backwards
leaves the display dark with no other symptom, which is why the polarity is
named in `boards.h` rather than written as a bare `digitalWrite`.

If the OLED is missing or fails to answer on I²C, the firmware logs it and
carries on. The modem's job is the radio link.

### Headless V4

For a board with no panel fitted. Everything the modem does happens over USB;
the display was only ever a convenience. Saves about 18 KB.

## Adding another board

1. Add a block to `boards.h` with its pins and one `BOARD_HAS_*` flag.
2. If it has a display type not already handled, add an implementation of the
   eight display functions next to the existing ones in `main.cpp`.
3. Add an environment to `platformio.ini` with the `-D BOARD_*` flag and any
   display library it needs.

If the new board also puts the SX1262 on 8/9/10/11/12/13/14, step 1 is the only
radio work there is.
