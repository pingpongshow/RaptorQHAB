/*
 * RaptorHab Ground Station Bridge
 * Heltec Vision Master T190 (ESP32-S3 + SX1262)
 * 
 * Receives packets via SX1262 and forwards them over USB serial AND Bluetooth LE
 * Displays RSSI, SNR, radio settings, and BLE status on 1.9" TFT LCD
 * 
 * BLUETOOTH:
 *   Device name: "RaptorModem"
 *   Uses Nordic UART Service (NUS) style UUIDs
 *   Static passkey: 123456 (configurable)
 *   Supports configuration and packet forwarding over BLE
 * 
 * CONFIGURATION MODE:
 *   On boot, modem waits for configuration from Mac/iOS app via USB OR Bluetooth
 *   Config command: CFG:<freq>,<bitrate>,<deviation>,<bandwidth>,<preamble>\n
 *   Example: CFG:915.0,96.0,50.0,467.0,32\n
 *   Response: CFG_OK:<params>\n or CFG_ERR:<message>\n
 * 
 * Serial Protocol to Mac (USB - unchanged):
 *   [0x7E][LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM][0x7E]
 * 
 * BLE Protocol to iOS:
 *   TX Characteristic (notify): [RSSI_FLOAT_LE][SNR_FLOAT_LE][DATA...]
 *   RX Characteristic (write): Configuration commands as UTF-8 strings
 *   Large packets are chunked with sequence numbers if needed
 * 
 * TFT Display:
 *   - Shows RSSI, SNR, packet counts, radio settings, and BLE status
 *   - Updates only during idle periods (no packets for >750ms)
 */

#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// ============================================================================
// Configuration
// ============================================================================

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

// Pin Definitions - Battery Monitoring
#define ADC_CTRL_PIN    46      // Controls P-FET switch for battery divider
#define VBAT_READ_PIN   6       // ADC input from voltage divider

// Battery voltage divider: R9=390K, R11=100K -> ratio = 100/(390+100) = 0.204
#define VBAT_DIVIDER_RATIO  4.9f    // Multiply ADC voltage by this to get VBAT
#define VBAT_MIN            3.0f    // Empty LiPo
#define VBAT_MAX            4.2f    // Full LiPo

// Pin Definitions - TFT Display (ST7789V3)
#define TFT_CS      39
#define TFT_RST     40
#define TFT_DC      47
#define TFT_SCLK    38
#define TFT_MOSI    48
#define TFT_LED_EN  17
#define TFT_PWR     7

// Display dimensions (landscape orientation)
#define TFT_WIDTH   320
#define TFT_HEIGHT  170

// Default RF Configuration
#define DEFAULT_FREQUENCY       915.0
#define DEFAULT_BITRATE         96.0
#define DEFAULT_DEVIATION       50.0
#define DEFAULT_RX_BANDWIDTH    467.0
#define DEFAULT_PREAMBLE_LEN    32
#define RF_DATA_SHAPING         0.5

// Configuration timeout
#define CONFIG_TIMEOUT_MS       120000    // 2 minutes

// Display update configuration
#define DISPLAY_IDLE_THRESHOLD_MS   750
#define DISPLAY_UPDATE_INTERVAL_MS  500
#define DISPLAY_STATS_INTERVAL_MS   1000

// Sync word "RAPT"
const uint8_t SYNC_WORD[] = {0x52, 0x41, 0x50, 0x54};
#define SYNC_WORD_LEN       4

// Serial Protocol
#define FRAME_DELIMITER     0x7E
#define SERIAL_BAUD         921600
#define MAX_PACKET_SIZE     255

// BLE Configuration
#define BLE_DEVICE_NAME     "RaptorModem"
#define BLE_PASSKEY         123456      // Static passkey for pairing

// Nordic UART Service UUIDs
#define SERVICE_UUID           "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define CHARACTERISTIC_UUID_RX "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  // Write (phone → modem)
#define CHARACTERISTIC_UUID_TX "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  // Notify (modem → phone)

// BLE packet header size (RSSI float + SNR float = 8 bytes)
#define BLE_HEADER_SIZE     8
// BLE MTU - we'll negotiate higher but start conservative
#define BLE_DEFAULT_MTU     20
#define BLE_MAX_MTU         512

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
#define COLOR_BLE           0x001F   // Blue for BLE indicator

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

bool configured = false;
bool configuredViaBLE = false;  // Track config source for display

// ============================================================================
// Global Objects - Radio & Display
// ============================================================================

SPIClass* spi = nullptr;
SX1262* radio = nullptr;
SPIClass* tftSpi = nullptr;
Adafruit_ST7789* tft = nullptr;

volatile bool packetReceived = false;
uint32_t packetsTotal = 0;
uint32_t packetsForwarded = 0;
uint32_t packetsRejectedNoRapt = 0;
uint32_t packetsRejectedCrc = 0;
uint32_t packetsRadioError = 0;
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
bool prevBleConnected = false;

// ============================================================================
// Global Objects - BLE
// ============================================================================

BLEServer* pServer = nullptr;
BLECharacteristic* pTxCharacteristic = nullptr;
BLECharacteristic* pRxCharacteristic = nullptr;
bool bleDeviceConnected = false;
bool bleOldDeviceConnected = false;
uint16_t bleMTU = BLE_DEFAULT_MTU;
String bleConfigBuffer = "";  // Buffer for config commands received over BLE

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
// BLE Callbacks
// ============================================================================

class ServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer* pServer, esp_ble_gatts_cb_param_t* param) {
        bleDeviceConnected = true;
        
        // Request higher MTU for larger packets
        // iOS typically supports 185-512, Android varies
        pServer->updatePeerMTU(param->connect.conn_id, BLE_MAX_MTU);
        
        Serial.println("[BLE] Device connected");
        displayNeedsFullRedraw = true;
    }

    void onDisconnect(BLEServer* pServer) {
        bleDeviceConnected = false;
        bleMTU = BLE_DEFAULT_MTU;
        Serial.println("[BLE] Device disconnected");
        displayNeedsFullRedraw = true;
        
        // Restart advertising
        pServer->startAdvertising();
    }

    void onMtuChanged(BLEServer* pServer, esp_ble_gatts_cb_param_t* param) {
        bleMTU = param->mtu.mtu;
        Serial.printf("[BLE] MTU changed to: %d\n", bleMTU);
    }
};

// Forward declarations - must be before RxCallbacks
void handleBLEConfig(String command);
bool parseConfigCommand(const String& cmd);
void sendBLEResponse(const String& response);

class RxCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic* pCharacteristic) {
        String rxValue = pCharacteristic->getValue();
        
        if (rxValue.length() > 0) {
            // Append to config buffer
            bleConfigBuffer += rxValue;
            
            DBGF("[BLE] Received %d bytes: %s\n", rxValue.length(), rxValue.c_str());
            
            // Check for complete command (ends with newline)
            int newlinePos = bleConfigBuffer.indexOf('\n');
            if (newlinePos >= 0) {
                String command = bleConfigBuffer.substring(0, newlinePos);
                bleConfigBuffer = bleConfigBuffer.substring(newlinePos + 1);
                
                Serial.printf("[BLE] Command received: %s\n", command.c_str());
                
                // Process the command (will be handled in main loop or config wait)
                // For now, echo it to USB serial for debugging
                if (command.startsWith("CFG:")) {
                    // Configuration command - handle it
                    handleBLEConfig(command);
                }
            }
        }
    }
};

// ============================================================================
// BLE Security Callbacks (must be before initBLE)
// ============================================================================

class MyBLESecurityCallbacks : public BLESecurityCallbacks {
public:
    uint32_t onPassKeyRequest() override {
        Serial.println("[BLE] PassKey Request");
        return BLE_PASSKEY;
    }

    void onPassKeyNotify(uint32_t pass_key) override {
        Serial.printf("[BLE] PassKey Notify: %06d\n", pass_key);
    }

    bool onConfirmPIN(uint32_t pass_key) override {
        Serial.printf("[BLE] Confirm PIN: %06d\n", pass_key);
        return true;
    }

    bool onSecurityRequest() override {
        Serial.println("[BLE] Security Request");
        return true;
    }

    void onAuthenticationComplete(esp_ble_auth_cmpl_t auth_cmpl) override {
        if (auth_cmpl.success) {
            Serial.println("[BLE] Authentication SUCCESS");
        } else {
            Serial.printf("[BLE] Authentication FAILED, reason: %d\n", auth_cmpl.fail_reason);
        }
    }
};

// ============================================================================
// BLE Initialization
// ============================================================================

void initBLE() {
    Serial.println("[BLE] Initializing Bluetooth...");
    
    // Create BLE Device
    BLEDevice::init(BLE_DEVICE_NAME);
    
    // Set up security with static passkey
    BLEDevice::setEncryptionLevel(ESP_BLE_SEC_ENCRYPT);
    BLEDevice::setSecurityCallbacks(new MyBLESecurityCallbacks());
    
    // Security settings for static passkey
    esp_ble_auth_req_t auth_req = ESP_LE_AUTH_REQ_SC_MITM_BOND;
    esp_ble_io_cap_t iocap = ESP_IO_CAP_OUT;  // Display only (shows passkey)
    uint8_t key_size = 16;
    uint8_t init_key = ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK;
    uint8_t rsp_key = ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK;
    uint32_t passkey = BLE_PASSKEY;
    uint8_t auth_option = ESP_BLE_ONLY_ACCEPT_SPECIFIED_AUTH_DISABLE;
    uint8_t oob_support = ESP_BLE_OOB_DISABLE;
    
    esp_ble_gap_set_security_param(ESP_BLE_SM_SET_STATIC_PASSKEY, &passkey, sizeof(uint32_t));
    esp_ble_gap_set_security_param(ESP_BLE_SM_AUTHEN_REQ_MODE, &auth_req, sizeof(uint8_t));
    esp_ble_gap_set_security_param(ESP_BLE_SM_IOCAP_MODE, &iocap, sizeof(uint8_t));
    esp_ble_gap_set_security_param(ESP_BLE_SM_MAX_KEY_SIZE, &key_size, sizeof(uint8_t));
    esp_ble_gap_set_security_param(ESP_BLE_SM_ONLY_ACCEPT_SPECIFIED_SEC_AUTH, &auth_option, sizeof(uint8_t));
    esp_ble_gap_set_security_param(ESP_BLE_SM_OOB_SUPPORT, &oob_support, sizeof(uint8_t));
    esp_ble_gap_set_security_param(ESP_BLE_SM_SET_INIT_KEY, &init_key, sizeof(uint8_t));
    esp_ble_gap_set_security_param(ESP_BLE_SM_SET_RSP_KEY, &rsp_key, sizeof(uint8_t));
    
    // Create BLE Server
    pServer = BLEDevice::createServer();
    pServer->setCallbacks(new ServerCallbacks());
    
    // Create BLE Service (Nordic UART Service)
    BLEService* pService = pServer->createService(SERVICE_UUID);
    
    // Create TX Characteristic (Notify - modem to phone)
    pTxCharacteristic = pService->createCharacteristic(
        CHARACTERISTIC_UUID_TX,
        BLECharacteristic::PROPERTY_NOTIFY
    );
    pTxCharacteristic->addDescriptor(new BLE2902());
    
    // Create RX Characteristic (Write - phone to modem)
    pRxCharacteristic = pService->createCharacteristic(
        CHARACTERISTIC_UUID_RX,
        BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR
    );
    pRxCharacteristic->setCallbacks(new RxCallbacks());
    
    // Start the service
    pService->start();
    
    // Start advertising
    BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
    pAdvertising->addServiceUUID(SERVICE_UUID);
    pAdvertising->setScanResponse(true);
    pAdvertising->setMinPreferred(0x06);  // Help with iPhone connection
    pAdvertising->setMinPreferred(0x12);
    BLEDevice::startAdvertising();
    
    Serial.println("[BLE] Bluetooth initialized - advertising as 'RaptorModem'");
    Serial.printf("[BLE] Passkey: %06d\n", BLE_PASSKEY);
}

// ============================================================================
// BLE Response Helper
// ============================================================================

void sendBLEResponse(const String& response) {
    if (bleDeviceConnected && pTxCharacteristic) {
        // For config responses, send as notification
        // Prepend with special marker to distinguish from packet data
        String markedResponse = "RSP:" + response;
        pTxCharacteristic->setValue((uint8_t*)markedResponse.c_str(), markedResponse.length());
        pTxCharacteristic->notify();
        Serial.printf("[BLE] Sent response: %s\n", response.c_str());
    }
}

// ============================================================================
// BLE Config Handler
// ============================================================================

void handleBLEConfig(String command) {
    // Remove "CFG:" prefix if present
    if (command.startsWith("CFG:")) {
        if (parseConfigCommand(command)) {
            // Success - send confirmation
            char response[100];
            snprintf(response, sizeof(response), "CFG_OK:%.1f,%.1f,%.1f,%.1f,%d",
                     rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, rfPreambleLen);
            sendBLEResponse(response);
            configuredViaBLE = true;
            configured = true;
        } else {
            sendBLEResponse("CFG_ERR:Invalid parameters");
        }
    }
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
void forwardPacketBLE(uint8_t* data, int len, float rssi, float snr);
void sendStats();
bool waitForConfiguration();
bool initializeRadio();
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
    tft->print(configuredViaBLE ? "BLE" : "USB");
    
    // Divider
    tft->drawFastHLine(0, 85, TFT_WIDTH, 0x4208);
    
    // Signal section header
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(5, 90);
    tft->print("SIGNAL");
    
    // BLE Status section (right side of signal area)
    tft->setCursor(200, 90);
    tft->print("BLUETOOTH");
    
    // Stats section header
    tft->setCursor(5, 135);
    tft->print("STATISTICS");
    
    displayNeedsFullRedraw = false;
}

void updateSignalDisplay() {
    // Only update if values changed
    if (lastRssi == prevRssi && lastSnr == prevSnr && bleDeviceConnected == prevBleConnected) {
        return;
    }
    
    // Clear signal value area
    tft->fillRect(5, 100, 190, 30, COLOR_BG);
    
    // RSSI
    tft->setTextSize(2);
    uint16_t rssiColor = lastRssi > -80 ? COLOR_GOOD : (lastRssi > -100 ? COLOR_WARN : COLOR_BAD);
    tft->setTextColor(rssiColor);
    tft->setCursor(5, 105);
    tft->printf("%.0f", lastRssi);
    tft->setTextSize(1);
    tft->print(" dBm");
    
    // SNR
    tft->setTextSize(2);
    uint16_t snrColor = lastSnr > 5 ? COLOR_GOOD : (lastSnr > 0 ? COLOR_WARN : COLOR_BAD);
    tft->setTextColor(snrColor);
    tft->setCursor(90, 105);
    tft->printf("%.1f", lastSnr);
    tft->setTextSize(1);
    tft->print(" dB");
    
    // BLE Status area
    tft->fillRect(200, 100, 120, 30, COLOR_BG);
    if (bleDeviceConnected) {
        tft->setTextSize(2);
        tft->setTextColor(COLOR_GOOD);
        tft->setCursor(200, 105);
        tft->print("CONNECTED");
    } else {
        tft->setTextSize(1);
        tft->setTextColor(COLOR_WARN);
        tft->setCursor(200, 100);
        tft->print("Advertising...");
        tft->setCursor(200, 112);
        tft->setTextColor(COLOR_VALUE);
        tft->printf("PIN: %06d", BLE_PASSKEY);
    }
    
    prevRssi = lastRssi;
    prevSnr = lastSnr;
    prevBleConnected = bleDeviceConnected;
}

void updateStatsDisplay() {
    // Only update periodically
    static uint32_t lastUpdate = 0;
    if (millis() - lastUpdate < 500) return;
    lastUpdate = millis();
    
    // Only update if values changed
    if (packetsForwarded == prevPacketsForwarded && packetsTotal == prevPacketsTotal) {
        return;
    }
    
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
    
    // Success rate
    float rate = packetsTotal > 0 ? (100.0f * packetsForwarded / packetsTotal) : 0.0f;
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(210, 147);
    tft->print("RATE:");
    tft->setTextColor(rate > 90 ? COLOR_GOOD : (rate > 70 ? COLOR_WARN : COLOR_BAD));
    tft->printf("%.1f%%", rate);
    
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
    
    // BLE indicator in stats
    tft->setCursor(160, 159);
    tft->setTextColor(COLOR_LABEL);
    tft->print("BLE:");
    tft->setTextColor(bleDeviceConnected ? COLOR_GOOD : COLOR_LABEL);
    tft->print(bleDeviceConnected ? "ON" : "OFF");
    
    // MTU if connected
    if (bleDeviceConnected) {
        tft->setCursor(210, 159);
        tft->setTextColor(COLOR_LABEL);
        tft->print("MTU:");
        tft->setTextColor(COLOR_VALUE);
        tft->printf("%d", bleMTU);
    }
    
    prevPacketsForwarded = packetsForwarded;
    prevPacketsTotal = packetsTotal;
}

// ============================================================================
// Battery Monitoring
// ============================================================================

float readBatteryVoltage() {
    // Enable the battery voltage divider by turning on Q3->Q2
    pinMode(ADC_CTRL_PIN, OUTPUT);
    digitalWrite(ADC_CTRL_PIN, HIGH);
    delayMicroseconds(100);  // Let it settle (very brief, won't affect packet timing)
    
    // Take multiple readings and average for stability
    uint32_t sum = 0;
    const int samples = 4;
    for (int i = 0; i < samples; i++) {
        sum += analogRead(VBAT_READ_PIN);
    }
    
    // Turn off the divider to save power
    digitalWrite(ADC_CTRL_PIN, LOW);
    
    // Calculate voltage
    // ESP32-S3 ADC: 12-bit (0-4095), default attenuation gives ~0-2.5V range
    // With ADC_ATTEN_DB_11, range is ~0-3.3V
    float avgRaw = (float)sum / samples;
    float vRead = (avgRaw / 4095.0f) * 3.3f;
    float vBat = vRead * VBAT_DIVIDER_RATIO;
    
    return vBat;
}

void updateBatteryDisplay() {
    // Only update periodically (same rate as stats)
    static uint32_t lastBatteryUpdate = 0;
    if (millis() - lastBatteryUpdate < 1000) return;
    lastBatteryUpdate = millis();
    
    // Read battery voltage
    batteryVoltage = readBatteryVoltage();
    
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
    tft->print("Connect via USB or Bluetooth");
    
    // BLE Info
    tft->setTextColor(COLOR_BLE);
    tft->setCursor(20, 95);
    tft->print("Bluetooth: ");
    tft->setTextColor(COLOR_VALUE);
    tft->print(BLE_DEVICE_NAME);
    
    tft->setTextColor(COLOR_BLE);
    tft->setCursor(20, 110);
    tft->print("Passkey: ");
    tft->setTextColor(COLOR_GOOD);
    tft->printf("%06d", BLE_PASSKEY);
    
    // Default settings info
    tft->setTextColor(COLOR_WARN);
    tft->setCursor(20, 135);
    tft->print("Default: 915MHz, 96kbps");
    
    tft->setTextColor(COLOR_LABEL);
    tft->setCursor(20, 155);
    tft->printf("Timeout: %ds", CONFIG_TIMEOUT_MS / 1000);
}

void showConfiguredScreen() {
    displayNeedsFullRedraw = true;
    drawStaticUI();
}

// ============================================================================
// Configuration Waiting
// ============================================================================

bool waitForConfiguration() {
    showWaitingScreen();
    
    Serial.println("\n[CONFIG] Waiting for configuration via USB or Bluetooth...");
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
            if (c == '\n' || c == '\r') {
                if (usbBuffer.length() > 0) {
                    Serial.printf("[USB] Received: %s\n", usbBuffer.c_str());
                    if (usbBuffer.startsWith("CFG:")) {
                        if (parseConfigCommand(usbBuffer)) {
                            Serial.printf("CFG_OK:%.1f,%.1f,%.1f,%.1f,%d\n",
                                         rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, rfPreambleLen);
                            configuredViaBLE = false;
                            return true;
                        } else {
                            Serial.println("CFG_ERR:Invalid parameters");
                        }
                    }
                    usbBuffer = "";
                }
            } else {
                usbBuffer += c;
            }
        }
        
        // Check if BLE config was received (handled in callback)
        if (configured && configuredViaBLE) {
            Serial.println("[CONFIG] Configuration received via Bluetooth");
            return true;
        }
        
        // Progress indicator
        if (millis() - lastDot > 1000) {
            lastDot = millis();
            Serial.print(".");
            
            // Update display with countdown
            int remaining = (CONFIG_TIMEOUT_MS - (millis() - startTime)) / 1000;
            tft->fillRect(100, 155, 50, 10, COLOR_BG);
            tft->setTextColor(COLOR_LABEL);
            tft->setCursor(100, 155);
            tft->printf("%ds", remaining);
            
            // Update BLE status
            tft->fillRect(200, 95, 120, 30, COLOR_BG);
            tft->setCursor(200, 95);
            if (bleDeviceConnected) {
                tft->setTextColor(COLOR_GOOD);
                tft->print("BLE Connected!");
            } else {
                tft->setTextColor(COLOR_WARN);
                tft->print("Searching...");
            }
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

bool initializeRadio() {
    Serial.println("[RADIO] Initializing SX1262...");
    
    spi = new SPIClass(FSPI);
    spi->begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_NSS);
    
    Module* mod = new Module(LORA_NSS, LORA_DIO1, LORA_RST, LORA_BUSY, *spi);
    radio = new SX1262(mod);
    
    Serial.printf("[RADIO] Initializing FSK: Freq=%.1f BR=%.1f Dev=%.1f BW=%.1f Pre=%d\n",
                  rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, rfPreambleLen);
    
    int state = radio->beginFSK(rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, 10, rfPreambleLen, 1.8, false);
    
    if (state != RADIOLIB_ERR_NONE) {
        Serial.printf("[ERROR] FSK init failed: %d\n", state);
        return false;
    }
    
    radio->setSyncWord(const_cast<uint8_t*>(SYNC_WORD), SYNC_WORD_LEN);
    radio->variablePacketLengthMode(MAX_PACKET_SIZE);
    radio->setDataShaping(RF_DATA_SHAPING);
    radio->setCRC(0);
    
    radio->setDio1Action(onPacketReceived);
    radio->startReceive();
    
    Serial.println("[RADIO] SX1262 initialized successfully");
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
    Serial.println("USB + Bluetooth LE Support");
    Serial.println("========================================\n");
    
    pinMode(USER_BUTTON, INPUT_PULLUP);
    
    // Initialize battery monitoring pins
    pinMode(ADC_CTRL_PIN, OUTPUT);
    digitalWrite(ADC_CTRL_PIN, LOW);  // Start with divider off to save power
    analogReadResolution(12);          // 12-bit ADC (0-4095)
    analogSetAttenuation(ADC_11db);    // Full 0-3.3V range
    
    // Initialize display
    Serial.println("[TFT] Initializing display...");
    initDisplay();
    Serial.println("[TFT] Display initialized");
    
    // Initialize Bluetooth BEFORE waiting for config
    initBLE();
    
    // Wait for configuration from USB or BLE
    waitForConfiguration();
    configured = true;
    
    // Initialize radio
    if (!initializeRadio()) {
        Serial.println("[ERROR] Radio initialization failed!");
        
        tft->fillScreen(COLOR_BAD);
        tft->setTextColor(COLOR_TEXT);
        tft->setTextSize(2);
        tft->setCursor(20, 70);
        tft->print("RADIO INIT FAILED!");
        
        while (1) {
            Serial.println("[ERROR] Radio init failed - please reset");
            delay(5000);
        }
    }
    
    showConfiguredScreen();
    
    Serial.printf("\n[CONFIG] Freq:%.1f BR:%.0f Dev:%.0f BW:%.0f Preamble:%d\n",
                  rfFrequency, rfBitrate, rfDeviation, rfRxBandwidth, rfPreambleLen);
    Serial.println("[READY] Listening for packets...");
    Serial.println("[BLE] Bluetooth ready for connections");
    
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
    
    // Handle BLE connection state changes
    if (bleDeviceConnected != bleOldDeviceConnected) {
        if (bleDeviceConnected) {
            Serial.println("[BLE] Client connected");
        } else {
            Serial.println("[BLE] Client disconnected");
        }
        bleOldDeviceConnected = bleDeviceConnected;
        displayNeedsFullRedraw = true;
    }
    
    // Send stats every 10 seconds
    sendStats();
    
    // Update display during idle periods
    updateDisplay();
}

// ============================================================================
// Statistics Reporting
// ============================================================================

void sendStats() {
    if (millis() - lastStatsTime < 10000) return;
    lastStatsTime = millis();
    
    float rate = packetsTotal > 0 ? (100.0 * packetsForwarded / packetsTotal) : 0.0;
    
    char statsBuf[350];
    snprintf(statsBuf, sizeof(statsBuf), 
        "\n[STATS] Total:%lu Fwd:%lu NoRAPT:%lu BadCRC:%lu Err:%lu Rate:%.1f%% BLE:%s Batt:%.2fV(%d%%)\n",
        packetsTotal, packetsForwarded, packetsRejectedNoRapt, packetsRejectedCrc, 
        packetsRadioError, rate, bleDeviceConnected ? "Connected" : "Idle",
        batteryVoltage, batteryPercent);
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
    
    // Valid packet - forward via USB AND BLE
    forwardPacket(packet, packetLen, lastRssi, lastSnr);
    forwardPacketBLE(packet, packetLen, lastRssi, lastSnr);
    packetsForwarded++;
    
    // Track by size
    if (packetLen < 100) {
        packetsSmall++;
    } else {
        packetsLarge++;
    }
}

// ============================================================================
// USB Packet Forwarding (unchanged)
// ============================================================================

void forwardPacket(uint8_t* data, int len, float rssi, float snr) {
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
    
    Serial.flush();
    delayMicroseconds(100);
    
    Serial.write(FRAME_DELIMITER);
    
    writeStuffed(lenHi);
    writeStuffed(lenLo);
    writeStuffed((uint8_t)rssiInt);
    writeStuffed(rssiFrac);
    writeStuffed((uint8_t)snrInt);
    writeStuffed(snrFrac);
    
    for (int i = 0; i < len; i++) {
        writeStuffed(data[i]);
    }
    
    writeStuffed(checksum);
    Serial.write(FRAME_DELIMITER);
    Serial.flush();
}

// ============================================================================
// BLE Packet Forwarding
// ============================================================================

void forwardPacketBLE(uint8_t* data, int len, float rssi, float snr) {
    if (!bleDeviceConnected || pTxCharacteristic == nullptr) {
        return;
    }
    
    // BLE packet format:
    // [PKT marker (3 bytes)] [RSSI float LE (4 bytes)] [SNR float LE (4 bytes)] [DATA...]
    // Total header: 11 bytes
    
    const char* marker = "PKT";
    int totalLen = 3 + 4 + 4 + len;  // marker + rssi + snr + data
    
    // Check if we need to chunk (MTU - 3 for ATT overhead)
    int maxPayload = bleMTU - 3;
    
    if (totalLen <= maxPayload) {
        // Single packet - send directly
        uint8_t* blePacket = (uint8_t*)malloc(totalLen);
        if (blePacket == nullptr) return;
        
        // Marker
        memcpy(blePacket, marker, 3);
        
        // RSSI as little-endian float
        memcpy(blePacket + 3, &rssi, 4);
        
        // SNR as little-endian float
        memcpy(blePacket + 7, &snr, 4);
        
        // Packet data
        memcpy(blePacket + 11, data, len);
        
        pTxCharacteristic->setValue(blePacket, totalLen);
        pTxCharacteristic->notify();
        
        free(blePacket);
    } else {
        // Need to chunk the packet
        // Chunk format: [CHK marker] [chunk#] [total chunks] [data...]
        
        int dataPerChunk = maxPayload - 5;  // 3 for "CHK" + 1 chunk# + 1 total
        int totalChunks = (totalLen + dataPerChunk - 1) / dataPerChunk;
        
        // Build full packet first
        uint8_t* fullPacket = (uint8_t*)malloc(totalLen);
        if (fullPacket == nullptr) return;
        
        memcpy(fullPacket, marker, 3);
        memcpy(fullPacket + 3, &rssi, 4);
        memcpy(fullPacket + 7, &snr, 4);
        memcpy(fullPacket + 11, data, len);
        
        // Send chunks
        for (int chunk = 0; chunk < totalChunks; chunk++) {
            int offset = chunk * dataPerChunk;
            int chunkLen = min(dataPerChunk, totalLen - offset);
            
            uint8_t* chunkPacket = (uint8_t*)malloc(chunkLen + 5);
            if (chunkPacket == nullptr) {
                free(fullPacket);
                return;
            }
            
            chunkPacket[0] = 'C';
            chunkPacket[1] = 'H';
            chunkPacket[2] = 'K';
            chunkPacket[3] = (uint8_t)chunk;
            chunkPacket[4] = (uint8_t)totalChunks;
            memcpy(chunkPacket + 5, fullPacket + offset, chunkLen);
            
            pTxCharacteristic->setValue(chunkPacket, chunkLen + 5);
            pTxCharacteristic->notify();
            
            free(chunkPacket);
            
            // Small delay between chunks to prevent BLE congestion
            delay(5);
        }
        
        free(fullPacket);
    }
}
