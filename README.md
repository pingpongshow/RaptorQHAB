# RaptorHab - High Altitude Balloon Image & Telemetry System

RaptorHab is a complete high-altitude balloon (HAB) telemetry and imagery downlink system consisting of airborne payload and ground station components. There are other projects available with a silimar purpose, but there are some distinctive advantages here. Raptor fountian codes, modern image compression, error correction, cross platform. 

Mac native rground station app in releases - uses a custom radio receiver based on the Hetec t190 LoRa board.

iOS app RaptorHABMobile for iOS >17 on iPad and iPhone available in the app store - free - pending Apple approval

Firmware for the Heltec T190 modem (FSK) available - packets over USB or BLE, supports battery

Python GUI and headless web based groundstations are being developed and are currently being debuged - run on a Pi, Windows etc - also uses custom Heltec T190 LoRa board modem

Many code changes due to various bug fixes and additional features have been made since I made the documentation for this project, there are some details that need to be updated and will be update when time permitts.

## Features

- **915 MHz FSK Radio Link** - 96 kbps data rate, adjustable
- **Fountain Codes** - Robust error correction using RaptorQ or LT codes
- **WebP Image Compression** - Efficient image transmission (~50KB per image)
- **GPS Telemetry** - Real-time position tracking with airborne dynamic model
- **Web Interface** - Real-time dashboard, map tracking, image gallery
- **Ground station apps** supporting live tracking, payload heading and distance, landing zone prediction, automatic telemetry/image recording, bust height calculation

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
- Mac ground station app available now using custom radio modem based on Heltec T190 radio
- iOS ground station app RaptorHABMobile available using custom radio modem based on Heltec T190 radio with BLE
- Cross platform python GUI and headless webserver ground stations being developed/debugged

## Installation

Pre-built Raspberry Pi images, contain both airborne and groundstation 
  - this is the easiest way to get started, flash to your Pi SD card with dd or Pi Imager, dont forget to "sudo raspi-config --expand-rootfs" after the first time you flash the new image to youe sd card. Connect to the Pi after flash through SSH on the microUSB data port at 10.0.0.1
    
https://mega.nz/file/3U03zIgY#bumpJyihANdUjG_XCdLndyDg479Gf7k2OrLLD-Lk9i4

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

### Map View
- Full-screen flight track visualization
- Multiple map layers (OpenStreetMap, Satellite, Terrain)
- Altitude profile chart
- Track export (KML, CSV)

### Image Gallery
- Thumbnail grid of received images
- Full-size image viewer
- Download capability
- Progress indicators for pending images

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

Use:
The airborne unit is universal all airborne units, which are currently base on rasberry Pi Zero2W, Waveshare SX1262 hat with GNSS and Rasberry Pi Cam 2 (IMX219) - use the same premade Pi image. Connect to the Airborne unit through SSH (user: raptor, password: raptor) after connecting to the RaptorAir WiFi AP (password RaptorAir). Set up the air unit to run as a service (instructions in documentation). 

The ground unit has a few options:
  1) run the ground unit Pi Zero 2W prebuild image - no camera, but uses the same Waveshare sx1262/GNSS hat board. Connect the Pi Zero 2W connect to its AP (RaptorGround, password: RaptorGround) and SSH into the Pi (user: raptor, password: raptor) run the ground app as needed or setup as a service to run on start (instructions in documentation)
  2)   run ground station on a Mac computer through the Mac native app - this is the most fully featured and best way to run the ground station - you will need to flash a Heltec T190 LoRa module with the custom code to act as a modem, external GPS for the mac is optional, but desirable.
  3)   run the python groundstation code in GUI mode or webserver mode - this could be done on an Pi 3,4,5 or some other computer or even windows. This also requires use of the T190 modem
  4)   iOS app RapttorHABMobile now available on the app store

