/*
  boards.h — the ground station modem across the boards people actually own.

  Every board here carries an SX1262. That is not a preference: the downlink is
  96 kbps GFSK with a 234 kHz receive bandwidth, and the SX1276 generation
  cannot do it. A board with an SX1276 will not work no matter how the pins are
  wired.

  Pin maps are not guessed. Each one is taken from the corresponding variant
  header in the Meshtastic firmware, which ships on these boards in volume and
  is the closest thing to an authoritative source for them. Where a value looks
  surprising -- the ADC control polarity differing between two Heltec boards a
  revision apart, for instance -- it is surprising in the source too, and is
  commented here rather than quietly normalised.

  The Heltec ESP32-S3 line shares one radio pin map. Everything else does not,
  so the radio pins live inside the per-board blocks.

  Select a board with exactly one -D BOARD_* flag from platformio.ini.
*/
#pragma once

// Every board here carries a TCXO on DIO3 at 1.8 V. Without setting this the
// radio does not calibrate, and the failure is silent.
#define LORA_TCXO_VOLTAGE 1.8f

// ---------------------------------------------------------------- boards ---

#if defined(BOARD_VISION_MASTER_T190)

  #define BOARD_NAME          "Vision Master T190"
  #define BOARD_HAS_TFT       1
  #define USER_BUTTON         21

  #define LORA_NSS    8
  #define LORA_SCK    9
  #define LORA_MOSI   10
  #define LORA_MISO   11
  #define LORA_RST    12
  #define LORA_BUSY   13
  #define LORA_DIO1   14

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

#elif defined(BOARD_WIFI_LORA_32_V3)

  // The most widely owned SX1262 board there is, and the one most people
  // reading this will already have in a drawer.
  #define BOARD_NAME          "WiFi LoRa 32 V3"
  #define BOARD_HAS_OLED      1
  #define USER_BUTTON         0

  #define LORA_NSS    8
  #define LORA_SCK    9
  #define LORA_MOSI   10
  #define LORA_MISO   11
  #define LORA_RST    12
  #define LORA_BUSY   13
  #define LORA_DIO1   14

  #define OLED_SDA            17
  #define OLED_SCL            18
  #define OLED_RST            21
  #define OLED_WIDTH          128
  #define OLED_HEIGHT         64
  #define OLED_ADDRESS        0x3C

  #define VEXT_CTRL_PIN       36
  #define VEXT_ON_STATE       LOW

  // Note the polarity against V4 below, which is HIGH. Heltec changed it
  // between revisions; both values are as the Meshtastic variants have them.
  // Getting it wrong reads the battery as flat.
  #define ADC_CTRL_PIN        37
  #define ADC_CTRL_ON_STATE   LOW
  #define VBAT_READ_PIN       1
  #define VBAT_DIVIDER_RATIO  5.12f     // 4.9 * 1.045 per the Meshtastic variant

#elif defined(BOARD_WIFI_LORA_32_V4)

  #define BOARD_NAME          "WiFi LoRa 32 V4"
  #define BOARD_HAS_OLED      1
  #define USER_BUTTON         0

  #define LORA_NSS    8
  #define LORA_SCK    9
  #define LORA_MOSI   10
  #define LORA_MISO   11
  #define LORA_RST    12
  #define LORA_BUSY   13
  #define LORA_DIO1   14

  #define OLED_SDA            17
  #define OLED_SCL            18
  #define OLED_RST            21
  #define OLED_WIDTH          128
  #define OLED_HEIGHT         64
  #define OLED_ADDRESS        0x3C

  #define VEXT_CTRL_PIN       36
  #define VEXT_ON_STATE       LOW

  #define ADC_CTRL_PIN        37
  #define ADC_CTRL_ON_STATE   HIGH      // opposite to V3; see above
  #define VBAT_READ_PIN       1
  #define VBAT_DIVIDER_RATIO  5.12f

#elif defined(BOARD_WIFI_LORA_32_V4_NODISPLAY)

  // The same board with the panel left off, or an OLED that has failed. The
  // modem's job is the radio link; a missing display must not stop it.
  #define BOARD_NAME          "WiFi LoRa 32 V4 (headless)"
  #define USER_BUTTON         0

  #define LORA_NSS    8
  #define LORA_SCK    9
  #define LORA_MOSI   10
  #define LORA_MISO   11
  #define LORA_RST    12
  #define LORA_BUSY   13
  #define LORA_DIO1   14

  #define VEXT_CTRL_PIN       36
  #define VEXT_ON_STATE       LOW
  #define ADC_CTRL_PIN        37
  #define ADC_CTRL_ON_STATE   HIGH
  #define VBAT_READ_PIN       1
  #define VBAT_DIVIDER_RATIO  5.12f

#elif defined(BOARD_WIRELESS_STICK_LITE_V3)

  // Same radio and same pins as the V3 above, in a much smaller package with
  // no display at all. A good permanent ground station: nothing to burn in,
  // nothing to draw current.
  #define BOARD_NAME          "Wireless Stick Lite V3"
  #define USER_BUTTON         0

  #define LORA_NSS    8
  #define LORA_SCK    9
  #define LORA_MOSI   10
  #define LORA_MISO   11
  #define LORA_RST    12
  #define LORA_BUSY   13
  #define LORA_DIO1   14

  #define VEXT_CTRL_PIN       36
  #define VEXT_ON_STATE       LOW
  #define ADC_CTRL_PIN        37
  #define ADC_CTRL_ON_STATE   LOW
  #define VBAT_READ_PIN       1
  #define VBAT_DIVIDER_RATIO  5.12f

#elif defined(BOARD_LILYGO_T3S3)

  // LilyGO T3-S3. The first board here that is not a Heltec, and the reason
  // the radio pins moved into these blocks -- nothing about its SPI wiring
  // matches.
  //
  // The 868/915 MHz version carries an SX1262. LilyGO sells visually identical
  // boards with an SX1276 and with an SX1280; neither will work here.
  #define BOARD_NAME          "LilyGO T3-S3"
  #define BOARD_HAS_OLED      1
  #define USER_BUTTON         0

  #define LORA_NSS    7
  #define LORA_SCK    5
  #define LORA_MOSI   6
  #define LORA_MISO   3
  #define LORA_RST    8
  #define LORA_BUSY   34
  #define LORA_DIO1   33

  #define OLED_SDA            18
  #define OLED_SCL            17
  #define OLED_WIDTH          128
  #define OLED_HEIGHT         64
  #define OLED_ADDRESS        0x3C

  // No VEXT rail and no ADC enable gate: the divider is always connected.
  #define VBAT_READ_PIN       1
  #define VBAT_DIVIDER_RATIO  2.11f     // per the Meshtastic variant

#elif defined(BOARD_XIAO_S3_WIO_SX1262)

  // Seeed XIAO ESP32-S3 with the Wio-SX1262 module. Cheap, tiny and now very
  // common.
  //
  // Unlike every other board here it has an external receive/transmit switch
  // on its own pin rather than one driven from DIO2, so RF_SWITCH_RX_PIN is
  // defined and the firmware wires it up. Without that the radio transmits and
  // receives into a terminated path and hears almost nothing -- which looks
  // exactly like a bad antenna.
  #define BOARD_NAME          "XIAO ESP32-S3 + Wio-SX1262"
  #define USER_BUTTON         21

  #define LORA_NSS    41
  #define LORA_SCK    7
  #define LORA_MOSI   9
  #define LORA_MISO   8
  #define LORA_RST    42
  #define LORA_BUSY   40
  #define LORA_DIO1   39

  #define RF_SWITCH_RX_PIN    38
  // TX side is not brought out; RadioLib is told so explicitly.
  #define RF_SWITCH_TX_PIN    RADIOLIB_NC

  // No battery divider on the XIAO itself.
  #define VBAT_READ_PIN       -1

#else
  #error "No board selected. Define one of: BOARD_VISION_MASTER_T190, BOARD_WIFI_LORA_32_V3, BOARD_WIFI_LORA_32_V4, BOARD_WIFI_LORA_32_V4_NODISPLAY, BOARD_WIRELESS_STICK_LITE_V3, BOARD_LILYGO_T3S3, BOARD_XIAO_S3_WIO_SX1262."
#endif

#ifndef BOARD_HAS_TFT
  #define BOARD_HAS_TFT 0
#endif
#ifndef BOARD_HAS_OLED
  #define BOARD_HAS_OLED 0
#endif
#define BOARD_HAS_DISPLAY (BOARD_HAS_TFT || BOARD_HAS_OLED)

// Battery limits are the same single-cell LiPo everywhere that has one.
#define VBAT_MIN 3.0f
#define VBAT_MAX 4.2f
