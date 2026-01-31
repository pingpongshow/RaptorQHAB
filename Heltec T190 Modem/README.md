# RaptorHAB Modem

A ground station radio modem for the RaptorHAB high-altitude balloon tracking system. Built on the Heltec Vision Master T190, this modem receives telemetry and image packets via LoRa FSK and forwards them to companion apps over USB and WiFi.

## Features

- **Dual Connectivity**: Simultaneous USB serial and WiFi packet forwarding
- **SX1262 LoRa Radio**: High-sensitivity FSK reception with configurable parameters
- **1.9" TFT Display**: Real-time status showing signal quality, packet statistics, radio settings, WiFi status, and battery level
- **Runtime Configuration**: Radio parameters configurable via USB or WiFi before reception begins
- **Battery Monitoring**: On-screen battery voltage and percentage with color-coded indicator
- **Packet Validation**: CRC32 verification and "RAPT" sync word filtering
- **iOS & macOS Support**: Works with RaptorHAB companion apps on both platforms

## Hardware Requirements

### Required

- **Heltec Vision Master T190** (ESP32-S3 + SX1262 + 1.9" ST7789 TFT)
  - ESP32-S3 dual-core 240MHz
  - SX1262 LoRa transceiver (150MHz - 960MHz)
  - 320x170 color TFT display
  - USB-C connector
  - Built-in battery charging circuit

### Optional

- **3.7V LiPo Battery**: For portable operation (JST connector on board)
- **915MHz Antenna**: SMA connector, tuned for your frequency band

## Installation

### Prerequisites

1. Install [PlatformIO](https://platformio.org/install) (VS Code extension recommended)
2. Install USB drivers if needed (CP2102 or native USB CDC)

### Build & Flash

1. Clone or download this repository

2. Open the project folder in VS Code with PlatformIO

3. Connect the T190 via USB-C

4. Build and upload:
   ```
   pio run -t upload
   ```

5. Open the serial monitor to verify:
   ```
   pio device monitor
   ```

### Dependencies

The following libraries are automatically installed by PlatformIO:

| Library | Version | Purpose |
|---------|---------|---------|
| RadioLib | ^6.4.0 | SX1262 radio driver |
| Adafruit GFX Library | ^1.11.9 | Graphics primitives |
| Adafruit ST7735 and ST7789 Library | ^1.10.3 | TFT display driver |
| Adafruit BusIO | ^1.15.0 | SPI/I2C abstraction |

## Configuration

### On Boot

The modem waits up to 2 minutes for configuration from USB or WiFi. If no configuration is received, it starts with default parameters.

### Configuration Command

Send via USB serial or WiFi:

```
CFG:<frequency>,<bitrate>,<deviation>,<bandwidth>,<preamble>\n
```

**Parameters:**
| Parameter | Unit | Default | Description |
|-----------|------|---------|-------------|
| frequency | MHz | 915.0 | RF center frequency |
| bitrate | kbps | 96.0 | FSK data rate |
| deviation | kHz | 50.0 | FSK frequency deviation |
| bandwidth | kHz | 467.0 | Receiver bandwidth |
| preamble | bits | 32 | Preamble length |

**Example:**
```
CFG:915.0,96.0,50.0,467.0,32
```

**Response:**
- Success: `CFG_OK:915.0,96.0,50.0,467.0,32`
- Error: `CFG_ERR:<message>`

### Default Radio Settings

- **Frequency**: 915.0 MHz (US ISM band)
- **Modulation**: FSK with 0.5 Gaussian shaping
- **Bitrate**: 96 kbps
- **Deviation**: ±50 kHz
- **Bandwidth**: 467 kHz
- **Sync Word**: "RAPT" (0x52 0x41 0x50 0x54)

## Bluetooth LE

### Connection Details

| Property | Value |
|----------|-------|
| Device Name | `RaptorModem` |
| Passkey | `123456` |
| Service | Nordic UART Service (NUS) |
| Service UUID | `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` |
| RX Characteristic | `6E400002-B5A3-F393-E0A9-E50E24DCCA9E` (Write) |
| TX Characteristic | `6E400003-B5A3-F393-E0A9-E50E24DCCA9E` (Notify) |


For packets exceeding MTU, chunked format is used:

```
[CHK][chunk#][total chunks][data...]
```

## USB Serial Protocol

**Baud Rate**: 921600

**Frame Format**:
```
[0x7E][LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM][0x7E]
```

- `0x7E`: Frame delimiter (byte-stuffed in payload)
- `LEN`: 16-bit packet length
- `RSSI`: Signed integer + fractional (e.g., -85.50 dBm)
- `SNR`: Signed integer + fractional (e.g., 7.25 dB)
- `CHECKSUM`: XOR of all bytes between delimiters

## Display Layout

```
┌────────────────────────────────────────┬──────────┐
│ RAPTORHAB MODEM                        │ ████ 4.1V│
├────────────────────────────────────────┴──────────┤
│ RADIO SETTINGS                                    │
│ FREQ: 915.0 MHz    BW: 467 kHz                    │
│ BR: 96 kbps        PRE: 32 bits                   │
│ DEV: 50 kHz        CFG: USB                       │
├───────────────────────────────────────────────────┤
│ SIGNAL                         BLUETOOTH          │
│ -85 dBm   7.2 dB               CONNECTED          │
├───────────────────────────────────────────────────┤
│ STATISTICS                                        │
│ RX: 142  FWD: 138  ERR: 4  RATE: 97.2%           │
│ TELEM: 98  IMAGE: 40  BLE: ON  MTU: 512          │
└───────────────────────────────────────────────────┘
```

### Signal Quality Indicators

| Metric | Good (Green) | Warning (Yellow) | Poor (Red) |
|--------|--------------|------------------|------------|
| RSSI | > -80 dBm | -80 to -100 dBm | < -100 dBm |
| SNR | > 5 dB | 0 to 5 dB | < 0 dB |
| Battery | > 50% | 20-50% | < 20% |

## Serial Statistics Output

Every 10 seconds, the modem prints statistics to USB serial:

```
[STATS] Total:142 Fwd:138 NoRAPT:2 BadCRC:2 Err:0 Rate:97.2% BLE:Connected Batt:4.12V(95%)
```

## Packet Validation

Incoming packets must pass two checks:

1. **Sync Word**: First 4 bytes must be "RAPT" (0x52 0x41 0x50 0x54)
2. **CRC32**: Last 4 bytes must match IEEE 802.3 CRC32 of payload

Packets failing validation are counted but not forwarded.

## Troubleshooting

### No Serial Output
- Ensure USB-C cable supports data (not charge-only)
- Check that USB CDC is enabled in build flags
- Try pressing reset button after connecting

### Radio Not Receiving
- Verify antenna is connected
- Check frequency matches transmitter
- Ensure sync word matches ("RAPT")
- Monitor RSSI — if stuck at -120 dBm, no signal is being received

### Display Not Working
- Check that TFT power pin (GPIO7) is being driven correctly
- Verify backlight enable (GPIO17) is HIGH

## License

MIT License — See LICENSE file for details.

## Related Projects

- **RaptorHAB Tracker**: ESP32-S3 based balloon payload with GPS, sensors, and camera
- **RaptorHAB iOS App**: iPhone/iPad companion app for tracking and image reception
- **RaptorHAB macOS App**: Desktop ground station software
