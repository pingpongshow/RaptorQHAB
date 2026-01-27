# RaptorHab - High Altitude Balloon Image & Telemetry System

RaptorHab is a complete high-altitude balloon (HAB) telemetry and imagery downlink system consisting of airborne payload and ground station components. There are other projects available with a silimar purpose, but there are some distinctive advantages here. Raptor fountian codes, modern image compression, error correction, cross platform. 

Mac native rground station app in releases - uses a custom radio receiver based on the Hetec t190 LoRa board.

Firmware for the Heltec T190 modem (FSK) available.

Python GUI and headless web based groundstations are being developed and are currently being debuged - run on a Pi, Windows etc - also uses custom Heltec T190 LoRa board modem


## Features

- **915 MHz FSK Radio Link** - 96 kbps data rate, adjustable
- **Fountain Codes** - Robust error correction using RaptorQ or LT codes
- **WebP Image Compression** - Efficient image transmission (~50KB per image)
- **GPS Telemetry** - Real-time position tracking with airborne dynamic model
- **Web Interface** - Real-time dashboard, map tracking, image gallery

## Hardware Requirements

### Airborne Payload
- Raspberry Pi Zero 2W
- Waveshare SX1262 868M/915M LoRa HAT
- Sony IMX219 Camera (Pi Camera Module v2)
- L76 GPS built into the Waveshare SX1262 hat (balloon mode)

### Ground Station
- Raspberry Pi Zero 2W (or any Pi)
- Waveshare SX1262 868M/915M LoRa HAT with GPS module
- Web browser for UI

## Installation

Pre-built Raspberry Pi images, contain both airborne and groundstation - this is the easiest way to get started, flash to your Pi SD card with dd or Pi Imager, dont forget to "sudo raspi-config --expand-rootfs" after the first time you flash the new image to youe sd card. reason for two images linkes is each Pi image is exactly the same except for the WiFi AP name, so if you are familiar with configuring Pi you could likely get away with one image file only.
Airborne Unit download: https://mega.nz/file/LB9WUADY#3Y-65y9NjXHN-LRaacJJJG6SZQ2jdLLwqMGUh5rF4MU
Ground Unit download: https://mega.nz/file/DEdgiKhI#F_BDjiJA3UOEzBZp5sV44614p8mnPsiAuGpoBk3tA_4
SSH logon: raptor, password: raptor
Airborne Unit AP SSID: RaptorAir password: RaptorAir
Ground Unit AP SSID: RaptorGround password: RaptorGround

See ReadMe.pdf in the /Pi/Documentation folder for most updated installation and use instructions.

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

Use:
The airborne unit is universal all airborne units, which are currently base on rasberry Pi Zero2W, Waveshare SX1262 hat with GNSS and Rasberry Pi Cam 2 (IMX219) - use the same premade Pi image. Connect to the Airborne unit through SSH (user: raptor, password: raptor) after connecting to the RaptorAir WiFi AP (password RaptorAir). Set up the air unit to run as a service (instructions in documentation). 

The ground unit has a few options:
  1) run the ground unit Pi Zero 2W prebuild image - no camera, but uses the same Waveshare sx1262/GNSS hat board. Connect the Pi Zero 2W connect to its AP (RaptorGround, password: RaptorGround) and SSH into the Pi (user: raptor, password: raptor) run the ground app as needed or setup as a service to run on start (instructions in documentation)
  2)   run ground station on a Mac computer through the Mac native app - this is the most fully featured and best way to run the ground station - you will need to flash a Heltec T190 LoRa module with the custom code to act as a modem, external GPS for the mac is optional, but desirable.
  3)   run the python groundstation code in GUI mode or webserver mode - this could be done on an Pi 3,4,5 or some other computer or even windows. This also requires use of the T190 modem


There is much to be done on the documentation side of this project that I just havent gotten around to, the project components that are on GitHub are fully functional and ready to go. 

