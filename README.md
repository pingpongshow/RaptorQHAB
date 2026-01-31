# RaptorHab - High Altitude Balloon Image & Telemetry System

RaptorHab is a complete high-altitude balloon (HAB) telemetry and imagery downlink system consisting of airborne payload and ground station components. There are other projects available with a silimar purpose, but there are some distinctive advantages here. Raptor fountian codes, modern image compression, error correction, cross platform. 

Mac native ground station app in releases - uses a custom radio receiver based on the Hetec t190 LoRa board.

Attempted iOS version of the ground station, but Apple restricts USB device access and BLE was incapable of sending the packets fast enough, WiFi is possible but this severely limits the apps functions for features needing internet access - sorry. You could run the groundstation on a Pi Zero 2W and connect to its web GUI with offline maps instead for portable operation. 

Custom modem firmware for the Heltec T190 modem (FSK) available - packets over USB

Python GUI and headless web based groundstations Windows, linux - uses custom modem

Many code changes due to various bug fixes and additional features have been made since I made the documentation for this project, there are some details that need to be updated and will be update when time permitts.

## Features

- **915 MHz FSK Radio Link** - 96 kbps data rate, rate and other radio parameters are adjustable
- **Fountain Codes** - Robust error correction using RaptorQ or LT codes (fallback)
- **WebP Image Compression** - Efficient image transmission (~50KB per image)
- **GPS Telemetry** - Real-time position tracking with airborne dynamic model
- **Web Interface** - Real-time dashboard, map tracking, image gallery and more
- **Ground station apps** supporting live tracking, payload heading and distance, landing zone prediction, automatic telemetry/image recording, bust height calculation

## Hardware Requirements

### Airborne Payload
- Raspberry Pi Zero 2W
- Waveshare SX1262 868M/915M LoRa HAT with GNSS L76 GPS builtin 
- Sony IMX219 Camera (Pi Camera Module v2)

### Ground Station
- Raspberry Pi Zero 2W (or any Pi) with Waveshare SX1262 868M/915M LoRa HAT with GPS module, web based access
- Mac ground station app using custom radio modem based on Heltec T190 radio board
- Cross platform python GUI and headless webserver ground stations using custom modem

## Installation

Pre-built Raspberry Pi images, contain both airborne and groundstation 
  - this is the easiest way to get started, flash to your Pi SD card with dd or Pi Imager, dont forget to "sudo raspi-config --expand-rootfs" after the first time you flash the new image to youe sd card. Connect to the Pi after flash through SSH on the microUSB data port at 10.0.0.1
    
https://mega.nz/file/3U03zIgY#bumpJyihANdUjG_XCdLndyDg479Gf7k2OrLLD-Lk9i4

SSH logon: raptor password: raptor

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
Connect to the Airborne unit through SSH (user: raptor, password: raptor) after connecting to the micro USB data port. Set up the air unit to run as a service (instructions in documentation). 

The ground unit has a few options:
  1) run the ground unit Pi Zero 2W prebuild image - no camera, but uses the same Waveshare sx1262/GNSS hat board. Connect the Pi Zero 2W connect to its USB data port and SSH into the Pi (user: raptor, password: raptor) run the ground app as needed or setup as a service to run on start (instructions in documentation) - setup AP mode for wireless operations.
  2)   run ground station on a Mac computer through the Mac native app - this is the most fully featured and best way to run the ground station - you will need to flash a Heltec T190 LoRa module with the custom code to act as a modem, external GPS for the mac is optional, but desirable.
  3)   run the python groundstation code in GUI mode or webserver mode - this could be done on an Pi 3,4,5 or some other computer or even windows. This also requires use of the T190 modem
  4)   iOS app RapttorHABMobile available on the app store once Apple approves it

