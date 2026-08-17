# RaptorHAB Ground Station

A macOS application for tracking high-altitude balloon flights. Receive live telemetry and images from your RaptorHAB payload, view the flight path on a map, predict landing locations, and share data with the SondeHub network.

![RaptorHAB Ground Station]

## Requirements

- macOS 13.0 (Ventura) or later
- RaptorHAB Modem (Heltec T190) connected via USB **or** RTL-SDR dongle
- Optional: USB GPS receiver for ground station position
- Optional: Antenna rotator with rotctld support

## Getting Started

### 1. Connect Your Modem

1. Plug the RaptorHAB Modem into your Mac via USB-C
2. Launch RaptorHAB Ground Station
3. In the sidebar, ensure **Serial (SX1262)** mode is selected
4. Select your modem's serial port from the dropdown (usually contains "usbmodem" or "SLAB")
5. Click **Refresh** if the port doesn't appear, or **Auto** to auto-detect

### 2. Configure Radio Settings

Click the **gear icon** in the toolbar to open Radio Settings:

| Setting | Description | Default |
|---------|-------------|---------|
| Frequency | RF center frequency in MHz | 915.0 |
| Bitrate | Data rate in kbps | 96.0 |
| Deviation | FSK deviation in kHz | 50.0 |
| Bandwidth | Receiver bandwidth in kHz | 467.0 |
| Preamble | Preamble length in bits | 32 |

These settings must match your balloon payload's transmitter configuration.

### 3. Start Receiving

Click the green **Play** button in the toolbar. The modem will be configured and begin listening for packets. When telemetry is received:

- Signal strength (RSSI/SNR) appears in the toolbar
- Telemetry cards populate with live data
- The balloon's position appears on the map
- Altitude history begins plotting

Click the red **Stop** button to end the session.

---

## Features

### Telemetry View

Displays live telemetry data in easy-to-read cards:

- **Altitude** — Current altitude in meters
- **Speed** — Ground speed in m/s
- **Battery** — Payload battery voltage
- **RSSI** — Signal strength from payload's radio
- **CPU Temp** — Payload processor temperature
- **Satellites** — GPS satellite count and fix type
- **Heading** — Direction of travel
- **Sequence** — Packet sequence number

Below the cards, an altitude chart shows the flight profile over time, and a table lists all received telemetry packets.

### Map View

Interactive map showing:

- **Balloon position** — Current payload location with altitude label
- **Flight path** — Track of all received positions
- **Ground station** — Your location (if GPS connected)
- **Bearing line** — Direct line from ground station to balloon
- **Landing prediction** — Predicted landing zone with confidence circle

**Map Controls:**
- Toggle between Apple Maps and offline tiles
- Download map tiles for offline use in areas without internet

### Flight Graphs

Detailed charts of flight data over time:

- Altitude profile
- Speed
- Battery voltage
- Temperature
- Signal quality

Use these graphs to analyze flight performance and identify anomalies.

### Landing Predictions

The app continuously predicts where the balloon will land based on:

- Current position and altitude
- Descent rate (calculated from vertical speed)
- Wind data from Open-Meteo (multi-altitude wind profiles)

**Prediction Settings:**
- **Burst altitude** — Expected maximum altitude before descent
- **Descent rates** — Expected descent speed at burst and near landing
- **Wind source** — Automatic from API or manual entry

Predictions update in real-time during descent, becoming more accurate as the balloon gets lower.

### Images

View images transmitted from the balloon payload:

- **Thumbnails** — Grid of all received images
- **Progress indicators** — Shows download progress for images in transit
- **Full-size viewer** — Click any image to view at full resolution with zoom/pan
- **Export** — Save images as WebP, JPEG, PNG, or TIFF

Images are received via RaptorQ fountain coding, allowing recovery even with significant packet loss.

### Missions

All flight data is automatically saved as a "mission" for later review:

- **Auto-recording** — Starts automatically when telemetry is received
- **Mission browser** — View past flights with summary statistics
- **Playback** — Review telemetry and images from previous flights
- **Export** — Save complete mission data as a ZIP archive
- **Custom folder** — Choose where missions are stored

### Packet Log

Raw packet log for debugging:

- View all received packets with timestamps
- See packet types (telemetry, image metadata, image data)
- Monitor for errors or malformed packets

---

## Sidebar Options

### GPS (Ground Station Position)

Connect a USB GPS to display your position on the map and enable bearing/distance calculations.

1. Connect a USB GPS receiver (NMEA compatible)
2. Select the GPS serial port from the dropdown
3. Set the appropriate baud rate (typically 9600 or 115200)
4. Enable GPS tracking

Your position is used for:
- Bearing and distance to balloon
- Elevation angle for antenna pointing
- SondeHub uploader position

### Landing Prediction

Configure landing prediction parameters:

- **Burst Altitude** — Set expected burst altitude
- **Descent Rates** — Adjust descent rate model
- **Wind** — Enable automatic wind data from Open-Meteo

Click **Fetch Wind** to manually refresh wind profile data.

### Recording

Control mission recording:

- **Auto-start** — Begin recording when telemetry arrives
- **Missions folder** — Choose storage location
- **Stop/Save** — End recording and save mission

### Alerts

Audio notifications for flight events:

| Alert | Trigger |
|-------|---------|
| Telemetry Received | First packet received |
| Burst Detected | Balloon begins descending |
| Signal Lost | No packets for configured timeout |
| Low Altitude | Balloon below threshold during descent |
| Landed | Balloon stopped moving at low altitude |

Enable **Speak Alerts** to have events announced via text-to-speech.

### Antenna Rotator

Automatic antenna tracking via rotctld protocol:

1. Start rotctld on your computer (part of Hamlib)
2. Enter the host and port (default: 127.0.0.1:4533)
3. Enable **Auto Track** to point antenna at balloon
4. Set **Park Position** for when tracking stops

The rotator will follow the balloon's bearing and elevation automatically.

### SondeHub

Share flight data with the global SondeHub Amateur network:

1. Enter your **Uploader Callsign** (your amateur radio call or identifier)
2. Enter the **Payload Callsign** (balloon identifier)
3. Enable **Upload Telemetry**
4. Optionally add a comment

Your balloon will appear on [amateur.sondehub.org](https://amateur.sondehub.org) for others to track.

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| ⌘1-7 | Switch tabs (Telemetry, Map, Graphs, etc.) |
| Space | Start/Stop receiving |
| ⌘, | Open Settings |
| ⌘R | Refresh serial ports |

---

## Input Modes

### Serial (SX1262) — Recommended

Use the RaptorHAB Modem (Heltec T190) connected via USB. The modem handles all RF reception and sends decoded packets to the app.

**Advantages:**
- Higher sensitivity than RTL-SDR
- Hardware FSK demodulation
- Portable, battery-powered operation
- Shows RSSI and SNR from the SX1262

### RTL-SDR

Use an RTL-SDR dongle for direct RF reception. The app performs software FSK demodulation.

**Advantages:**
- Lower cost hardware
- Wider frequency range
- Software-adjustable parameters

**Note:** RTL-SDR mode requires more CPU and may have lower sensitivity than the dedicated SX1262 modem.

---

## Offline Maps

For field use without internet:

1. Go to **Map View** → click the download icon
2. Center the map on your expected flight area
3. Select zoom levels to download
4. Click **Download Tiles**

Tiles are cached locally and used automatically when offline mode is enabled.

---

## Data Storage

### Mission Files

Missions are stored in `~/Documents/RaptorHabGS/Missions/` by default:

```
Mission_2026-01-25_143022/
├── mission.json          # Mission metadata
├── telemetry.json        # All telemetry points
└── images/
    ├── image_001.webp
    ├── image_002.webp
    └── ...
```

### Exported Data

- **CSV Export** — Telemetry table can be exported as CSV
- **Image Export** — Images can be saved as WebP, JPEG, PNG, or TIFF
- **Mission ZIP** — Complete mission archive for sharing

---

## Troubleshooting

### Modem Not Detected

- Ensure the USB cable supports data (not charge-only)
- Try a different USB port
- Check System Settings → Privacy & Security → USB access
- Click **Refresh** to rescan serial ports

### No Telemetry Received

- Verify radio settings match the payload
- Check antenna connection on the modem
- Ensure payload is transmitting
- Monitor the modem's screen for signal indicators

### Weak Signal / Packet Errors

- Use a directional antenna pointed at the balloon
- Elevate your receiving antenna
- Move away from sources of RF interference
- Enable antenna rotator for auto-tracking

### GPS Not Working

- Ensure GPS has clear sky view
- Check serial port selection and baud rate
- Wait for GPS to acquire satellites (can take several minutes)

### Map Not Loading

- Check internet connection
- Download offline tiles for the area
- Switch to offline map mode

---

## Support

For issues, feature requests, or questions:

- File an issue on GitHub
- Join the RaptorHAB community

