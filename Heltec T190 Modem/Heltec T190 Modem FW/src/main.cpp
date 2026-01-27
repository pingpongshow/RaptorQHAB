/*
 * RaptorHab Ground Station Bridge
 * Heltec Vision Master T190 (ESP32-S3 + SX1262)
 * 
 * Receives packets via SX1262 and forwards them over USB serial
 * Displays RSSI, SNR, and radio settings on 1.9" TFT LCD
 * 
 * CONFIGURATION MODE:
 *   On boot, modem waits for configuration from Mac app before starting.
 *   Config command: CFG:<freq>,<bitrate>,<deviation>,<bandwidth>,<preamble>\n
 *   Example: CFG:915.0,96.0,50.0,467.0,32\n
 *   Response: CFG_OK\n or CFG_ERR:<message>\n
 * 
 * Serial Protocol to Mac (after configuration):
 *   [0x7E][LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM][0x7E]
 * 
 * TFT Display:
 *   - Shows RSSI, SNR, packet counts, and radio settings
 *   - Updates only during idle periods (no packets for >750ms)
 *   - USB packet forwarding always has priority
 */

#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>

// ============================================================================
// Configuration
// ============================================================================

// Set to true to enable debug output (interferes with binary protocol!)
#define DEBUG_OUTPUT        false

// Pin Definitions - Heltec Vision Master T190 LoRa
#define LORA_NSS    8
#define LORA_SCK    9
#define LORA_MOSI   10
#define LORA_MISO   11
#define LORA_RST    12
#define LORA_BUSY   13
#define LORA_DIO1   14
#define USER_BUTTON 21

// Pin Definitions - TFT Display (ST7789V3)
#define TFT_CS      39
#define TFT_RST     40
#define TFT_DC      47   // RS pin
#define TFT_SCLK    38
#define TFT_MOSI    48   // SDA pin
#define TFT_LED_EN  17   // Backlight control (AW9364DNR)
#define TFT_PWR     7    // VTFT power control (LOW = enabled)

// Display dimensions (landscape orientation)
#define TFT_WIDTH   320
#define TFT_HEIGHT  170

// Default RF Configuration (used if no config received within timeout)
#define DEFAULT_FREQUENCY       915.0
#define DEFAULT_BITRATE         96.0      // 96 kbps
#define DEFAULT_DEVIATION       50.0      // 50 kHz
#define DEFAULT_RX_BANDWIDTH    467.0     // 467 kHz
#define DEFAULT_PREAMBLE_LEN    32        // 32 bits
#define RF_DATA_SHAPING         0.5

// Configuration timeout (ms) - wait this long for config before using defaults
#define CONFIG_TIMEOUT_MS       120000    // 2 minutes

// Display update configuration
#define DISPLAY_IDLE_THRESHOLD_MS   750   // Only update display if no packets for this long
#define DISPLAY_UPDATE_INTERVAL_MS  500   // Minimum time between display updates
#define DISPLAY_STATS_INTERVAL_MS   1000  // Update stats section this often

// Sync word "RAPT" 
const uint8_t SYNC_WORD[] = {0x52, 0x41, 0x50, 0x54};
#define SYNC_WORD_LEN       4

// Serial Protocol
#define FRAME_DELIMITER     0x7E
#define SERIAL_BAUD         921600
#define MAX_PACKET_SIZE     255

// Colors for display
#define COLOR_BG            ST77XX_BLACK
#define COLOR_HEADER        0x001F   // Dark blue
#define COLOR_TEXT          ST77XX_WHITE
#define COLOR_LABEL         0x8410   // Gray
#define COLOR_VALUE         ST77XX_CYAN
#define COLOR_GOOD          ST77XX_GREEN
#define COLOR_WARN          ST77XX_YELLOW
#define COLOR_BAD           ST77XX_RED
#define COLOR_ACCENT        0x07FF   // Cyan

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
// Runtime RF Configuration (set by Mac app or defaults)
// ============================================================================

float rfFrequency = DEFAULT_FREQUENCY;
float rfBitrate = DEFAULT_BITRATE;
float rfDeviation = DEFAULT_DEVIATION;
float rfRxBandwidth = DEFAULT_RX_BANDWIDTH;
uint16_t rfPreambleLen = DEFAULT_PREAMBLE_LEN;

bool configured = false;

// ============================================================================
// Global Objects
// ============================================================================

// LoRa Radio (uses FSPI)
SPIClass* spi = nullptr;
SX1262* radio = nullptr;

// TFT Display (uses HSPI)
SPIClass* tftSpi = nullptr;
Adafruit_ST7789* tft = nullptr;

volatile bool packetReceived = false;
uint32_t packetsTotal = 0;
uint32_t packetsForwarded = 0;
uint32_t packetsRejectedNoRapt = 0;
uint32_t packetsRejectedCrc = 0;
uint32_t packetsRadioError = 0;
uint32_t packetsSmall = 0;      // < 100 bytes (telemetry)
uint32_t packetsLarge = 0;      // >= 100 bytes (image data)
float lastRssi = -120.0;
float lastSnr = 0.0;

uint32_t lastStatsTime = 0;
uint32_t lastPacketTime = 0;
uint32_t lastDisplayUpdate = 0;
uint32_t lastStatsDisplayUpdate = 0;
bool displayNeedsFullRedraw = true;

// Previous values for partial display updates
float prevRssi = -999;
float prevSnr = -999;
uint32_t prevPacketsForwarded = 0;
uint32_t prevPacketsTotal = 0;

// ============================================================================
// CRC32 (IEEE 802.3 polynomial - same as protocol)
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
// Interrupt Handler
// ============================================================================

void IRAM_ATTR onPacketReceived() {
    packetReceived = true;
}

// ============================================================================
// Forward Declarations
// ============================================================================

void handlePacket();
void forwardPacket(uint8_t* data, int len, float rssi, float snr);
void sendStats();
bool waitForConfiguration();
bool parseConfigCommand(const String& cmd);
bool initializeRadio();
void initDisplay();
void drawStaticUI();
void updateDisplay();
void updateSignalDisplay();
void updateStatsDisplay();
void showWaitingScreen();
void showConfiguredScreen();

// ============================================================================
// Display Functions
// ============================================================================

void initDisplay() {
    // Enable TFT power
    pinMode(TFT_PWR, OUTPUT);
    digitalWrite(TFT_PWR, LOW);  // LOW enables power
    delay(20);
    
    // Initialize HSPI for TFT (separate from LoRa's FSPI)
    tftSpi = new SPIClass(HSPI);
    tftSpi->begin(TFT_SCLK, -1, TFT_MOSI, TFT_CS);
    
    // Create display object
    tft = new Adafruit_ST7789(tftSpi, TFT_CS, TFT_DC, TFT_RST);
    
    // Initialize display (170x320)
    tft->init(TFT_HEIGHT, TFT_WIDTH);  // Note: swapped for landscape
    tft->setRotation(1);  // Landscape mode
    tft->fillScreen(COLOR_BG);
    
    // Enable backlight
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
    tft->print("RAPTORHAB GROUND STATION");
    
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
    
    // Settings labels (right column)
    tft->setCursor(165, 48);
    tft->print("DEV:");
    tft->setCursor(165, 60);
    tft->print("BW:");
    
    // Settings values
    char buf[32];
    tft->setTextColor(COLOR_VALUE);
    
    tft->setCursor(45, 48);
    snprintf(buf, sizeof(buf), "%.3f MHz", rfFrequency);
    tft->print(buf);
    
    tft->setCursor(30, 60);
    snprintf(buf, sizeof(buf), "%.0f kbps", rfBitrate);
    tft->print(buf);
    
    tft->setCursor(195, 48);
    snprintf(buf, sizeof(buf), "%.0f kHz", rfDeviation);
    tft->print(buf);
    
    tft->setCursor(190, 60);
    snprintf(buf, sizeof(buf), "%.0f kHz", rfRxBandwidth);
    tft->print(buf);
    
    // Signal Section
    tft->drawFastHLine(0, 75, TFT_WIDTH, 0x4208);
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(5, 82);
    tft->print("SIGNAL QUALITY");
    
    // Signal labels
    tft->setTextSize(2);
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(5, 98);
    tft->print("RSSI:");
    tft->setCursor(165, 98);
    tft->print("SNR:");
    
    // Stats Section
    tft->drawFastHLine(0, 125, TFT_WIDTH, 0x4208);
    tft->setTextSize(1);
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(5, 132);
    tft->print("STATISTICS");
    
    // Stats labels
    tft->setCursor(5, 148);
    tft->print("PKT:");
    tft->setCursor(100, 148);
    tft->print("FWD:");
    tft->setCursor(200, 148);
    tft->print("RATE:");
    
    // Status bar
    tft->drawFastHLine(0, 160, TFT_WIDTH, 0x4208);
    tft->setTextColor(COLOR_GOOD);
    tft->setCursor(5, 164);
    tft->print("LISTENING...");
    
    displayNeedsFullRedraw = false;
}

void updateSignalDisplay() {
    // Only update if values changed significantly
    if (abs(lastRssi - prevRssi) < 0.5 && abs(lastSnr - prevSnr) < 0.5) {
        return;
    }
    
    char buf[16];
    
    // Clear previous RSSI value
    tft->fillRect(65, 98, 90, 20, COLOR_BG);
    
    // Draw new RSSI with color coding
    uint16_t rssiColor = COLOR_GOOD;
    if (lastRssi < -110) rssiColor = COLOR_BAD;
    else if (lastRssi < -90) rssiColor = COLOR_WARN;
    
    tft->setTextSize(2);
    tft->setTextColor(rssiColor);
    tft->setCursor(65, 98);
    snprintf(buf, sizeof(buf), "%.0f", lastRssi);
    tft->print(buf);
    tft->setTextSize(1);
    tft->setCursor(110, 105);
    tft->print("dBm");
    
    // Clear previous SNR value
    tft->fillRect(205, 98, 90, 20, COLOR_BG);
    
    // Draw new SNR with color coding
    uint16_t snrColor = COLOR_GOOD;
    if (lastSnr < 0) snrColor = COLOR_BAD;
    else if (lastSnr < 5) snrColor = COLOR_WARN;
    
    tft->setTextSize(2);
    tft->setTextColor(snrColor);
    tft->setCursor(205, 98);
    snprintf(buf, sizeof(buf), "%.1f", lastSnr);
    tft->print(buf);
    tft->setTextSize(1);
    tft->setCursor(260, 105);
    tft->print("dB");
    
    prevRssi = lastRssi;
    prevSnr = lastSnr;
}

void updateStatsDisplay() {
    // Only update if values changed
    if (packetsForwarded == prevPacketsForwarded && packetsTotal == prevPacketsTotal) {
        return;
    }
    
    char buf[32];
    tft->setTextSize(1);
    
    // Clear and update packet count
    tft->fillRect(30, 148, 60, 10, COLOR_BG);
    tft->setTextColor(COLOR_VALUE);
    tft->setCursor(30, 148);
    snprintf(buf, sizeof(buf), "%lu", packetsTotal);
    tft->print(buf);
    
    // Clear and update forwarded count
    tft->fillRect(130, 148, 60, 10, COLOR_BG);
    tft->setTextColor(COLOR_GOOD);
    tft->setCursor(130, 148);
    snprintf(buf, sizeof(buf), "%lu", packetsForwarded);
    tft->print(buf);
    
    // Clear and update success rate
    tft->fillRect(240, 148, 70, 10, COLOR_BG);
    float rate = packetsTotal > 0 ? (100.0 * packetsForwarded / packetsTotal) : 0.0;
    uint16_t rateColor = rate > 90 ? COLOR_GOOD : (rate > 50 ? COLOR_WARN : COLOR_BAD);
    tft->setTextColor(rateColor);
    tft->setCursor(240, 148);
    snprintf(buf, sizeof(buf), "%.1f%%", rate);
    tft->print(buf);
    
    prevPacketsForwarded = packetsForwarded;
    prevPacketsTotal = packetsTotal;
}

void updateDisplay() {
    uint32_t now = millis();
    
    // Check if we should update display
    // Only update if no packet received recently (USB priority)
    if (now - lastPacketTime < DISPLAY_IDLE_THRESHOLD_MS) {
        return;  // Too close to last packet, skip update
    }
    
    // Throttle update rate
    if (now - lastDisplayUpdate < DISPLAY_UPDATE_INTERVAL_MS) {
        return;
    }
    
    lastDisplayUpdate = now;
    
    // Full redraw if needed
    if (displayNeedsFullRedraw) {
        drawStaticUI();
    }
    
    // Always update signal section when display updates
    updateSignalDisplay();
    
    // Update stats periodically
    if (now - lastStatsDisplayUpdate > DISPLAY_STATS_INTERVAL_MS) {
        updateStatsDisplay();
        lastStatsDisplayUpdate = now;
    }
}

void showWaitingScreen() {
    tft->fillScreen(COLOR_BG);
    
    // Header
    tft->fillRect(0, 0, TFT_WIDTH, 24, COLOR_HEADER);
    tft->setTextColor(COLOR_TEXT);
    tft->setTextSize(2);
    tft->setCursor(10, 4);
    tft->print("RAPTORHAB GROUND STATION");
    
    tft->drawFastHLine(0, 25, TFT_WIDTH, COLOR_ACCENT);
    
    // Waiting message
    tft->setTextColor(COLOR_WARN);
    tft->setTextSize(2);
    tft->setCursor(20, 60);
    tft->print("WAITING FOR CONFIG");
    
    tft->setTextColor(COLOR_LABEL);
    tft->setTextSize(1);
    tft->setCursor(20, 90);
    tft->print("Connect Mac app or wait for defaults...");
    
    // Show default settings
    char buf[64];
    tft->setTextColor(COLOR_VALUE);
    tft->setCursor(20, 110);
    snprintf(buf, sizeof(buf), "Default: %.1f MHz, %.0f kbps", DEFAULT_FREQUENCY, DEFAULT_BITRATE);
    tft->print(buf);
}

void showConfiguredScreen() {
    displayNeedsFullRedraw = true;
    drawStaticUI();
}

// ============================================================================
// Configuration Parsing
// ============================================================================

bool parseConfigCommand(const String& cmd) {
    // Expected format: CFG:<freq>,<bitrate>,<deviation>,<bandwidth>,<preamble>
    // Example: CFG:915.0,96.0,50.0,467.0,32
    
    if (!cmd.startsWith("CFG:")) {
        return false;
    }
    
    String params = cmd.substring(4);  // Remove "CFG:"
    params.trim();
    
    // Parse comma-separated values
    int idx1 = params.indexOf(',');
    int idx2 = params.indexOf(',', idx1 + 1);
    int idx3 = params.indexOf(',', idx2 + 1);
    int idx4 = params.indexOf(',', idx3 + 1);
    
    if (idx1 < 0 || idx2 < 0 || idx3 < 0 || idx4 < 0) {
        Serial.println("CFG_ERR:Invalid format - expected CFG:freq,bitrate,deviation,bandwidth,preamble");
        return false;
    }
    
    float freq = params.substring(0, idx1).toFloat();
    float bitrate = params.substring(idx1 + 1, idx2).toFloat();
    float deviation = params.substring(idx2 + 1, idx3).toFloat();
    float bandwidth = params.substring(idx3 + 1, idx4).toFloat();
    int preamble = params.substring(idx4 + 1).toInt();
    
    // Validate ranges
    if (freq < 150.0 || freq > 960.0) {
        Serial.printf("CFG_ERR:Frequency %.1f out of range (150-960 MHz)\n", freq);
        return false;
    }
    if (bitrate < 1.0 || bitrate > 300.0) {
        Serial.printf("CFG_ERR:Bitrate %.1f out of range (1-300 kbps)\n", bitrate);
        return false;
    }
    if (deviation < 1.0 || deviation > 200.0) {
        Serial.printf("CFG_ERR:Deviation %.1f out of range (1-200 kHz)\n", deviation);
        return false;
    }
    if (bandwidth < 50.0 || bandwidth > 500.0) {
        Serial.printf("CFG_ERR:Bandwidth %.1f out of range (50-500 kHz)\n", bandwidth);
        return false;
    }
    if (preamble < 8 || preamble > 128) {
        Serial.printf("CFG_ERR:Preamble %d out of range (8-128 bits)\n", preamble);
        return false;
    }
    
    // Store configuration
    rfFrequency = freq;
    rfBitrate = bitrate;
    rfDeviation = deviation;
    rfRxBandwidth = bandwidth;
    rfPreambleLen = preamble;
    
    return true;
}

// ============================================================================
// Wait for Configuration from Mac App
// ============================================================================

bool waitForConfiguration() {
    Serial.println("\n[WAIT_CFG] Waiting for configuration from Mac app...");
    Serial.println("[WAIT_CFG] Send: CFG:<freq>,<bitrate>,<deviation>,<bandwidth>,<preamble>");
    Serial.printf("[WAIT_CFG] Example: CFG:%.1f,%.1f,%.1f,%.1f,%d\n", 
                  DEFAULT_FREQUENCY, DEFAULT_BITRATE, DEFAULT_DEVIATION, 
                  DEFAULT_RX_BANDWIDTH, DEFAULT_PREAMBLE_LEN);
    Serial.println("[WAIT_CFG] Or wait 2 minutes for defaults...");
    
    // Show waiting screen on TFT
    showWaitingScreen();
    
    String inputBuffer = "";
    uint32_t startTime = millis();
    uint32_t lastPromptTime = 0;
    uint32_t lastCountdownUpdate = 0;
    
    while (millis() - startTime < CONFIG_TIMEOUT_MS) {
        // Send periodic prompts so Mac app knows we're waiting
        if (millis() - lastPromptTime > 1000) {
            Serial.println("[WAIT_CFG]");
            lastPromptTime = millis();
        }
        
        // Update countdown on display
        if (millis() - lastCountdownUpdate > 1000) {
            int remaining = (CONFIG_TIMEOUT_MS - (millis() - startTime)) / 1000;
            char buf[32];
            tft->fillRect(20, 130, 280, 20, COLOR_BG);
            tft->setTextColor(COLOR_LABEL);
            tft->setTextSize(1);
            tft->setCursor(20, 130);
            snprintf(buf, sizeof(buf), "Timeout in %d seconds...", remaining);
            tft->print(buf);
            lastCountdownUpdate = millis();
        }
        
        // Check for serial input
        while (Serial.available()) {
            char c = Serial.read();
            
            if (c == '\n' || c == '\r') {
                if (inputBuffer.length() > 0) {
                    inputBuffer.trim();
                    
                    if (parseConfigCommand(inputBuffer)) {
                        Serial.printf("CFG_OK:%.1f,%.1f,%.1f,%.1f,%d\n",
                                     rfFrequency, rfBitrate, rfDeviation, 
                                     rfRxBandwidth, rfPreambleLen);
                        return true;
                    }
                    
                    inputBuffer = "";
                }
            } else if (inputBuffer.length() < 100) {
                inputBuffer += c;
            }
        }
        
        delay(10);
    }
    
    // Timeout - use defaults
    Serial.println("[WAIT_CFG] Timeout - using default configuration");
    Serial.printf("CFG_OK:%.1f,%.1f,%.1f,%.1f,%d\n",
                 rfFrequency, rfBitrate, rfDeviation, 
                 rfRxBandwidth, rfPreambleLen);
    return true;
}

// ============================================================================
// Initialize Radio with Current Configuration
// ============================================================================

bool initializeRadio() {
    // Initialize SPI
    DBGLN("[GS] Initializing SPI...");
    spi = new SPIClass(FSPI);
    spi->begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_NSS);
    
    pinMode(LORA_NSS, OUTPUT);
    digitalWrite(LORA_NSS, HIGH);
    
    // Reset radio
    DBGLN("[GS] Resetting SX1262...");
    pinMode(LORA_RST, OUTPUT);
    digitalWrite(LORA_RST, LOW);
    delay(10);
    digitalWrite(LORA_RST, HIGH);
    delay(100);
    
    // Wait for BUSY
    pinMode(LORA_BUSY, INPUT);
    uint32_t busyWait = millis();
    while (digitalRead(LORA_BUSY) == HIGH) {
        delay(1);
        if (millis() - busyWait > 1000) {
            Serial.println("[ERROR] SX1262 BUSY timeout");
            return false;
        }
    }
    
    // Create radio
    DBGLN("[GS] Creating radio module...");
    Module* mod = new Module(LORA_NSS, LORA_DIO1, LORA_RST, LORA_BUSY, *spi, SPISettings(2000000, MSBFIRST, SPI_MODE0));
    radio = new SX1262(mod);
    
    // Initialize FSK mode with TCXO at 1.8V
    Serial.printf("[GS] Initializing FSK: Freq=%.1f BR=%.1f Dev=%.1f BW=%.1f Pre=%d\n",
                  rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, rfPreambleLen);
    
    int state = radio->beginFSK(rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, 10, rfPreambleLen, 1.8, false);
    
    if (state != RADIOLIB_ERR_NONE) {
        Serial.printf("[ERROR] FSK init failed: %d\n", state);
        return false;
    }
    
    DBGLN("[GS] SX1262 initialized!");
    
    // Configure radio
    radio->setSyncWord(const_cast<uint8_t*>(SYNC_WORD), SYNC_WORD_LEN);
    radio->variablePacketLengthMode(MAX_PACKET_SIZE);
    radio->setDataShaping(RF_DATA_SHAPING);
    radio->setCRC(0);  // Disable radio CRC - protocol handles it
    
    // Setup interrupt and start RX
    radio->setDio1Action(onPacketReceived);
    radio->startReceive();
    
    return true;
}

// ============================================================================
// Setup
// ============================================================================

void setup() {
    Serial.begin(SERIAL_BAUD);
    delay(1000);
    
    Serial.println("\n========================================");
    Serial.println("RaptorHab Ground Station Bridge");
    Serial.println("Heltec Vision Master T190");
    Serial.println("========================================\n");
    
    pinMode(USER_BUTTON, INPUT_PULLUP);
    
    // Initialize TFT display first
    Serial.println("[TFT] Initializing display...");
    initDisplay();
    Serial.println("[TFT] Display initialized");
    
    // Wait for configuration from Mac app
    waitForConfiguration();
    configured = true;
    
    // Initialize radio with received (or default) configuration
    if (!initializeRadio()) {
        Serial.println("[ERROR] Radio initialization failed!");
        
        // Show error on display
        tft->fillScreen(COLOR_BAD);
        tft->setTextColor(COLOR_TEXT);
        tft->setTextSize(2);
        tft->setCursor(20, 70);
        tft->print("RADIO INIT FAILED!");
        tft->setTextSize(1);
        tft->setCursor(20, 100);
        tft->print("Please reset device");
        
        while (1) { 
            Serial.println("[ERROR] Radio init failed - please reset");
            delay(5000); 
        }
    }
    
    // Show configured screen
    showConfiguredScreen();
    
    Serial.printf("\n[CONFIG] Freq:%.1f BR:%.0f Dev:%.0f BW:%.0f Preamble:%d\n",
                  rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, rfPreambleLen);
    Serial.println("[READY] Listening for packets...");
    Serial.println("[STATS] Starting - will report every 10 seconds");
    
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
    
    // Send stats every 10 seconds
    sendStats();
    
    // Update display only during idle periods
    // This ensures USB packet forwarding is never delayed
    updateDisplay();
    
    // No delay - spin as fast as possible
}

// ============================================================================
// Statistics Reporting
// ============================================================================

void sendStats() {
    if (millis() - lastStatsTime < 10000) return;
    lastStatsTime = millis();
    
    float rate = packetsTotal > 0 ? (100.0 * packetsForwarded / packetsTotal) : 0.0;
    
    char statsBuf[300];
    snprintf(statsBuf, sizeof(statsBuf), 
        "\n[STATS] Total:%lu Fwd:%lu NoRAPT:%lu BadCRC:%lu Err:%lu Rate:%.1f%% Small:%lu Large:%lu\n",
        packetsTotal, packetsForwarded, packetsRejectedNoRapt, packetsRejectedCrc, 
        packetsRadioError, rate, packetsSmall, packetsLarge);
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
    lastSnr = radio->getSNR();
    packetsTotal++;
    
    // IMMEDIATELY restart receive to not miss next packet!
    radio->startReceive();
    
    if (state != RADIOLIB_ERR_NONE) {
        packetsRadioError++;
        return;
    }
    
    // Validate packet starts with protocol sync "RAPT"
    if (packetLen < 12 || 
        packet[0] != 0x52 || packet[1] != 0x41 || 
        packet[2] != 0x50 || packet[3] != 0x54) {
        packetsRejectedNoRapt++;
        return;
    }
    
    // Validate CRC32 (last 4 bytes of packet)
    uint32_t receivedCrc = ((uint32_t)packet[packetLen-4] << 24) |
                           ((uint32_t)packet[packetLen-3] << 16) |
                           ((uint32_t)packet[packetLen-2] << 8) |
                           ((uint32_t)packet[packetLen-1]);
    uint32_t calculatedCrc = crc32(packet, packetLen - 4);
    
    if (receivedCrc != calculatedCrc) {
        packetsRejectedCrc++;
        return;
    }
    
    // Valid packet - forward it (radio is already receiving again!)
    forwardPacket(packet, packetLen, lastRssi, lastSnr);
    packetsForwarded++;
    
    // Track by size
    if (packetLen < 100) {
        packetsSmall++;
    } else {
        packetsLarge++;
    }
}

// ============================================================================
// Packet Forwarding (with byte stuffing)
// ============================================================================

void forwardPacket(uint8_t* data, int len, float rssi, float snr) {
    /*
     * Frame format with byte stuffing:
     * [0x7E][LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM][0x7E]
     * 
     * Byte stuffing (HDLC-style):
     * - 0x7E in data -> 0x7D 0x5E
     * - 0x7D in data -> 0x7D 0x5D
     */
    
    uint8_t lenHi = (len >> 8) & 0xFF;
    uint8_t lenLo = len & 0xFF;
    int8_t rssiInt = (int8_t)rssi;
    uint8_t rssiFrac = (uint8_t)(abs(rssi - rssiInt) * 100);
    int8_t snrInt = (int8_t)snr;
    uint8_t snrFrac = (uint8_t)(abs(snr - snrInt) * 100);
    
    // Calculate checksum over unstuffed data
    uint8_t checksum = lenHi ^ lenLo ^ (uint8_t)rssiInt ^ rssiFrac ^ (uint8_t)snrInt ^ snrFrac;
    for (int i = 0; i < len; i++) {
        checksum ^= data[i];
    }
    
    // Helper lambda to write with byte stuffing
    auto writeStuffed = [](uint8_t b) {
        if (b == 0x7E) {
            Serial.write(0x7D);
            Serial.write(0x5E);
        } else if (b == 0x7D) {
            Serial.write(0x7D);
            Serial.write(0x5D);
        } else {
            Serial.write(b);
        }
    };
    
    // Ensure clean frame boundary
    Serial.flush();
    delayMicroseconds(100);
    
    // Start delimiter (not stuffed)
    Serial.write(FRAME_DELIMITER);
    
    // Header (stuffed)
    writeStuffed(lenHi);
    writeStuffed(lenLo);
    writeStuffed((uint8_t)rssiInt);
    writeStuffed(rssiFrac);
    writeStuffed((uint8_t)snrInt);
    writeStuffed(snrFrac);
    
    // Data (stuffed)
    for (int i = 0; i < len; i++) {
        writeStuffed(data[i]);
    }
    
    // Checksum (stuffed)
    writeStuffed(checksum);
    
    // End delimiter (not stuffed)
    Serial.write(FRAME_DELIMITER);
    Serial.flush();
}
