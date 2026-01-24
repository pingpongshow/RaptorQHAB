# RaptorHab - High Altitude Balloon Image & Telemetry System

RaptorHab is a complete high-altitude balloon (HAB) telemetry and imagery downlink system consisting of airborne payload and ground station components.

## Features

- **915 MHz FSK Radio Link** - 200 kbps data rate, FCC Part 15.247 compliant
- **Fountain Codes** - Robust error correction using RaptorQ or LT codes
- **WebP Image Compression** - Efficient image transmission (~50KB per image)
- **GPS Telemetry** - Real-time position tracking with airborne dynamic model
- **Web Interface** - Real-time dashboard, map tracking, image gallery

## Hardware Requirements

### Airborne Payload
- Raspberry Pi Zero 2W
- Waveshare SX1262 868M/915M LoRa HAT
- Sony IMX219 Camera (Pi Camera Module v2)
- L76 GPS built into the Waveshare SX1262 hat

### Ground Station
- Raspberry Pi Zero 2W (or any Pi)
- Waveshare SX1262 868M/915M LoRa HAT with GPS module
- Web browser for UI

## Installation

Pre-build Raspberry Pi image, contains both airborne and groundstation - this is the easiest wat to get started!!!
download from https://www.dropbox.com/scl/fo/6gg3gslbegtxcpt9paqfd/ADLQRzHeOkjQu1rJ2CoCmxU?rlkey=czmw5ctd03qi6r9cold0atwao&st=9gicknop&dl=0

See ReadMe.pdf in the /Documentation folder for most updated installation and use instructions.

## Ground Station Web Interface

The ground station provides a web interface with:

### Dashboard
- Real-time telemetry display (position, altitude, speed, battery, temperatures)
- Signal quality indicators (RSSI, packet counts)
- Mini map with current position
- Latest received image preview
- Quick command buttons

### Map View
- Full-screen flight track visualization
- Multiple map layers (OpenStreetMap, Satellite, Terrain)
- Altitude profile chart
- Track export (KML, CSV)

### Image Gallery
- Thumbnail grid of received images
- Filter by time (all, last hour, today)
- Full-size image viewer
- Download capability
- Progress indicators for pending images

### Commands
- Ping, Capture Image buttons
- Parameter settings (TX power, image quality, capture interval)
- Reboot command with confirmation
- Command log and statistics

## Protocol Overview

### Packet Structure

```
┌─────────┬──────┬───────┬───────┬─────────┬───────┐
│ SYNC(4) │ TYPE │ SEQ(2)│ FLAGS │ PAYLOAD │ CRC32 │
│ "RAPT"  │ (1)  │       │ (1)   │ (var)   │ (4)   │
└─────────┴──────┴───────┴───────┴─────────┴───────┘
```

### Packet Types

| Type | Name | Direction |
|------|------|-----------|
| 0x00 | TELEMETRY | Air → Ground |
| 0x01 | IMAGE_META | Air → Ground |
| 0x02 | IMAGE_DATA | Air → Ground |
| 0x03 | TEXT_MSG | Air → Ground |
| 0x10 | CMD_ACK | Air → Ground |
| 0x80 | CMD_PING | Ground → Air |
| 0x81 | CMD_SETPARAM | Ground → Air |
| 0x82 | CMD_CAPTURE | Ground → Air |
| 0x83 | CMD_REBOOT | Ground → Air |

## State Machine

```
INITIALIZING → TX_ACTIVE ↔ RX_LISTEN
                   ↓
             ERROR_STATE
              (reboot)
```

- **TX_ACTIVE** (10 sec): Transmit telemetry and images
- **RX_LISTEN** (10 sec): Listen for commands, send ACKs
- **ERROR_STATE**: Too many errors, automatic reboot

