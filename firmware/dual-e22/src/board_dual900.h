/*
  board_dual900.h — PingPong Dual RF board wired for two 900 MHz modules.

  Pin assignments are taken from the KiCad netlist of the manufactured v2
  board (see ESP32-S3_E22_Custom_LoRa/README.md §2) and are stable across
  v1, v2 and the Single RF variant. Do not change without a PCB revision.

  The stock board ships slot A with an E22-400M30S behind a 7-element 433 MHz
  low-pass filter. This firmware assumes slot A has been repopulated with an
  E22-900M30S *and* that the filter has been bypassed -- L2/L4/L6 replaced with
  0 ohm links, C1/C3/C5/C7 removed. Without that rework the filter puts about
  60 dB of attenuation in slot A's path at 915 MHz, which is not a degradation
  so much as a disconnection.
*/
#pragma once
#include <stdint.h>

// ---------- Shared SPI (FSPI) ----------
#define PIN_SPI_SCK    39
#define PIN_SPI_MOSI    8
#define PIN_SPI_MISO   40

// ---------- Per-slot control lines ----------
struct SlotPins {
    int8_t nss;
    int8_t rst;
    int8_t busy;
    int8_t dio1;
};

enum SlotId : uint8_t {
    SLOT_MESHTASTIC = 0,   // physical slot A (38/18/15/6)
    SLOT_RAPTOR     = 1,   // physical slot B (4/5/7/16)
    NUM_SLOTS       = 2,
};

// Slot A — was the 400 MHz slot; now an E22-900M30S for Meshtastic.
static constexpr SlotPins SLOT_A_PINS = { 38, 18, 15, 6 };
// Slot B — E22-900M30S, the RAPTOR downlink receiver.
static constexpr SlotPins SLOT_B_PINS = {  4,  5,  7, 16 };

static constexpr SlotPins SLOT_PINS[NUM_SLOTS] = { SLOT_A_PINS, SLOT_B_PINS };
static constexpr const char* SLOT_NAME[NUM_SLOTS] = { "meshtastic", "raptor" };

// ---------- Mandatory radio settings for this board ----------
//
// DIO2 drives TXEN directly and a transistor inverts it to RXEN. There are no
// MCU pins on the RF switch, so a driver that does not set this leaves the
// module stuck in RX and drives the PA into a disabled path on transmit.
#define USE_DIO2_AS_RF_SWITCH   true

// The E22 modules carry a TCXO powered from DIO3 at 1.8 V. Without this the
// radio never calibrates.
#define TCXO_VOLTAGE_V          1.8f

// The E22's own PA adds roughly 8 dB on top of the SX1262 output to reach the
// module's rated 1 W. Asking the SX1262 for more than +22 dBm overdrives it.
#define SX1262_MAX_DBM          22

// ---------- Peripherals ----------
#define PIN_I2C_SDA    42       // J4, 4k7 pull-up fitted on board
#define PIN_I2C_SCL     2
#define PIN_USER_BUTTON 0       // BOOT

// ---------- Band guard ----------
// Both slots are 900 MHz hardware now. Reject anything outside the module's
// range rather than letting a typo key a PA into the wrong band.
#define BAND_MIN_MHZ   850.0f
#define BAND_MAX_MHZ   930.0f
