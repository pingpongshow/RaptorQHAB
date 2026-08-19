/*
  RaptorHAB Dual-900 Ground Station Modem
  PingPong Dual RF board — ESP32-S3-WROOM-1U + two E22-900M30S (SX1262).

  Slot B receives the RAPTOR GFSK image and telemetry downlink, doing the job
  the Heltec T190 did, at the same settings and with the same USB frame format.

  Slot A receives *and transmits* Meshtastic LoRa. That is the capability the
  single-radio modem could not have: the T190's one SX1262 cannot be in GFSK
  and LoRa at the same time, so listening for beacons meant not listening for
  images. Here they are separate radios and both run continuously.

  Two things about this board shape the firmware:

    - The RF switch is hardware. DIO2 drives TXEN and a transistor inverts it
      to RXEN. setDio2AsRfSwitch(true) is not optional; without it the module
      idles in RX and a transmit drives the PA into a disabled path.

    - Both radios are now in the same band, centimetres apart, unfiltered.
      When slot A transmits at 1 W, slot B is not degraded, it is deaf. The
      arbiter serialises transmissions and accounts for the receive time lost,
      because a modem that silently drops image packets whenever it beacons
      looks exactly like a bad radio link.
*/

#include <Arduino.h>
#include <RadioLib.h>
#include <SPI.h>

#include "board_dual900.h"
#include "tx_arbiter.h"
#include "usb_protocol.h"

// ---------------------------------------------------------------- config ---

// RAPTOR downlink defaults. These must match the payload: see
// Pi/common/radio.py SX1262Config. The 234.3 kHz bandwidth follows Carson's
// rule for 96 kbps / 50 kHz deviation; the receive preamble stays short so the
// payload's 128-bit transmit preamble has margin above the detector.
struct RaptorConfig {
    float    freqMHz     = 915.0f;
    float    bitrateKbps = 96.0f;
    float    devKhz      = 50.0f;
    float    bwKhz       = 234.3f;
    uint16_t preambleBits = 32;
};

// Meshtastic LongFast. Standard parameters and the standard sync word, which
// is what lets any stock Meshtastic radio hear the balloon and lets this board
// talk back to it.
struct MeshConfig {
    float   freqMHz = 906.875f;   // US channel 20; region-dependent
    float   bwKhz   = 250.0f;
    uint8_t sf      = 11;
    uint8_t cr      = 5;
    uint8_t syncWord = 0x2B;
    int8_t  powerDbm = 22;        // SX1262 output; the E22 PA adds ~8 dB
};

static RaptorConfig raptorCfg;
static MeshConfig   meshCfg;

// Sent in place of an SNR reading on the GFSK slot, which has none. The
// Meshtastic slot runs LoRa and does report a real SNR, so only slot B uses
// this -- the distinction is the whole point.
#define SNR_NOT_AVAILABLE (-128.0f)

static const uint8_t RAPTOR_SYNC[] = { 0x52, 0x41, 0x50, 0x54 };  // "RAPT"
static constexpr size_t MAX_PACKET = 255;

// ---------------------------------------------------------------- state ----

static SPIClass spiBus(FSPI);
static SpiBusLock spiLock;
static TxArbiter arbiter;

static SX1262* raptorRadio = nullptr;   // slot B
static SX1262* meshRadio   = nullptr;   // slot A

static volatile bool raptorIrq = false;
static volatile bool meshIrq   = false;

static uint32_t raptorTotal = 0, raptorFwd = 0, raptorNoSync = 0, raptorBadCrc = 0;
static uint32_t meshRx = 0, meshTx = 0, meshTxFail = 0;
static uint32_t lastStatsMs = 0;
static bool     raptorReady = false, meshReady = false;

IRAM_ATTR void onRaptorIrq() { raptorIrq = true; }
IRAM_ATTR void onMeshIrq()   { meshIrq = true; }

// ----------------------------------------------------------------- CRC -----

// CRC-32 (IEEE 802.3), matching Pi/common/protocol.py.
static uint32_t crc32(const uint8_t* data, size_t len) {
    uint32_t crc = 0xFFFFFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ (0xEDB88320 & (-(int32_t)(crc & 1)));
    }
    return ~crc;
}

// ------------------------------------------------------------ radio setup --

static bool configureRaptor() {
    SpiBusGuard guard(spiLock);

    int state = raptorRadio->beginFSK(
        raptorCfg.freqMHz, raptorCfg.bitrateKbps, raptorCfg.devKhz,
        raptorCfg.bwKhz, SX1262_MAX_DBM, raptorCfg.preambleBits,
        TCXO_VOLTAGE_V, false);
    if (state != RADIOLIB_ERR_NONE) {
        Serial.printf("[RAPTOR] FSK init failed: %d\n", state);
        return false;
    }

    raptorRadio->setDio2AsRfSwitch(USE_DIO2_AS_RF_SWITCH);
    raptorRadio->setSyncWord(const_cast<uint8_t*>(RAPTOR_SYNC), sizeof(RAPTOR_SYNC));
    raptorRadio->variablePacketLengthMode(MAX_PACKET);
    raptorRadio->setDataShaping(RADIOLIB_SHAPING_0_5);
    // The payload appends its own CRC-32 inside the packet, so the hardware
    // CRC stays off and the check happens where the framing is understood.
    raptorRadio->setCRC(0);

    // Whitening on the image slot, matching the payload. Mandatory, not an
    // optimisation: a mismatch removes the link rather than degrading it. The
    // Meshtastic slot below does not get this -- Meshtastic defines its own
    // on-air format and changing it would make the balloon unreadable by every
    // stock radio, which is the entire point of using Meshtastic.
    raptorRadio->setWhitening(true, 0x01FF);
    raptorRadio->setPacketReceivedAction(onRaptorIrq);

    state = raptorRadio->startReceive();
    if (state != RADIOLIB_ERR_NONE) {
        Serial.printf("[RAPTOR] startReceive failed: %d\n", state);
        return false;
    }
    Serial.printf("[RAPTOR] listening: %.3f MHz GFSK %.0f kbps dev %.0f kHz "
                  "bw %.1f kHz preamble %u\n",
                  raptorCfg.freqMHz, raptorCfg.bitrateKbps, raptorCfg.devKhz,
                  raptorCfg.bwKhz, raptorCfg.preambleBits);
    return true;
}

static bool configureMesh() {
    SpiBusGuard guard(spiLock);

    int state = meshRadio->begin(
        meshCfg.freqMHz, meshCfg.bwKhz, meshCfg.sf, meshCfg.cr,
        meshCfg.syncWord, meshCfg.powerDbm, 16, TCXO_VOLTAGE_V, false);
    if (state != RADIOLIB_ERR_NONE) {
        Serial.printf("[MESH] LoRa init failed: %d\n", state);
        return false;
    }

    meshRadio->setDio2AsRfSwitch(USE_DIO2_AS_RF_SWITCH);
    // Meshtastic runs an explicit header with CRC on.
    meshRadio->setCRC(true);
    meshRadio->setPacketReceivedAction(onMeshIrq);

    state = meshRadio->startReceive();
    if (state != RADIOLIB_ERR_NONE) {
        Serial.printf("[MESH] startReceive failed: %d\n", state);
        return false;
    }
    Serial.printf("[MESH] listening: %.4f MHz LoRa SF%u BW%.0f CR4/%u sync 0x%02X "
                  "at %d dBm (+PA)\n",
                  meshCfg.freqMHz, meshCfg.sf, meshCfg.bwKhz, meshCfg.cr,
                  meshCfg.syncWord, meshCfg.powerDbm);
    return true;
}

// -------------------------------------------------------------- receive ----

static void serviceRaptor() {
    uint8_t packet[MAX_PACKET];
    int len;
    float rssi, snr;

    {
        SpiBusGuard guard(spiLock);
        len = raptorRadio->getPacketLength();
        if (len <= 0 || len > (int)MAX_PACKET) {
            raptorRadio->startReceive();
            return;
        }
        int state = raptorRadio->readData(packet, len);
        rssi = raptorRadio->getRSSI();
        // GFSK has no SNR. RadioLib returns RADIOLIB_ERR_WRONG_MODEM (-20) for
        // a non-LoRa modem, which is an error code, not a measurement.
        snr  = SNR_NOT_AVAILABLE;
        raptorRadio->startReceive();
        if (state != RADIOLIB_ERR_NONE) return;
    }

    raptorTotal++;

    // The payload's own sync word leads every packet; anything else on this
    // frequency is not ours.
    if (len < 12 || memcmp(packet, RAPTOR_SYNC, 4) != 0) {
        raptorNoSync++;
        return;
    }

    uint32_t want = ((uint32_t)packet[len-4] << 24) | ((uint32_t)packet[len-3] << 16)
                  | ((uint32_t)packet[len-2] << 8)  |  (uint32_t)packet[len-1];
    if (crc32(packet, len - 4) != want) {
        raptorBadCrc++;
        return;
    }

    raptorFwd++;
    sendFrame(Serial, FRAME_RAPTOR, packet, len, rssi, snr);
}

static void serviceMesh() {
    uint8_t packet[MAX_PACKET];
    int len;
    float rssi, snr;

    {
        SpiBusGuard guard(spiLock);
        len = meshRadio->getPacketLength();
        if (len <= 0 || len > (int)MAX_PACKET) {
            meshRadio->startReceive();
            return;
        }
        int state = meshRadio->readData(packet, len);
        rssi = meshRadio->getRSSI();
        snr  = meshRadio->getSNR();
        meshRadio->startReceive();
        if (state != RADIOLIB_ERR_NONE) return;
    }

    // Meshtastic packets are forwarded whole and still encrypted. The ground
    // station holds the channel keys; this modem deliberately does not, so a
    // borrowed board never carries them.
    meshRx++;
    sendFrame(Serial, FRAME_MESHTASTIC, packet, len, rssi, snr);
}

// ------------------------------------------------------------- transmit ----

static bool transmitMesh(const uint8_t* data, size_t len) {
    if (!meshReady) return false;
    if (!arbiter.acquire(SLOT_MESHTASTIC, 3000)) {
        meshTxFail++;
        Serial.println("[MESH] transmit refused: the other radio holds the token");
        return false;
    }

    int state;
    {
        SpiBusGuard guard(spiLock);
        state = meshRadio->transmit(const_cast<uint8_t*>(data), len);
        meshRadio->startReceive();
    }
    arbiter.release();

    if (state == RADIOLIB_ERR_NONE) { meshTx++; return true; }
    meshTxFail++;
    Serial.printf("[MESH] transmit failed: %d\n", state);
    return false;
}

// -------------------------------------------------------------- commands ---

static int hexNibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// CFG:<freq>,<bitrate>,<dev>,<bandwidth>,<preamble>   — RAPTOR slot
// MCFG:<freq>,<bw>,<sf>,<cr>,<power>                  — Meshtastic slot
// MTX:<hex>                                           — transmit a raw packet
// STATUS                                              — print the stats line
static void handleCommand(const String& line) {
    if (line.startsWith("CFG:")) {
        float f, br, dev, bw; int pre;
        if (sscanf(line.c_str() + 4, "%f,%f,%f,%f,%d", &f, &br, &dev, &bw, &pre) != 5) {
            Serial.println("CFG_ERR:parse"); return;
        }
        if (f < BAND_MIN_MHZ || f > BAND_MAX_MHZ) {
            Serial.printf("CFG_ERR:%.1f MHz is outside this board's 850-930 MHz hardware\n", f);
            return;
        }
        // Keep what is working. Reporting the failure is not enough on its
        // own: without the rollback the slot stays dead, configured with the
        // settings that just killed it, until someone sends another CFG.
        RaptorConfig previous = raptorCfg;
        raptorCfg = { f, br, dev, bw, (uint16_t)pre };
        raptorReady = configureRaptor();
        if (raptorReady) {
            Serial.printf("CFG_OK:%.1f,%.1f,%.1f,%.1f,%d\n", f, br, dev, bw, pre);
        } else {
            raptorCfg = previous;
            raptorReady = configureRaptor();
            Serial.println(raptorReady
                ? "CFG_ERR:radio refused the settings; the previous ones are back"
                : "CFG_ERR:radio refused the settings and would not restore the old "
                  "ones; the image slot is deaf until reset");
        }
        return;
    }

    if (line.startsWith("MCFG:")) {
        float f, bw; int sf, cr, pwr;
        if (sscanf(line.c_str() + 5, "%f,%f,%d,%d,%d", &f, &bw, &sf, &cr, &pwr) != 5) {
            Serial.println("MCFG_ERR:parse"); return;
        }
        if (f < BAND_MIN_MHZ || f > BAND_MAX_MHZ) {
            Serial.printf("MCFG_ERR:%.4f MHz is outside this board's 850-930 MHz hardware\n", f);
            return;
        }
        if (pwr > SX1262_MAX_DBM) {
            // Clamping rather than refusing: the caller asked for more reach,
            // and the module's PA already provides it. Going above +22 dBm at
            // the chip overdrives that PA instead of helping.
            Serial.printf("MCFG_WARN:clamping %d dBm to %d; the E22 PA adds ~8 dB\n",
                          pwr, SX1262_MAX_DBM);
            pwr = SX1262_MAX_DBM;
        }
        MeshConfig previous = meshCfg;
        meshCfg = { f, bw, (uint8_t)sf, (uint8_t)cr, meshCfg.syncWord, (int8_t)pwr };
        meshReady = configureMesh();
        if (meshReady) {
            Serial.printf("MCFG_OK:%.4f,%.0f,%d,%d,%d\n", f, bw, sf, cr, pwr);
        } else {
            meshCfg = previous;
            meshReady = configureMesh();
            Serial.println(meshReady
                ? "MCFG_ERR:radio refused the settings; the previous ones are back"
                : "MCFG_ERR:radio refused the settings and would not restore the old "
                  "ones; the Meshtastic slot is deaf until reset");
        }
        return;
    }

    if (line.startsWith("MTX:")) {
        const char* hex = line.c_str() + 4;
        size_t hexLen = strlen(hex);
        if (hexLen == 0 || hexLen % 2 || hexLen / 2 > MAX_PACKET) {
            Serial.println("MTX_ERR:length"); return;
        }
        uint8_t buf[MAX_PACKET];
        for (size_t i = 0; i < hexLen / 2; i++) {
            int hi = hexNibble(hex[i*2]), lo = hexNibble(hex[i*2+1]);
            if (hi < 0 || lo < 0) { Serial.println("MTX_ERR:hex"); return; }
            buf[i] = (uint8_t)((hi << 4) | lo);
        }
        Serial.println(transmitMesh(buf, hexLen / 2) ? "MTX_OK" : "MTX_ERR:radio");
        return;
    }

    if (line.startsWith("STATUS")) { lastStatsMs = 0; return; }
}

static void serviceUsb() {
    static String buffer;
    while (Serial.available()) {
        char c = Serial.read();
        if (c != '\n' && c != '\r') {
            if (buffer.length() < 600) buffer += c;
            continue;
        }
        if (buffer.length()) { handleCommand(buffer); buffer = ""; }
    }
}

// ----------------------------------------------------------------- setup ---

void setup() {
    Serial.begin(921600);
    delay(1500);

    Serial.println("\n========================================");
    Serial.println("RaptorHAB Dual-900 Ground Station Modem");
    Serial.println("PingPong Dual RF — 2 x E22-900M30S");
    Serial.println("========================================\n");

#if !SLOT_A_FILTER_BYPASSED
    Serial.println("[WARNING] This build has not been told the slot A 433 MHz");
    Serial.println("[WARNING] low-pass filter was bypassed. At 915 MHz that");
    Serial.println("[WARNING] filter attenuates roughly 60 dB, so the Meshtastic");
    Serial.println("[WARNING] radio will hear nothing and be heard by nobody.");
    Serial.println("[WARNING] Replace L2/L4/L6 with 0 ohm links, remove");
    Serial.println("[WARNING] C1/C3/C5/C7, then rebuild with");
    Serial.println("[WARNING] -DSLOT_A_FILTER_BYPASSED=1\n");
#endif

    pinMode(PIN_USER_BUTTON, INPUT_PULLUP);

    spiLock.begin();
    arbiter.begin();
    spiBus.begin(PIN_SPI_SCK, PIN_SPI_MISO, PIN_SPI_MOSI);

    raptorRadio = new SX1262(new Module(SLOT_B_PINS.nss, SLOT_B_PINS.dio1,
                                        SLOT_B_PINS.rst, SLOT_B_PINS.busy, spiBus));
    meshRadio   = new SX1262(new Module(SLOT_A_PINS.nss, SLOT_A_PINS.dio1,
                                        SLOT_A_PINS.rst, SLOT_A_PINS.busy, spiBus));

    Serial.println("[RADIO] bringing up slot B (RAPTOR downlink)...");
    raptorReady = configureRaptor();
    Serial.println("[RADIO] bringing up slot A (Meshtastic)...");
    meshReady = configureMesh();

    if (!raptorReady && !meshReady) {
        Serial.println("[ERROR] neither radio initialised — check SPI wiring");
    }

    Serial.println("\n[READY] both receivers run continuously; transmissions are");
    Serial.println("[READY] serialised and the receive time they cost is reported.");
    Serial.println("[READY] Commands: CFG: MCFG: MTX: STATUS\n");
}

// ------------------------------------------------------------------ loop ---

void loop() {
    if (raptorIrq) { raptorIrq = false; if (raptorReady) serviceRaptor(); }
    if (meshIrq)   { meshIrq   = false; if (meshReady)   serviceMesh(); }

    serviceUsb();

    if (millis() - lastStatsMs >= 10000) {
        lastStatsMs = millis();
        float rate = raptorTotal ? (100.0f * raptorFwd / raptorTotal) : 0.0f;
        Serial.printf("\n[STATS] Total:%lu Fwd:%lu NoRAPT:%lu BadCRC:%lu Rate:%.1f%% "
                      "| Mesh rx:%lu tx:%lu fail:%lu "
                      "| RX blinded by own TX: %lu.%lu%%\n",
                      raptorTotal, raptorFwd, raptorNoSync, raptorBadCrc, rate,
                      meshRx, meshTx, meshTxFail,
                      arbiter.blindPermille(millis()) / 10,
                      arbiter.blindPermille(millis()) % 10);
    }
}
