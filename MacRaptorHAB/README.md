# RaptorHab Ground Station for macOS

A native macOS application for receiving and decoding telemetry from RaptorHab high-altitude balloon payloads using an RTL-SDR receiver.

## Features

- **RTL-SDR Integration**: Direct USB support via librtlsdr
- **FSK Demodulation**: Software-defined radio receiver for 96 kbps FSK
- **Real-time Telemetry**: Live display of GPS position, altitude, battery, temperatures
- **Map Tracking**: Interactive map with flight path visualization
- **Image Reception**: Fountain code decoding for transmitted images
- **Data Logging**: Automatic CSV export and packet logging
- **Text Messages**: Display of text messages from the payload

## Requirements

### Hardware
- RTL-SDR USB dongle (RTL2832U-based)
- Antenna tuned for 915 MHz (or your configured frequency)

### Software
- macOS 14.0 (Sonoma) or later
- Xcode 15.0 or later
- librtlsdr (install via Homebrew)

## Installation

### 1. Install librtlsdr

Using Homebrew:
```bash
brew install librtlsdr
```

Or manually from source:
```bash
git clone https://github.com/osmocom/rtl-sdr.git
cd rtl-sdr
mkdir build && cd build
cmake ..
make
sudo make install
```

### 2. Build the Application

1. Open `RaptorHabGS.xcodeproj` in Xcode
2. Select your development team for code signing
3. Build and run (⌘R)

### 3. First Run

1. Connect your RTL-SDR device
2. Click "Scan Devices" in the Radio Configuration panel
3. Select your device and configure the frequency (default: 915 MHz)
4. Click "Start" to begin receiving

## Configuration

### Radio Settings

| Parameter | Default | Description |
|-----------|---------|-------------|
| Frequency | 915.0 MHz | Center frequency (must match airborne unit) |
| Bit Rate | 96,000 bps | FSK symbol rate |
| Frequency Deviation | 50,000 Hz | FSK deviation |
| Sample Rate | 1,000,000 Hz | RTL-SDR sample rate |
| Gain | 40 dB | Tuner gain (0 = auto) |

These settings match the RaptorHab airborne payload configuration.

## Protocol Details

### Packet Structure

```
┌──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
│ Sync (4) │ Type (1) │ Seq (2)  │ Flags(1) │ Payload  │ CRC (4)  │
│  "RAPT"  │          │          │          │ (var)    │ CRC-32   │
└──────────┴──────────┴──────────┴──────────┴──────────┴──────────┘
```

### Packet Types

| Type | ID | Description |
|------|-----|-------------|
| Telemetry | 0x00 | GPS position, sensors, status (36 bytes) |
| Image Meta | 0x01 | Image metadata for fountain decoding (22 bytes) |
| Image Data | 0x02 | Fountain-coded image symbol (206 bytes) |
| Text Message | 0x03 | Variable-length text string |

### Telemetry Payload (36 bytes)

| Field | Type | Scale | Description |
|-------|------|-------|-------------|
| Latitude | int32 | ×1e7 | Degrees |
| Longitude | int32 | ×1e7 | Degrees |
| Altitude | uint32 | ×1000 | Meters |
| Speed | uint16 | ×100 | m/s |
| Heading | uint16 | ×100 | Degrees |
| Satellites | uint8 | - | Count |
| Fix Type | uint8 | - | 0=None, 1=2D, 2=3D |
| GPS Time | uint32 | - | Unix timestamp |
| Battery | uint16 | - | Millivolts |
| CPU Temp | int16 | ×100 | Celsius |
| Radio Temp | int16 | ×100 | Celsius |
| Image ID | uint16 | - | Current image |
| Image Progress | uint8 | - | Percent |
| RSSI | int8 | - | dBm |
| Reserved | 4 bytes | - | Future use |

## Modulation Details

The RaptorHab payload uses FSK (Frequency Shift Keying) modulation:

- **Modulation**: 2-FSK
- **Bit Rate**: 96,000 bps
- **Frequency Deviation**: ±50 kHz
- **Sync Word**: "RAPT" (0x52415054)
- **Preamble**: 32 bits

The ground station performs:
1. IQ sample acquisition from RTL-SDR
2. Decimation and low-pass filtering
3. Quadrature FM demodulation
4. Mueller-Muller clock recovery
5. Bit slicing and sync word detection
6. CRC-32 verification
7. Payload deserialization

## Image Transmission

Images are encoded using Luby Transform (LT) fountain codes:

1. Image is split into source symbols (200 bytes each)
2. Encoded symbols are transmitted with redundancy
3. Receiver reconstructs image when enough symbols received
4. CRC-32 verifies image integrity

This allows image recovery even with significant packet loss.

## Data Storage

Data is stored in `~/Documents/RaptorHabGS/`:

```
RaptorHabGS/
├── packets_YYYY-MM-DD_HH-mm-ss.log  # Raw packet log
├── telemetry/
│   └── telemetry_UUID.json           # Individual telemetry points
└── images/
    └── image_ID_timestamp.webp       # Decoded images
```

## Troubleshooting

### RTL-SDR Not Detected

1. Ensure the device is connected
2. Check for kernel driver conflicts:
   ```bash
   sudo kextunload -b com.apple.driver.usb.IOUSBHostHIDDevice
   ```
3. Try a different USB port

### No Packets Received

1. Verify antenna is connected and tuned for 915 MHz
2. Increase gain setting
3. Check that frequency matches the airborne payload
4. Verify the airborne payload is transmitting

### Poor Signal Quality

1. Use a directional antenna pointed at the payload
2. Minimize cable length between antenna and RTL-SDR
3. Move away from RF interference sources
4. Add a 915 MHz bandpass filter

### Library Loading Failed

Ensure librtlsdr is installed in a standard location:
```bash
# Check library location
ls -la /usr/local/lib/librtlsdr.dylib
ls -la /opt/homebrew/lib/librtlsdr.dylib
```

## Development

### Project Structure

```
RaptorHabGS/
├── RaptorHabGS.xcodeproj
└── RaptorHabGS/
    ├── RaptorHabGSApp.swift      # App entry point
    ├── ContentView.swift          # Main UI
    ├── Protocol.swift             # Packet definitions
    ├── RTLSDRManager.swift        # RTL-SDR interface
    ├── FSKDemodulator.swift       # FSK demodulation
    ├── GroundStationManager.swift # Main coordinator
    ├── Assets.xcassets/
    └── RaptorHabGS.entitlements
```

### Key Classes

- **RTLSDRManager**: Handles RTL-SDR device communication via librtlsdr
- **FSKDemodulator**: Implements FM demodulation and bit recovery
- **GroundStationManager**: Coordinates all components and manages state
- **PacketParser**: Validates and parses received packets

## License

This project is part of the RaptorHab high-altitude balloon system.

## Credits

- RTL-SDR drivers: [osmocom/rtl-sdr](https://github.com/osmocom/rtl-sdr)
- RaptorHab project by KD2NDR
