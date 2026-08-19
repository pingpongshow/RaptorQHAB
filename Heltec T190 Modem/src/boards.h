/*
  boards.h — the ground station modem across the Heltec range.

  All four supported boards use an SX1262 on the same seven pins. Heltec
  standardised that across the ESP32-S3 line, and it is worth stating plainly
  because it means the radio half of this firmware is genuinely identical
  everywhere; only the display and the power switching differ.

  Pin maps are not guessed. They come from two independent open-source
  projects that ship these boards -- Meshtastic's variant headers and the
  LoRa APRS iGate's board_pinout headers -- and where both cover a board they
  agree.

  Select a board with exactly one -D BOARD_* flag from platformio.ini.
*/
#pragma once

// ---------------------------------------------------------------- radio ---
//
// Identical on every board here. If a future board differs, move these inside
// the per-board blocks rather than special-casing at the call site.
#define LORA_NSS    8
#define LORA_SCK    9
#define LORA_MOSI   10
#define LORA_MISO   11
#define LORA_RST    12
#define LORA_BUSY   13
#define LORA_DIO1   14

// Every one of these carries a TCXO on DIO3 at 1.8 V. Without setting this the
// radio does not calibrate, and the failure is silent.
#define LORA_TCXO_VOLTAGE 1.8f

// ---------------------------------------------------------------- boards ---

#if defined(BOARD_VISION_MASTER_T190)

  #define BOARD_NAME          "Vision Master T190"
  #define BOARD_HAS_TFT       1
  #define USER_BUTTON         21

  #define TFT_CS              39
  #define TFT_RST             40
  #define TFT_DC              47
  #define TFT_SCLK            38
  #define TFT_MOSI            48
  #define TFT_LED_EN          17
  #define TFT_PWR             7
  #define TFT_WIDTH           320
  #define TFT_HEIGHT          170

  #define ADC_CTRL_PIN        46
  #define ADC_CTRL_ON_STATE   HIGH
  #define VBAT_READ_PIN       6
  #define VBAT_DIVIDER_RATIO  4.9f

#elif defined(BOARD_VISION_MASTER_E290)

  #define BOARD_NAME          "Vision Master E290"
  #define BOARD_HAS_EINK      1
  #define USER_BUTTON         0

  // 2.9" 296x128 black and white panel, on its own SPI bus.
  #define EINK_CS             3
  #define EINK_DC             4
  #define EINK_RST            5
  #define EINK_BUSY           6
  #define EINK_SCLK           2
  #define EINK_MOSI           1
  #define EINK_WIDTH          296
  #define EINK_HEIGHT         128

  // VEXT powers the panel only, and is active HIGH on this board.
  #define VEXT_CTRL_PIN       18
  #define VEXT_ON_STATE       HIGH

  #define ADC_CTRL_PIN        46
  #define ADC_CTRL_ON_STATE   HIGH
  #define VBAT_READ_PIN       7
  #define VBAT_DIVIDER_RATIO  5.05f     // 4.9 * 1.03 per the Meshtastic variant

#elif defined(BOARD_WIFI_LORA_32_V4)

  #define BOARD_NAME          "WiFi LoRa 32 V4"
  #define BOARD_HAS_OLED      1
  #define USER_BUTTON         0

  #define OLED_SDA            17
  #define OLED_SCL            18
  #define OLED_RST            21
  #define OLED_WIDTH          128
  #define OLED_HEIGHT         64
  #define OLED_ADDRESS        0x3C

  // Note the polarity: VEXT is active LOW here and active HIGH on the E290.
  // Getting it backwards leaves the display dark with no other symptom.
  #define VEXT_CTRL_PIN       36
  #define VEXT_ON_STATE       LOW

  #define ADC_CTRL_PIN        37
  #define ADC_CTRL_ON_STATE   HIGH
  #define VBAT_READ_PIN       1
  #define VBAT_DIVIDER_RATIO  5.12f     // 4.9 * 1.045 per the Meshtastic variant

#elif defined(BOARD_WIFI_LORA_32_V4_NODISPLAY)

  // The same board with the panel left off, or an OLED that has failed. The
  // modem's job is the radio link; a missing display must not stop it.
  #define BOARD_NAME          "WiFi LoRa 32 V4 (headless)"
  #define USER_BUTTON         0
  #define VEXT_CTRL_PIN       36
  #define VEXT_ON_STATE       LOW
  #define ADC_CTRL_PIN        37
  #define ADC_CTRL_ON_STATE   HIGH
  #define VBAT_READ_PIN       1
  #define VBAT_DIVIDER_RATIO  5.12f

#else
  #error "No board selected. Define one of BOARD_VISION_MASTER_T190, BOARD_VISION_MASTER_E290, BOARD_WIFI_LORA_32_V4, BOARD_WIFI_LORA_32_V4_NODISPLAY."
#endif

#ifndef BOARD_HAS_TFT
  #define BOARD_HAS_TFT 0
#endif
#ifndef BOARD_HAS_EINK
  #define BOARD_HAS_EINK 0
#endif
#ifndef BOARD_HAS_OLED
  #define BOARD_HAS_OLED 0
#endif
#define BOARD_HAS_DISPLAY (BOARD_HAS_TFT || BOARD_HAS_EINK || BOARD_HAS_OLED)

// Battery limits are the same single-cell LiPo everywhere.
#define VBAT_MIN 3.0f
#define VBAT_MAX 4.2f
