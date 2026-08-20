/*
 * RaptorHab Ground Station Bridge
 * Heltec Vision Master T190 (ESP32-S3 + SX1262)
 *
 * Receives packets via SX1262 and forwards them over USB serial only
 * Displays RSSI, SNR, radio settings on 1.9" TFT LCD
 *
 * CONFIGURATION MODE:
 *   On boot, modem waits for configuration from Mac app via USB
 *   Config command: CFG:<freq>,<bitrate>,<deviation>,<bandwidth>,<preamble>\n
 *   Example: CFG:915.0,96.0,50.0,467.0,32\n
 *   Response: CFG_OK:<params>\n or CFG_ERR:<message>\n
 *
 * Serial Protocol (USB):
 *   [0x7E][LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM][0x7E]
 *
 * TFT Display:
 *   - Shows RSSI, SNR, packet counts, and radio settings
 *   - Updates only during idle periods (no packets for >750ms)
 */

#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include <Preferences.h>
#include <Adafruit_GFX.h>
#include "boards.h"

#if BOARD_HAS_TFT
  #include <Adafruit_ST7789.h>
#elif BOARD_HAS_OLED
  #include <Adafruit_SSD1306.h>

#endif

// ============================================================================
// Configuration
// ============================================================================

#define DEBUG_OUTPUT        false

// Pin Definitions - Heltec Vision Master T190 LoRa
#define DEFAULT_FREQUENCY       915.0
#define DEFAULT_BITRATE         96.0
#define DEFAULT_DEVIATION       50.0
#define DEFAULT_RX_BANDWIDTH    234.3
#define DEFAULT_PREAMBLE_LEN    32
#define RF_DATA_SHAPING         0.5

// Configuration timeout
#define CONFIG_TIMEOUT_MS       120000    // 2 minutes

// Display update configuration
#define DISPLAY_IDLE_THRESHOLD_MS   750
#define DISPLAY_UPDATE_INTERVAL_MS  500
#define DISPLAY_STATS_INTERVAL_MS   1000

// Sync word "RAPT"
// Sent in place of an SNR reading, because GFSK does not have one. Chosen to
// be outside any physically meaningful SNR so a host cannot mistake it for a
// measurement; it fits the int8 the frame carries.
#define SNR_NOT_AVAILABLE (-128.0f)

const uint8_t SYNC_WORD[] = {0x52, 0x41, 0x50, 0x54};
#define SYNC_WORD_LEN       4

// Serial Protocol
#define FRAME_DELIMITER     0x7E
#define SERIAL_BAUD         921600
#define MAX_PACKET_SIZE     255

// Colors for display. ST77XX_* only exists when the TFT driver is compiled in;
// the other panels are monochrome and do not use these at all.
#if BOARD_HAS_TFT
#define COLOR_BG            ST77XX_BLACK
#define COLOR_HEADER        0x001F   // Dark blue
#define COLOR_TEXT          ST77XX_WHITE
#define COLOR_LABEL         0x8410   // Gray
#define COLOR_VALUE         ST77XX_CYAN
#define COLOR_GOOD          ST77XX_GREEN
#define COLOR_WARN          ST77XX_YELLOW
#define COLOR_BAD           ST77XX_RED
#define COLOR_ACCENT        0x07FF   // Cyan
#endif  // BOARD_HAS_TFT

// ============================================================================
// Debug macros
// ============================================================================

#if DEBUG_OUTPUT
  #define DBG(x) Serial.print(x)
  #define DBGLN(x) Serial.println(x)
  #define DBGF(...) Serial.printf(__VA_ARGS__)
#else
  #define DBG(x)
  #define DBGLN(x)
  #define DBGF(...)
#endif

// ============================================================================
// Runtime RF Configuration
// ============================================================================

float rfFrequency = DEFAULT_FREQUENCY;
float rfBitrate = DEFAULT_BITRATE;
float rfDeviation = DEFAULT_DEVIATION;
float rfRxBandwidth = DEFAULT_RX_BANDWIDTH;
uint16_t rfPreambleLen = DEFAULT_PREAMBLE_LEN;

// Non-volatile storage for the RF configuration. Without this the modem comes
// up deaf after every power cycle: the radio is not initialised until someone
// sends CFG:, so unplugging the modem silently costs you the downlink until
// the app happens to reconnect and reconfigure it.
Preferences prefs;
#define CFG_NAMESPACE   "raptorhab"
#define CFG_VERSION     1

bool configured = false;

// ============================================================================
// Global Objects - Radio & Display
// ============================================================================

// RadioLib keeps clearIrqStatus protected, which is reasonable -- clearing
// the wrong bit races the interrupt that carries every packet. Exposing just
// that call through a subclass lets the link funnel reset the two status bits
// it counts, turning a latched flag that fires once into an honest rate.
class SX1262Funnel : public SX1262 {
public:
    using SX1262::SX1262;
    int16_t clearIrq(uint16_t mask) { return clearIrqStatus(mask); }
};

SPIClass* spi = nullptr;
SX1262Funnel* radio = nullptr;
#if BOARD_HAS_TFT
SPIClass* tftSpi = nullptr;
Adafruit_ST7789* tft = nullptr;
#endif

volatile bool packetReceived = false;
uint32_t packetsTotal = 0;
uint32_t packetsForwarded = 0;
// Frames the USB host was not draining fast enough to accept. Counted and
// shown rather than dropped in silence -- a link that is quietly losing
// frames looks identical to one that is idle.
uint32_t usbDropped = 0;
// Frames offered to an attached USB host -- the denominator that makes the
// success rate honest. Radio totals keep climbing while the modem sits
// unattended, so dividing by them punishes the rate for time nobody was
// listening.
uint32_t usbOffered = 0;

// Live packet rate over a one-second window. This replaces SNR on the
// displays: the SX1262 measures SNR only for LoRa, so on the GFSK image link
// the field could never show anything but n/a. Packets per second is the
// figure a glance at the screen actually wants -- is the link alive, and how
// busy is it.
float currentPacketRate() {
    static uint32_t winStart = 0;
    static uint32_t winCount = 0;
    static float rate = 0.0f;
    uint32_t now = millis();
    if (now - winStart >= 1000) {
        if (winStart != 0) {
            rate = (packetsTotal - winCount) * 1000.0f / (now - winStart);
        }
        winStart = now;
        winCount = packetsTotal;
    }
    return rate;
}

uint32_t packetsRejectedNoRapt = 0;
uint32_t packetsRejectedCrc = 0;
uint32_t packetsRadioError = 0;
// Link-margin funnel. GFSK has no frequency-error measurement -- the SX1262
// exposes that only for LoRa, and RadioLib returns 0.0 on any other modem,
// which is the same trap the SNR field fell into. What *is* measurable in
// GFSK is how far each transmission got: preamble seen, sync word matched,
// whole packet recovered. Drift, a failing TCXO in the cold, or a marginal
// antenna all show the same way -- preambles keep arriving while sync
// matches fall away -- and that is the diagnosis the frequency-error
// reading would have been used to make.
uint32_t preamblesDetected = 0;
uint32_t syncWordsMatched = 0;
uint32_t packetsSmall = 0;
uint32_t packetsLarge = 0;
float lastRssi = -120.0;
float lastSnr = 0.0;

// Battery monitoring
float batteryVoltage = 0.0;
int batteryPercent = 0;
float prevBatteryVoltage = -1.0;

uint32_t lastStatsTime = 0;
uint32_t lastPacketTime = 0;
uint32_t lastDisplayUpdate = 0;
uint32_t lastStatsDisplayUpdate = 0;
bool displayNeedsFullRedraw = true;

float prevRssi = -999;
float prevSnr = -999;
uint32_t prevPacketsForwarded = 0;
uint32_t prevPacketsTotal = 0;

// ============================================================================
// CRC32 (IEEE 802.3 polynomial)
// ============================================================================

uint32_t crc32(const uint8_t* data, size_t len) {
    uint32_t crc = 0xFFFFFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int j = 0; j < 8; j++) {
            crc = (crc >> 1) ^ (0xEDB88320 & -(crc & 1));
        }
    }
    return ~crc;
}

// ============================================================================
// Forward Declarations
// ============================================================================

bool parseConfigCommand(const String& cmd);

// The longest command this accepts, with room to spare. Anything longer is not
// a command, and letting a String grow without limit on a device with no
// virtual memory is a way to run the heap out from the far end of a USB cable.
// Applies to the boot configuration window as well as the runtime handler --
// the boot window listens for two minutes, which is plenty of time for a
// terminal opened at the wrong baud rate to fill the heap with framing noise.
static const size_t MAX_COMMAND_LEN = 128;

// ============================================================================
// Interrupt Handler
// ============================================================================

void IRAM_ATTR onPacketReceived() {
    packetReceived = true;
}

// ============================================================================
// Forward Declarations
// ============================================================================

void handlePacket();
bool forwardPacket(uint8_t* data, int len, float rssi, float snr);
void sendStats();
bool waitForConfiguration();
bool loadConfiguration();
void saveConfiguration();
void handleUsbCommands();
bool initializeRadio();
bool configureRadio();
float currentPacketRate();
void sampleLinkFunnel();
void initDisplay();
void drawStaticUI();
void updateDisplay();
void updateSignalDisplay();
void updateStatsDisplay();
void updateBatteryDisplay();
float readBatteryVoltage();
void showWaitingScreen();
void showConfiguredScreen();

// ============================================================================
// Display Functions
// ============================================================================

#if BOARD_HAS_TFT

void initDisplay() {
    pinMode(TFT_PWR, OUTPUT);
    digitalWrite(TFT_PWR, LOW);
    delay(20);
    
    tftSpi = new SPIClass(HSPI);
    tftSpi->begin(TFT_SCLK, -1, TFT_MOSI, TFT_CS);
    
    tft = new Adafruit_ST7789(tftSpi, TFT_CS, TFT_DC, TFT_RST);
    
    tft->init(TFT_HEIGHT, TFT_WIDTH);
    tft->setRotation(1);
    tft->fillScreen(COLOR_BG);
    
    pinMode(TFT_LED_EN, OUTPUT);
    digitalWrite(TFT_LED_EN, HIGH);
    
    displayNeedsFullRedraw = true;
}

void drawStaticUI() {
    tft->fillScreen(COLOR_BG);
    
    // Header bar
    tft->fillRect(0, 0, TFT_WIDTH, 24, COLOR_HEADER);
    tft->setTextColor(COLOR_TEXT);
    tft->setTextSize(2);
    tft->setCursor(10, 4);
    tft->print("RAPTORHAB MODEM");
    
    // Divider line
    tft->drawFastHLine(0, 25, TFT_WIDTH, COLOR_ACCENT);
    
    // Radio Settings Section
    tft->setTextSize(1);
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(5, 32);
    tft->print("RADIO SETTINGS");
    
    tft->drawFastHLine(0, 42, TFT_WIDTH, 0x4208);
    
    // Settings labels (left column)
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(5, 48);
    tft->print("FREQ:");
    tft->setCursor(5, 60);
    tft->print("BR:");
    tft->setCursor(5, 72);
    tft->print("DEV:");
    
    // Settings labels (right column)
    tft->setCursor(110, 48);
    tft->print("BW:");
    tft->setCursor(110, 60);
    tft->print("PRE:");
    tft->setCursor(110, 72);
    tft->print("CFG:");
    
    // Settings values (left column)
    tft->setTextColor(COLOR_VALUE);
    tft->setCursor(35, 48);
    tft->printf("%.1f MHz", rfFrequency);
    tft->setCursor(25, 60);
    tft->printf("%.0f kbps", rfBitrate);
    tft->setCursor(30, 72);
    tft->printf("%.0f kHz", rfDeviation);
    
    // Settings values (right column)
    tft->setCursor(130, 48);
    tft->printf("%.0f kHz", rfRxBandwidth);
    tft->setCursor(135, 60);
    tft->printf("%d bits", rfPreambleLen);
    tft->setCursor(135, 72);
    tft->print("USB");

    // Divider
    tft->drawFastHLine(0, 85, TFT_WIDTH, 0x4208);

    // Signal section header
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(5, 90);
    tft->print("SIGNAL");

    // Stats section header
    tft->setCursor(5, 135);
    tft->print("STATISTICS");
    
    displayNeedsFullRedraw = false;
}

void updateSignalDisplay() {
    // Only update if values changed
    if (lastRssi == prevRssi && lastSnr == prevSnr) {
        return;
    }

    // Clear signal value area
    tft->fillRect(5, 100, 310, 30, COLOR_BG);

    // RSSI
    tft->setTextSize(2);
    uint16_t rssiColor = lastRssi > -80 ? COLOR_GOOD : (lastRssi > -100 ? COLOR_WARN : COLOR_BAD);
    tft->setTextColor(rssiColor);
    tft->setCursor(5, 105);
    tft->printf("%.0f", lastRssi);
    tft->setTextSize(1);
    tft->print(" dBm");

    // USB status indicator
    tft->setTextSize(1);
    tft->setTextColor(COLOR_GOOD);
    tft->setCursor(200, 105);
    tft->print("USB ACTIVE");

    prevRssi = lastRssi;
    prevSnr = lastSnr;
}

void updateStatsDisplay() {
    // Only update periodically
    static uint32_t lastUpdate = 0;
    if (millis() - lastUpdate < 500) return;
    lastUpdate = millis();
    
    // Only update if values changed -- including the live rate, so a stream
    // that stops shows 0.0 rather than freezing at its last figure.
    static float prevRateShown = -1.0f;
    float pktRate = currentPacketRate();
    if (packetsForwarded == prevPacketsForwarded && packetsTotal == prevPacketsTotal
            && pktRate == prevRateShown) {
        return;
    }
    prevRateShown = pktRate;
    
    // Clear stats value area
    tft->fillRect(5, 145, 310, 25, COLOR_BG);
    
    // Stats row 1
    tft->setTextSize(1);
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(5, 147);
    tft->print("RX:");
    tft->setTextColor(COLOR_VALUE);
    tft->printf("%lu", packetsTotal);
    
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(70, 147);
    tft->print("FWD:");
    tft->setTextColor(COLOR_GOOD);
    tft->printf("%lu", packetsForwarded);
    
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(140, 147);
    tft->print("ERR:");
    tft->setTextColor(packetsRejectedCrc + packetsRejectedNoRapt > 0 ? COLOR_BAD : COLOR_VALUE);
    tft->printf("%lu", packetsRejectedCrc + packetsRejectedNoRapt);
    
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(210, 147);
    tft->print("PKT/S:");
    tft->setTextColor(pktRate > 0 ? COLOR_GOOD : COLOR_VALUE);
    tft->printf("%.0f", pktRate);
    
    
    // Stats row 2 - packet sizes
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(5, 159);
    tft->print("TELEM:");
    tft->setTextColor(COLOR_VALUE);
    tft->printf("%lu", packetsSmall);

    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(80, 159);
    tft->print("IMAGE:");
    tft->setTextColor(COLOR_VALUE);
    tft->printf("%lu", packetsLarge);

    // Output indicator
    tft->setCursor(160, 159);
    tft->setTextColor(COLOR_LABEL);
    tft->print("OUT:");
    tft->setTextColor(COLOR_GOOD);
    tft->print("USB");

    // Preambles that never became packets. Zero is healthy: on frequency,
    // every preamble is consumed by the packet that follows it. A climbing
    // count with the packet rate at zero is the signature of being off
    // frequency -- a drifting TCXO in the cold, or a mistuned payload --
    // and it is what the SX1262 can actually measure in GFSK, where it
    // reports no frequency error at all.
    tft->setCursor(215, 159);
    tft->setTextColor(COLOR_LABEL);
    tft->print("PRE:");
    bool deaf = preamblesDetected > 0 && pktRate <= 0.0f;
    tft->setTextColor(deaf ? COLOR_BAD
                           : (preamblesDetected > 0 ? COLOR_WARN : COLOR_VALUE));
    tft->printf("%lu", preamblesDetected);


    prevPacketsForwarded = packetsForwarded;
    prevPacketsTotal = packetsTotal;
}

// ============================================================================
// Battery Monitoring
// ============================================================================

float readBatteryVoltage() {
#if VBAT_READ_PIN < 0
    // No divider fitted on this board. Saying so is more useful than returning
    // a number derived from a floating pin.
    return NAN;
#else

#ifdef ADC_CTRL_PIN
    // Enable the battery voltage divider.
    //
    // This used to write HIGH unconditionally, which is correct on the V4 and
    // T190 and wrong on the V3 and Wireless Stick Lite, where the enable is
    // active LOW -- Heltec changed the polarity between revisions. On those
    // boards it would leave the divider off and read the battery as flat.
    pinMode(ADC_CTRL_PIN, OUTPUT);
    digitalWrite(ADC_CTRL_PIN, ADC_CTRL_ON_STATE);
    delayMicroseconds(100);  // Let it settle (very brief, won't affect packet timing)
#endif

    // Take multiple readings and average for stability
    uint32_t sum = 0;
    const int samples = 4;
    for (int i = 0; i < samples; i++) {
        sum += analogRead(VBAT_READ_PIN);
    }

#ifdef ADC_CTRL_PIN
    // Turn off the divider to save power
    digitalWrite(ADC_CTRL_PIN, !ADC_CTRL_ON_STATE);
#endif

    // Calculate voltage
    // ESP32-S3 ADC: 12-bit (0-4095), default attenuation gives ~0-2.5V range
    // With ADC_ATTEN_DB_11, range is ~0-3.3V
    float avgRaw = (float)sum / samples;
    float vRead = (avgRaw / 4095.0f) * 3.3f;
    float vBat = vRead * VBAT_DIVIDER_RATIO;

    return vBat;
#endif
}

void updateBatteryDisplay() {
    // Only update periodically (same rate as stats)
    static uint32_t lastBatteryUpdate = 0;
    if (millis() - lastBatteryUpdate < 1000) return;
    lastBatteryUpdate = millis();
    
    // Read battery voltage
    batteryVoltage = readBatteryVoltage();
    if (isnan(batteryVoltage)) return;   // board has no divider fitted

    // Calculate percentage (linear approximation between min and max)
    batteryPercent = (int)(((batteryVoltage - VBAT_MIN) / (VBAT_MAX - VBAT_MIN)) * 100.0f);
    batteryPercent = constrain(batteryPercent, 0, 100);
    
    // Only redraw if voltage changed significantly (>0.05V)
    if (abs(batteryVoltage - prevBatteryVoltage) < 0.05f) {
        return;
    }
    prevBatteryVoltage = batteryVoltage;
    
    // Draw battery indicator in header bar (right side)
    // Clear battery area first
    tft->fillRect(250, 2, 68, 20, COLOR_HEADER);
    
    // Choose color based on level
    uint16_t battColor;
    if (batteryPercent > 50) {
        battColor = COLOR_GOOD;
    } else if (batteryPercent > 20) {
        battColor = COLOR_WARN;
    } else {
        battColor = COLOR_BAD;
    }
    
    // Draw battery icon outline (small rectangle with nub)
    int battX = 252;
    int battY = 5;
    int battW = 24;
    int battH = 12;
    tft->drawRect(battX, battY, battW, battH, COLOR_TEXT);
    tft->fillRect(battX + battW, battY + 3, 2, 6, COLOR_TEXT);  // Battery nub
    
    // Fill battery level
    int fillW = (battW - 4) * batteryPercent / 100;
    if (fillW > 0) {
        tft->fillRect(battX + 2, battY + 2, fillW, battH - 4, battColor);
    }
    
    // Draw voltage text
    tft->setTextSize(1);
    tft->setTextColor(battColor);
    tft->setCursor(280, 8);
    tft->printf("%.2fV", batteryVoltage);
}

void updateDisplay() {
    uint32_t now = millis();
    
    // Only update display during idle periods
    if (now - lastPacketTime < DISPLAY_IDLE_THRESHOLD_MS) {
        return;
    }
    
    // Rate limit display updates
    if (now - lastDisplayUpdate < DISPLAY_UPDATE_INTERVAL_MS) {
        return;
    }
    lastDisplayUpdate = now;
    
    if (displayNeedsFullRedraw) {
        drawStaticUI();
    }
    
    updateSignalDisplay();
    updateStatsDisplay();
    updateBatteryDisplay();
}

void showWaitingScreen() {
    tft->fillScreen(COLOR_BG);

    tft->setTextColor(COLOR_ACCENT);
    tft->setTextSize(2);
    tft->setCursor(20, 20);
    tft->print("RAPTORHAB MODEM");

    tft->setTextColor(COLOR_TEXT);
    tft->setTextSize(1);
    tft->setCursor(20, 50);
    tft->print("Waiting for configuration...");

    tft->setCursor(20, 70);
    tft->print("Connect via USB serial");

    // Default settings info
    tft->setTextColor(COLOR_WARN);
    tft->setCursor(20, 100);
    tft->print("Default: 915MHz, 96kbps");

    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(20, 120);
    tft->printf("Timeout: %ds", CONFIG_TIMEOUT_MS / 1000);
}

void showConfiguredScreen() {
    displayNeedsFullRedraw = true;
    drawStaticUI();
}

#endif  // BOARD_HAS_TFT

#if BOARD_HAS_OLED

// ---------------------------------------------------------------------------
// SSD1306, 128x64. Small enough that the whole frame is redrawn each time --
// there is no partial-update complexity to justify at this size.
// ---------------------------------------------------------------------------

static Adafruit_SSD1306* oled = nullptr;

void initDisplay() {
#ifdef VEXT_CTRL_PIN
    // The Heltec boards gate the panel's supply; the T3-S3 does not have the
    // rail at all. Polarity differs between boards, hence the per-board
    // VEXT_ON_STATE rather than a literal here.
    pinMode(VEXT_CTRL_PIN, OUTPUT);
    digitalWrite(VEXT_CTRL_PIN, VEXT_ON_STATE);
    delay(50);
#endif

#ifdef OLED_RST
    pinMode(OLED_RST, OUTPUT);
    digitalWrite(OLED_RST, LOW);  delay(20);
    digitalWrite(OLED_RST, HIGH); delay(50);
#endif

    Wire.begin(OLED_SDA, OLED_SCL);
    oled = new Adafruit_SSD1306(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);
    if (!oled->begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS)) {
        Serial.println("[TFT] SSD1306 not found; continuing without a display");
        delete oled;
        oled = nullptr;
        return;
    }
    oled->clearDisplay();
    oled->setTextColor(SSD1306_WHITE);
    oled->display();
}

void showWaitingScreen() {
    if (!oled) return;
    oled->clearDisplay();
    oled->setTextSize(1);
    oled->setCursor(0, 0);
    oled->println("RAPTORHAB MODEM");
    oled->drawFastHLine(0, 10, OLED_WIDTH, SSD1306_WHITE);
    oled->setCursor(0, 16);
    oled->println("Waiting for config");
    oled->setCursor(0, 28);
    oled->println("over USB serial...");
    oled->display();
}

void updateDisplay() {
    if (!oled) return;
    if (millis() - lastPacketTime < DISPLAY_IDLE_THRESHOLD_MS) return;
    if (millis() - lastDisplayUpdate < DISPLAY_UPDATE_INTERVAL_MS) return;
    lastDisplayUpdate = millis();

    oled->clearDisplay();
    oled->setTextSize(1);

    oled->setCursor(0, 0);
    oled->printf("%.1fMHz %.0fk", rfFrequency, rfBitrate);
    oled->drawFastHLine(0, 10, OLED_WIDTH, SSD1306_WHITE);

    oled->setCursor(0, 14);
    oled->printf("RSSI %6.1f dBm", lastRssi);
    oled->setCursor(0, 24);
    oled->printf("PKT/S %6.1f", currentPacketRate());

    oled->setCursor(0, 36);
    oled->printf("RX %lu  FWD %lu", packetsTotal, packetsForwarded);
    oled->setCursor(0, 46);
    oled->printf("CRC %lu  PRE %lu", packetsRejectedCrc, preamblesDetected);

    oled->setCursor(0, 56);
    if (isnan(batteryVoltage) || batteryVoltage <= 0.0f) {
        oled->print("USB powered");
    } else {
        oled->printf("%.2fV %d%%", batteryVoltage, batteryPercent);
    }
    oled->display();
}

// The TFT splits its redraw into regions to avoid flicker. At 128x64 the whole
// frame is cheap, so these exist only to satisfy the shared API.
void drawStaticUI() {}
void updateSignalDisplay() {}
void updateStatsDisplay() {}
void updateBatteryDisplay() {}

void showConfiguredScreen() {
    if (!oled) return;
    oled->clearDisplay();
    oled->setTextSize(1);
    oled->setCursor(0, 0);
    oled->println("CONFIGURED");
    oled->drawFastHLine(0, 10, OLED_WIDTH, SSD1306_WHITE);
    oled->setCursor(0, 16);
    oled->printf("%.3f MHz", rfFrequency);
    oled->setCursor(0, 28);
    oled->printf("%.0f kbps dev %.0f", rfBitrate, rfDeviation);
    oled->setCursor(0, 40);
    oled->printf("BW %.1f pre %d", rfRxBandwidth, rfPreambleLen);
    oled->setCursor(0, 54);
    oled->println("Listening...");
    oled->display();
}

#elif !BOARD_HAS_DISPLAY

// A headless build. The modem's job is the radio link, and it does all of it
// over USB; the panel was only ever a convenience.
void initDisplay() {}
void showWaitingScreen() {}
void updateDisplay() {}
void drawStaticUI() {}
void updateSignalDisplay() {}
void updateStatsDisplay() {}
void updateBatteryDisplay() {}
void showConfiguredScreen() {}

#endif

// ============================================================================
// Configuration Waiting
// ============================================================================

void saveConfiguration() {
    prefs.begin(CFG_NAMESPACE, false);
    prefs.putUChar("ver", CFG_VERSION);
    prefs.putFloat("freq", rfFrequency);
    prefs.putFloat("br", rfBitrate);
    prefs.putFloat("dev", rfDeviation);
    prefs.putFloat("bw", rfRxBandwidth);
    prefs.putUShort("pre", rfPreambleLen);
    prefs.end();
    Serial.println("[CONFIG] Saved to flash");
}

bool loadConfiguration() {
    prefs.begin(CFG_NAMESPACE, true);
    uint8_t ver = prefs.getUChar("ver", 0);
    if (ver != CFG_VERSION) {
        prefs.end();
        return false;
    }
    rfFrequency    = prefs.getFloat("freq", DEFAULT_FREQUENCY);
    rfBitrate      = prefs.getFloat("br",   DEFAULT_BITRATE);
    rfDeviation    = prefs.getFloat("dev",  DEFAULT_DEVIATION);
    rfRxBandwidth  = prefs.getFloat("bw",   DEFAULT_RX_BANDWIDTH);
    rfPreambleLen  = prefs.getUShort("pre", DEFAULT_PREAMBLE_LEN);
    prefs.end();

    Serial.printf("[CONFIG] Restored from flash: Freq=%.1f BR=%.1f Dev=%.1f BW=%.1f Pre=%d\n",
                  rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, rfPreambleLen);
    return true;
}

// CFG: used to be accepted only during the boot window, so a modem that had
// already started listening would silently ignore reconfiguration. Accepting
// it at any time means the app can retune a running modem.

void handleUsbCommands() {
    static String usbBuffer = "";
    while (Serial.available()) {
        char c = Serial.read();
        if (c != '\n' && c != '\r') {
            if (usbBuffer.length() < MAX_COMMAND_LEN) {
                usbBuffer += c;
            } else {
                // Overlong line: drop it and resynchronise on the next newline
                // rather than keep appending.
                usbBuffer = "";
            }
            continue;
        }
        if (usbBuffer.length() == 0) continue;

        if (usbBuffer.startsWith("CFG:")) {
            // Keep the settings that are currently working, so a rejected
            // reconfiguration can be undone rather than merely reported.
            float prevFreq = rfFrequency, prevBitrate = rfBitrate;
            float prevDev = rfDeviation, prevBw = rfRxBandwidth;
            int   prevPre = rfPreambleLen;

            if (parseConfigCommand(usbBuffer)) {
                // The app sends its configuration on every connect. When
                // nothing changed, reinitialising anyway costs a deaf window
                // mid-stream and a flash write per connect -- for nothing.
                if (configured
                        && rfFrequency == prevFreq && rfBitrate == prevBitrate
                        && rfDeviation == prevDev && rfRxBandwidth == prevBw
                        && rfPreambleLen == prevPre) {
                    Serial.printf("CFG_OK:%.1f,%.1f,%.1f,%.1f,%d\n",
                                  rfFrequency, rfBitrate, rfDeviation,
                                  rfRxBandwidth, rfPreambleLen);
                    usbBuffer = "";
                    continue;
                }
                Serial.println("[RADIO] Reconfiguring for new settings...");

                // The return value used to be discarded. A setting that passes
                // the range checks here can still be refused by the radio --
                // an rxBandwidth the SX1262 does not offer, for instance -- and
                // the modem was then left deaf, having already answered CFG_OK.
                // This is the same failure the ground station spent a long time
                // chasing the last time it went silent.
                if (configureRadio()) {
                    saveConfiguration();
                    Serial.printf("CFG_OK:%.1f,%.1f,%.1f,%.1f,%d\n",
                                  rfFrequency, rfBitrate, rfDeviation,
                                  rfRxBandwidth, rfPreambleLen);
                } else {
                    rfFrequency = prevFreq; rfBitrate = prevBitrate;
                    rfDeviation = prevDev;  rfRxBandwidth = prevBw;
                    rfPreambleLen = prevPre;

                    if (configureRadio()) {
                        Serial.println("CFG_ERR:Radio refused the settings; "
                                       "the previous ones are back");
                    } else {
                        // Nothing left to fall back to. Say so loudly rather
                        // than sit there looking connected and hearing nothing.
                        Serial.println("CFG_ERR:Radio refused the settings and "
                                       "would not restore the old ones; modem "
                                       "is deaf until reset");
                    }
                }
            } else {
                Serial.println("CFG_ERR:Invalid parameters");
            }
        }
        usbBuffer = "";
    }
}

bool waitForConfiguration() {
    showWaitingScreen();

    Serial.println("\n[CONFIG] Waiting for configuration via USB...");
    Serial.printf("[CONFIG] Send: CFG:<freq>,<bitrate>,<deviation>,<bandwidth>,<preamble>\n");
    Serial.printf("[CONFIG] Example: CFG:915.0,96.0,50.0,467.0,32\n");
    Serial.printf("[CONFIG] Timeout: %d seconds (will use defaults)\n\n", CONFIG_TIMEOUT_MS / 1000);

    String usbBuffer = "";
    uint32_t startTime = millis();
    uint32_t lastDot = 0;

    while (millis() - startTime < CONFIG_TIMEOUT_MS) {
        // Check USB Serial
        while (Serial.available()) {
            char c = Serial.read();
            if (c != '\n' && c != '\r') {
                if (usbBuffer.length() < MAX_COMMAND_LEN) {
                    usbBuffer += c;
                } else {
                    // Overlong line: drop it and resynchronise on the next
                    // newline rather than keep appending.
                    usbBuffer = "";
                }
                continue;
            }
            {
                if (usbBuffer.length() > 0) {
                    Serial.printf("[USB] Received: %s\n", usbBuffer.c_str());
                    if (usbBuffer.startsWith("CFG:")) {
                        if (parseConfigCommand(usbBuffer)) {
                            saveConfiguration();
                            Serial.printf("CFG_OK:%.1f,%.1f,%.1f,%.1f,%d\n",
                                         rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, rfPreambleLen);
                            return true;
                        } else {
                            Serial.println("CFG_ERR:Invalid parameters");
                        }
                    }
                    usbBuffer = "";
                }
            }
        }

        // Progress indicator
        if (millis() - lastDot > 1000) {
            lastDot = millis();
            Serial.print(".");

            // Update display with countdown
            int remaining = (CONFIG_TIMEOUT_MS - (millis() - startTime)) / 1000;
#if BOARD_HAS_TFT
            tft->fillRect(100, 120, 50, 10, COLOR_BG);
            tft->setTextColor(COLOR_LABEL);
            tft->setCursor(100, 120);
            tft->printf("%ds", remaining);
#else
            (void)remaining;   // the other panels are not redrawn per second
#endif
        }

        delay(10);
    }

    Serial.println("\n[CONFIG] Timeout - using defaults");
    return false;
}

bool parseConfigCommand(const String& cmd) {
    // Expected: CFG:<freq>,<bitrate>,<deviation>,<bandwidth>,<preamble>
    if (!cmd.startsWith("CFG:")) return false;
    
    String params = cmd.substring(4);
    int comma1 = params.indexOf(',');
    int comma2 = params.indexOf(',', comma1 + 1);
    int comma3 = params.indexOf(',', comma2 + 1);
    int comma4 = params.indexOf(',', comma3 + 1);
    
    if (comma1 < 0 || comma2 < 0 || comma3 < 0 || comma4 < 0) {
        Serial.println("[CONFIG] Parse error: missing commas");
        return false;
    }
    
    float freq = params.substring(0, comma1).toFloat();
    float bitrate = params.substring(comma1 + 1, comma2).toFloat();
    float deviation = params.substring(comma2 + 1, comma3).toFloat();
    float bandwidth = params.substring(comma3 + 1, comma4).toFloat();
    int preamble = params.substring(comma4 + 1).toInt();
    
    // Validate
    if (freq < 150.0 || freq > 960.0) {
        Serial.printf("[CONFIG] Invalid frequency: %.1f\n", freq);
        return false;
    }
    if (bitrate < 1.0 || bitrate > 300.0) {
        Serial.printf("[CONFIG] Invalid bitrate: %.1f\n", bitrate);
        return false;
    }
    if (deviation < 1.0 || deviation > 200.0) {
        Serial.printf("[CONFIG] Invalid deviation: %.1f\n", deviation);
        return false;
    }
    if (bandwidth < 10.0 || bandwidth > 500.0) {
        Serial.printf("[CONFIG] Invalid bandwidth: %.1f\n", bandwidth);
        return false;
    }
    if (preamble < 8 || preamble > 65535) {
        Serial.printf("[CONFIG] Invalid preamble: %d\n", preamble);
        return false;
    }
    
    rfFrequency = freq;
    rfBitrate = bitrate;
    rfDeviation = deviation;
    rfRxBandwidth = bandwidth;
    rfPreambleLen = preamble;
    
    Serial.printf("[CONFIG] Applied: Freq=%.1f BR=%.1f Dev=%.1f BW=%.1f Pre=%d\n",
                  rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, rfPreambleLen);
    
    return true;
}

// ============================================================================
// Radio Initialization
// ============================================================================

// Configure (or reconfigure) the radio that initializeRadio() constructed.
// Safe to call while running -- beginFSK resets the chip -- and it always
// leaves the radio listening. This is deliberately separate from
// construction: the runtime CFG: path used to call initializeRadio(), which
// built a second SPIClass on the same live bus and orphaned the object the
// interrupt was attached to. The radio then sat in standby forever -- the
// modem froze the moment the app sent its configuration, every time it was
// reconfigured outside the boot window.
bool configureRadio() {

#ifdef RF_SWITCH_RX_PIN
    // Boards with an external antenna switch rather than one driven from DIO2.
    // Set before begin(), because begin() configures the switch. Miss this and
    // the radio transmits and receives into a terminated path: it appears to
    // work, and hears almost nothing, which looks exactly like a bad antenna.
    radio->setRfSwitchPins(RF_SWITCH_RX_PIN, RF_SWITCH_TX_PIN);
    Serial.printf("[RADIO] External RF switch on RX pin %d\n", RF_SWITCH_RX_PIN);
#endif
    
    Serial.printf("[RADIO] Initializing FSK: Freq=%.1f BR=%.1f Dev=%.1f BW=%.1f Pre=%d\n",
                  rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, rfPreambleLen);
    
    int state = radio->beginFSK(rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, 10,
                                rfPreambleLen, LORA_TCXO_VOLTAGE, false);
    
    if (state != RADIOLIB_ERR_NONE) {
        Serial.printf("[ERROR] FSK init failed: %d\n", state);
        return false;
    }
    
    radio->setSyncWord(const_cast<uint8_t*>(SYNC_WORD), SYNC_WORD_LEN);
    radio->variablePacketLengthMode(MAX_PACKET_SIZE);
    radio->setDataShaping(RF_DATA_SHAPING);
    radio->setCRC(0);
    // Whitening, with the SX1262's default 0x01FF seed. This must match the
    // payload exactly -- a mismatch does not degrade the link, it removes it
    // (the sync word is not whitened, so packets arrive and then fail every
    // content check). Measured on the bench: 0.916% bad CRC without it, 0.000%
    // with it, over 16690 packets. See docs/REVIEW.md.
    radio->setWhitening(true, 0x01FF);
    
    radio->setDio1Action(onPacketReceived);
    // Preamble and sync-word IRQs are enabled but deliberately NOT mapped to
    // DIO1: they latch in the status register for the loop to count, without
    // adding an interrupt per preamble to the path that carries the imagery.
    radio->startReceive(
        RADIOLIB_SX126X_RX_TIMEOUT_INF,
        RADIOLIB_SX126X_IRQ_RX_DEFAULT
            | RADIOLIB_SX126X_IRQ_PREAMBLE_DETECTED
            | RADIOLIB_SX126X_IRQ_SYNC_WORD_VALID,
        RADIOLIB_SX126X_IRQ_RX_DONE);
    
    Serial.println("[RADIO] SX1262 initialized successfully");
    return true;
}

bool initializeRadio() {
    Serial.println("[RADIO] Initializing SX1262...");

    // Construction happens exactly once. Reconfiguration goes through
    // configureRadio(), which reuses these objects.
    if (spi == nullptr) {
        spi = new SPIClass(FSPI);
        spi->begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_NSS);

        Module* mod = new Module(LORA_NSS, LORA_DIO1, LORA_RST, LORA_BUSY, *spi);
        radio = new SX1262Funnel(mod);
    }

    return configureRadio();
}

// ============================================================================
// Setup
// ============================================================================

void setup() {
    // The hardware CDC TX ring defaults to 256 bytes -- smaller than one
    // stuffed image frame -- so at any real packet rate most writes found the
    // ring already holding a frame and dropped. Size it to ride out a burst:
    // 16 KB is under a second of traffic at the payload's peak rate.
    Serial.setTxBufferSize(16384);
    Serial.begin(SERIAL_BAUD);
    // Never block the main loop on a host that has stopped reading. The radio
    // is serviced from the same loop, so a stalled write makes the modem deaf
    // as well as silent. Dropping is the right failure here: the ground
    // station reconstructs images from any sufficient subset of packets.
    Serial.setTxTimeoutMs(0);
    delay(1000);

    Serial.println("\n========================================");
    Serial.println("RaptorHab Ground Station Bridge");
    Serial.println("Heltec Vision Master T190");
    Serial.println("USB Serial Output Only");
    Serial.println("========================================\n");

    pinMode(USER_BUTTON, INPUT_PULLUP);

    // Initialize battery monitoring pins
#ifdef ADC_CTRL_PIN
    pinMode(ADC_CTRL_PIN, OUTPUT);
    digitalWrite(ADC_CTRL_PIN, !ADC_CTRL_ON_STATE);  // divider off to save power
#endif
    analogReadResolution(12);          // 12-bit ADC (0-4095)
    analogSetAttenuation(ADC_11db);    // Full 0-3.3V range

    // Initialize display
    Serial.println("[TFT] Initializing display...");
    initDisplay();
    Serial.println("[TFT] Display initialized");

    // A stored configuration means the modem can come straight up listening.
    // Only a modem that has never been configured blocks waiting for the app.
    if (loadConfiguration()) {
        Serial.println("[CONFIG] Using stored configuration; send CFG: at any time to change it");
    } else {
        // Only a configuration the host actually sent is persisted -- saving
        // the fallback defaults here would mean a modem that timed out once
        // never waits for the app again, and never learns the right settings.
        waitForConfiguration();
    }
    configured = true;

    // Initialize radio
    bool radioUp = initializeRadio();
    if (!radioUp) {
        // A stored configuration the radio refuses would brick the modem:
        // every reboot reloads the same bad settings and fails the same way,
        // and only reflashing recovers it. Erase it and try the defaults
        // before giving up.
        Serial.println("[ERROR] Radio refused the stored settings; clearing them and trying defaults");
        prefs.begin(CFG_NAMESPACE, false);
        prefs.clear();
        prefs.end();
        rfFrequency   = DEFAULT_FREQUENCY;
        rfBitrate     = DEFAULT_BITRATE;
        rfDeviation   = DEFAULT_DEVIATION;
        rfRxBandwidth = DEFAULT_RX_BANDWIDTH;
        rfPreambleLen = DEFAULT_PREAMBLE_LEN;
        radioUp = (radio != nullptr) && configureRadio();
    }
    if (!radioUp) {
        Serial.println("[ERROR] Radio initialization failed!");

#if BOARD_HAS_TFT
        tft->fillScreen(COLOR_BAD);
        tft->setTextColor(COLOR_TEXT);
        tft->setTextSize(2);
        tft->setCursor(20, 70);
        tft->print("RADIO INIT FAILED!");
#endif

        while (1) {
            Serial.println("[ERROR] Radio init failed - please reset");
            delay(5000);
        }
    }

    showConfiguredScreen();

    Serial.printf("\n[CONFIG] Freq:%.1f BR:%.0f Dev:%.0f BW:%.0f Preamble:%d\n",
                  rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, rfPreambleLen);
    Serial.println("[READY] Listening for packets...");
    Serial.println("[USB] Packets will be forwarded via USB serial");

    lastPacketTime = millis();
    lastDisplayUpdate = millis();
    lastStatsDisplayUpdate = millis();
}

// ============================================================================
// Main Loop
// ============================================================================

void loop() {
    // Handle incoming packets with highest priority
    if (packetReceived) {
        packetReceived = false;
        handlePacket();
        lastPacketTime = millis();
    }

    // Sample the latched receive IRQs. Cheap, and only the two bits we
    // count are cleared -- clearing RX_DONE here would race the interrupt
    // that carries every packet.
    sampleLinkFunnel();

    // Accept reconfiguration at any time
    handleUsbCommands();

    // Send stats every 10 seconds
    sendStats();

    // Update display during idle periods
    updateDisplay();
}

void sampleLinkFunnel() {
    static uint32_t lastSample = 0;
    if (radio == nullptr || millis() - lastSample < 20) return;
    lastSample = millis();

    uint16_t irq = radio->getIrqStatus();
    uint16_t clear = 0;
    if (irq & RADIOLIB_SX126X_IRQ_PREAMBLE_DETECTED) {
        preamblesDetected++;
        clear |= RADIOLIB_SX126X_IRQ_PREAMBLE_DETECTED;
    }
    if (irq & RADIOLIB_SX126X_IRQ_SYNC_WORD_VALID) {
        syncWordsMatched++;
        clear |= RADIOLIB_SX126X_IRQ_SYNC_WORD_VALID;
    }
    // Only these two bits. Clearing RX_DONE here would race the interrupt
    // that carries every packet.
    if (clear) radio->clearIrq(clear);
}

// ============================================================================
// Statistics Reporting
// ============================================================================

void sendStats() {
    if (millis() - lastStatsTime < 10000) return;
    lastStatsTime = millis();

    float rate = usbOffered > 0 ? (100.0 * packetsForwarded / usbOffered) : 100.0;

    char battBuf[24];
    if (isnan(batteryVoltage) || batteryVoltage <= 0.0f) {
        snprintf(battBuf, sizeof(battBuf), "n/a");
    } else {
        snprintf(battBuf, sizeof(battBuf), "%.2fV(%d%%)", batteryVoltage, batteryPercent);
    }

    char statsBuf[256];
    snprintf(statsBuf, sizeof(statsBuf),
        "\n[STATS] Total:%lu Fwd:%lu NoRAPT:%lu BadCRC:%lu Err:%lu Rate:%.1f%% Drop:%lu Pre:%lu Sync:%lu Batt:%s\n",
        packetsTotal, packetsForwarded, packetsRejectedNoRapt, packetsRejectedCrc,
        packetsRadioError, rate, usbDropped, preamblesDetected, syncWordsMatched, battBuf);
    Serial.print(statsBuf);
}

// ============================================================================
// Packet Handling
// ============================================================================

void handlePacket() {
    uint8_t packet[MAX_PACKET_SIZE];
    
    int packetLen = radio->getPacketLength();
    if (packetLen <= 0 || packetLen > MAX_PACKET_SIZE) {
        radio->startReceive();
        return;
    }
    
    int state = radio->readData(packet, packetLen);
    lastRssi = radio->getRSSI();

    // Not radio->getSNR(). The SX1262 measures SNR only for LoRa: the figure
    // comes out of the LoRa packet-status registers, and RadioLib returns
    // RADIOLIB_ERR_WRONG_MODEM -- which is the integer -20 -- when the active
    // modem is FSK. That value was being stored as a float, printed on the
    // display as "-20.0 dB" in red, and forwarded over USB in every single
    // frame. Verified against the modem on the bench: 2281 frames out of 2281
    // reported exactly -20.0.
    //
    // GFSK has no SNR to report, so say so instead of inventing one.
    lastSnr = SNR_NOT_AVAILABLE;
    packetsTotal++;
    
    // IMMEDIATELY restart receive
    radio->startReceive();
    
    if (state != RADIOLIB_ERR_NONE) {
        packetsRadioError++;
        return;
    }
    
    // Validate packet starts with "RAPT"
    if (packetLen < 12 || 
        packet[0] != 0x52 || packet[1] != 0x41 || 
        packet[2] != 0x50 || packet[3] != 0x54) {
        packetsRejectedNoRapt++;
        return;
    }
    
    // Validate CRC32
    uint32_t receivedCrc = ((uint32_t)packet[packetLen-4] << 24) |
                           ((uint32_t)packet[packetLen-3] << 16) |
                           ((uint32_t)packet[packetLen-2] << 8) |
                           ((uint32_t)packet[packetLen-1]);
    uint32_t calculatedCrc = crc32(packet, packetLen - 4);
    
    if (receivedCrc != calculatedCrc) {
        packetsRejectedCrc++;
        return;
    }
    
    // Valid packet - forward via USB
    if (forwardPacket(packet, packetLen, lastRssi, lastSnr)) {
        packetsForwarded++;
    }
    
    // Track by size
    if (packetLen < 100) {
        packetsSmall++;
    } else {
        packetsLarge++;
    }
}

// ============================================================================
// USB Packet Forwarding
// ============================================================================

bool forwardPacket(uint8_t* data, int len, float rssi, float snr) {
    // Serial here is the ESP32-S3's hardware USB-Serial-JTAG (HWCDC). Its
    // write() waits on the TX ring buffer, so writing a frame one byte at a
    // time -- about 240 calls -- lets a host that has stopped reading stall
    // this function for as long as it takes every one of those calls to time
    // out. The radio is serviced from the same loop, so the whole modem goes
    // deaf: received-packet counters freeze and the display stops updating,
    // which is exactly what happened when the ground station app was closed
    // mid-stream. The frame is now built in memory and handed over in a
    // single write, with the TX timeout set to zero in setup() so a host that
    // is not draining costs us dropped bytes rather than a wedged modem.
    if (len <= 0 || len > 255) return false;

    uint8_t lenHi = (len >> 8) & 0xFF;
    uint8_t lenLo = len & 0xFF;
    int8_t rssiInt = (int8_t)rssi;
    uint8_t rssiFrac = (uint8_t)(abs(rssi - rssiInt) * 100);
    int8_t snrInt = (int8_t)snr;
    uint8_t snrFrac = (uint8_t)(abs(snr - snrInt) * 100);

    uint8_t checksum = lenHi ^ lenLo ^ (uint8_t)rssiInt ^ rssiFrac ^ (uint8_t)snrInt ^ snrFrac;
    for (int i = 0; i < len; i++) {
        checksum ^= data[i];
    }

    // Worst case every byte needs escaping: 2 delimiters + 2 * (6 header +
    // 255 data + 1 checksum).
    static uint8_t frame[2 + 2 * (6 + 255 + 1)];
    size_t n = 0;

    // 0x7B is the dual-radio modem's second frame delimiter. This board has
    // one radio and never opens a 0x7B frame, but it must still escape the
    // byte: a ground station that watches for both delimiters would otherwise
    // read a raw 0x7B in image data as the start of a frame. Escaping it here
    // keeps one wire format across every board, so the parser needs no
    // knowledge of which one it is talking to.
    auto stuff = [&](uint8_t b) {
        if (b == 0x7E)      { frame[n++] = 0x7D; frame[n++] = 0x5E; }
        else if (b == 0x7B) { frame[n++] = 0x7D; frame[n++] = 0x5B; }
        else if (b == 0x7D) { frame[n++] = 0x7D; frame[n++] = 0x5D; }
        else                { frame[n++] = b; }
    };

    frame[n++] = FRAME_DELIMITER;
    stuff(lenHi);
    stuff(lenLo);
    stuff((uint8_t)rssiInt);
    stuff(rssiFrac);
    stuff((uint8_t)snrInt);
    stuff(snrFrac);
    for (int i = 0; i < len; i++) stuff(data[i]);
    stuff(checksum);
    frame[n++] = FRAME_DELIMITER;

    // No host attached means nobody to send to -- not a failure, so it
    // neither counts as forwarded nor as dropped. Otherwise DROP climbs and
    // RATE sinks the whole time the modem sits unattended, and the numbers
    // read as a fault where there is none.
    if (!HWCDC::isConnected()) {
        return false;
    }
    usbOffered++;

    // A partial frame is worse than no frame: with the TX timeout at zero a
    // full ring makes write() return short, and the fragment it did queue is
    // garbage on the wire. Check space first and send whole or not at all.
    if (Serial.availableForWrite() < (int)n) {
        usbDropped++;
        return false;
    }
    if (Serial.write(frame, n) != n) {
        usbDropped++;
        return false;
    }
    return true;
}
