# RaptorHab Mobile - WiFi Edition

Full-featured iOS/iPadOS ground station app for RaptorHab high-altitude balloon tracking using WiFi connection to the Heltec modem.

## Features

### Tracking
- **Real-time Telemetry** - Live position, altitude, speed, heading, battery, temperatures
- **Flight Map** - Track balloon path with ground station position, distance, and bearing
- **Telemetry Graphs** - Altitude, speed, battery, temperature, RSSI charts
- **Burst Detection** - Automatic balloon burst detection with alerts

### Predictions
- **Landing Predictions** - Real-time predicted landing zone during flight
- **Wind Profile** - Multi-altitude wind data from Open-Meteo API
- **Flight Planning** - Pre-launch trajectory predictions
- **Balloon Calculator** - Burst altitude and ascent rate calculations

### Data
- **Image Reception** - Receive JPEG images from the payload
- **Text Messages** - Receive status messages from payload
- **Mission Recording** - Record and save flight data for later analysis
- **Mission Playback** - Review past flights with full telemetry
- **CSV Export** - Export telemetry data for analysis

### Maps
- **Offline Maps** - Download map tiles for use without internet
- **OpenStreetMap Tiles** - Free map data via OSM
- **SQLite Cache** - MBTiles format for efficient storage
- **Region Download** - Pre-download maps for expected flight area

### Connectivity
- **WiFi AP** - Wireless connection to RaptorModem (BLE was tried, but it is not fast enough - Apple restricts USB access, so thats not an option)
- **SondeHub Upload** - Share telemetry to SondeHub amateur network
- **Internal GPS** - Uses device's built-in GPS for ground station position

### Alerts
- **Audio Alerts** - Configurable sounds for burst, landing, signal loss
- **Haptic Feedback** - Tactile feedback on packet reception
- **Voice Announcements** - Spoken alerts for key events

## Requirements

- iOS 16.0+ or iPadOS 16.0+
- iPhone or iPad
- RaptorModem (Heltec Vision Master T190) with WiFi firmware

## Bluetooth Pairing

1. Power on the RaptorModem
2. Open RaptorHab app on your iOS device
3. The app will automatically scan for "RaptorModem"
4. When prompted, enter the passkey: **123456**
5. The modem will show "CONNECTED" on its display

## App Structure

```
RaptorHabMobile/
├── RaptorHabMobileApp.swift    # App entry point
├── ContentView.swift           # Main UI with adaptive iPhone/iPad layout
├── GroundStationManager.swift  # Main coordinator
├── LocationManager.swift       # Internal GPS via CoreLocation
├── Protocol.swift              # Packet definitions and parsing
│
├── LandingPrediction.swift     # Landing prediction engine
├── BurstDetection.swift        # Burst detection logic
├── AudioAlertManager.swift     # Audio and haptic alerts
├── MissionManager.swift        # Mission recording and playback
├── SondeHubManager.swift       # SondeHub telemetry upload
├── OfflineMapManager.swift     # Offline map tile caching (SQLite)
│
├── MapView.swift               # Flight tracking map with offline support
├── GraphsView.swift            # Telemetry charts
├── ImagesView.swift            # Image gallery
├── PredictionsView.swift       # Landing prediction UI
├── MissionsView.swift          # Mission history and playback
├── PacketLogView.swift         # Raw packet inspection
├── SettingsView.swift          # Configuration UI
├── OfflineMapSettingsView.swift # Offline map download UI
└── StatusViews.swift           # System status displays
```

## Tabs / Views

| Tab | Description |
|-----|-------------|
| Telemetry | Live flight data, position, system status |
| Map | Flight path, ground station, predicted landing (supports offline tiles) |
| Graphs | Altitude, speed, RSSI, temperature charts |
| Predictions | Landing prediction with wind profiles |
| Images | Received images from payload |
| Missions | Record, save, and playback flights |
| Packets | Raw packet log for debugging |
| Status | Connection, receiver, and upload statistics |
| Offline Maps | Download map tiles for offline use |
| Alerts | Configure audio/haptic alerts |
| SondeHub | Configure telemetry upload |

## BLE Protocol

Uses Nordic UART Service (NUS) style UUIDs:
- **Service UUID**: `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`
- **TX Characteristic** (notify): `6E400003-B5A3-F393-E0A9-E50E24DCCA9E`
- **RX Characteristic** (write): `6E400002-B5A3-F393-E0A9-E50E24DCCA9E`

## Permissions Required

- **Bluetooth** - To connect to RaptorModem
- **Location (When In Use)** - For ground station GPS position
- **Background Modes** (optional) - For continuous tracking

## Building

1. Open `RaptorHabMobile.xcodeproj` in Xcode 15+
2. Set your Development Team in Signing & Capabilities
3. Build and run on your device

## Modem Firmware

The modified modem firmware is in `folder/Modem/src/main.cpp`. Changes from USB-only version:

**Dual Output** - Packets forwarded via USB AND WiFi simultaneously


### Building Modem Firmware

```bash
cd folder
pio run
pio run -t upload
```

## License

Not for commercial use
