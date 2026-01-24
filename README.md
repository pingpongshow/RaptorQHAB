# RaptorHab - High Altitude Balloon Image & Telemetry System

RaptorHab is a complete high-altitude balloon (HAB) telemetry and imagery downlink system consisting of airborne payload and ground station components.

## Features

- **915 MHz FSK Radio Link** - 200 kbps data rate, FCC Part 15.247 compliant
- **Fountain Codes** - Robust error correction using RaptorQ or LT codes
- **WebP Image Compression** - Efficient image transmission (~50KB per image)
- **GPS Telemetry** - Real-time position tracking with airborne dynamic model
- **Bidirectional Communication** - 10s TX / 10s RX duty cycle for commands
- **Web Interface** - Real-time dashboard, map tracking, image gallery

## Hardware Requirements

### Airborne Payload
- Raspberry Pi Zero 2W
- Waveshare SX1262 868M/915M LoRa HAT
- Sony IMX219 Camera (Pi Camera Module v2)
- USB uBlox GPS module (e.g., VK-172)

### Ground Station
- Raspberry Pi Zero 2W (or any Pi)
- Waveshare SX1262 868M/915M LoRa HAT
- Web browser for UI

## Pin Configuration (Waveshare SX1262 HAT)

| SX1262 HAT Pin | Raspberry Pi GPIO (BCM) |
|----------------|-------------------------|
| CS (NSS)       | GPIO 21                 |
| CLK (SCK)      | GPIO 11                 |
| MOSI           | GPIO 10                 |
| MISO           | GPIO 9                  |
| BUSY           | GPIO 20                 |
| DIO1           | GPIO 16                 |
| TXEN (DIO4)    | GPIO 6                  |
| RST (RESET)    | GPIO 18                 |

**Note:** The non-standard CS pin (GPIO 21) requires bit-banged SPI.

## Installation

Pre-build Raspberry Pi image, contains both airborne and groundstation - this is the easiest wat to get started!!!
download from https://www.dropbox.com/scl/fo/6gg3gslbegtxcpt9paqfd/ADLQRzHeOkjQu1rJ2CoCmxU?rlkey=czmw5ctd03qi6r9cold0atwao&st=9gicknop&dl=0

### 1. Install System Dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
    python3-dev python3-pip python3-venv \
    python3-numpy python3-serial python3-spidev python3-rpi.gpio \
    python3-picamera2 libwebp-dev imagemagick fonts-dejavu
```

### 2. Install Python Dependencies

```bash
cd /home/pi/raptorhab
pip3 install -r requirements.txt
```

### 3. Configure GPS udev Rule (Optional)

Create `/etc/udev/rules.d/99-ublox.rules`:
```
SUBSYSTEM=="tty", ATTRS{idVendor}=="1546", ATTRS{idProduct}=="01a7", SYMLINK+="ublox"
```

Then reload udev:
```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

### 4. Install Systemd Service

**Airborne Payload:**
```bash
sudo cp systemd/raptorhab-airborne.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable raptorhab-airborne
```

**Ground Station:**
```bash
sudo cp systemd/raptorhab-ground.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable raptorhab-ground
```

## Usage

### Airborne Payload

```bash
# Normal operation
python3 -m airborne.main

# Debug/simulation mode (no hardware required)
python3 -m airborne.main --debug

# With custom settings
python3 -m airborne.main --callsign MYBALLOON --frequency 915.5 --power 20
```

### Ground Station

```bash
# Normal operation
python3 -m ground.main

# Simulation mode (generates fake telemetry)
python3 -m ground.main --simulate

# Custom port and data path
python3 -m ground.main --web-port 8080 --data-path /mnt/data

# Disable web interface (command line only)
python3 -m ground.main --no-web
```

**Web Interface:** Open http://localhost:5000 in your browser.

### Systemd Service

```bash
# Airborne
sudo systemctl start raptorhab-airborne
sudo systemctl status raptorhab-airborne
journalctl -u raptorhab-airborne -f

# Ground Station
sudo systemctl start raptorhab-ground
sudo systemctl status raptorhab-ground
journalctl -u raptorhab-ground -f
```

### Environment Variables

**Airborne:**
- `RAPTORHAB_CALLSIGN` - Payload callsign
- `RAPTORHAB_FREQUENCY` - RF frequency in MHz
- `RAPTORHAB_TX_POWER` - TX power in dBm (0-22)
- `RAPTORHAB_GPS_DEVICE` - GPS serial device path
- `RAPTORHAB_DEBUG` - Enable debug mode (1/true/yes)
- `RAPTORHAB_SIMULATE_GPS` - Simulate GPS data
- `RAPTORHAB_SIMULATE_CAMERA` - Simulate camera capture

**Ground Station:**
- `RAPTORHAB_GND_CALLSIGN` - Station callsign
- `RAPTORHAB_GND_FREQUENCY` - RF frequency in MHz
- `RAPTORHAB_GND_DATA_PATH` - Data storage path
- `RAPTORHAB_GND_IMAGE_PATH` - Image storage path
- `RAPTORHAB_GND_LOG_PATH` - Log file path
- `RAPTORHAB_GND_WEB_PORT` - Web interface port
- `RAPTORHAB_GND_DEBUG` - Enable debug mode
- `RAPTORHAB_GND_SIMULATE` - Simulation mode

## Project Structure

```
raptorhab/
├── airborne/                    # Airborne payload
│   ├── __init__.py
│   ├── main.py                  # Entry point, state machine
│   ├── config.py                # Configuration
│   ├── camera.py                # IMX219 capture
│   ├── gps.py                   # uBlox GPS interface
│   ├── fountain.py              # RaptorQ/LT encoder
│   ├── packets.py               # Packet scheduling
│   ├── telemetry.py             # Sensor collection
│   ├── commands.py              # Command handler
│   └── utils.py                 # Utilities
├── ground/                      # Ground station
│   ├── __init__.py
│   ├── main.py                  # Entry point, coordinator
│   ├── config.py                # Configuration
│   ├── receiver.py              # Packet reception
│   ├── decoder.py               # Fountain code decoder
│   ├── telemetry.py             # Telemetry processing
│   ├── commands.py              # Command transmission
│   ├── storage.py               # Image/data storage
│   ├── web.py                   # Flask web interface
│   ├── templates/               # HTML templates
│   │   ├── base.html
│   │   ├── index.html           # Dashboard
│   │   ├── map.html             # Track map
│   │   ├── images.html          # Image gallery
│   │   └── commands.html        # Command interface
│   └── static/                  # Static web files
├── common/                      # Shared modules
│   ├── __init__.py
│   ├── protocol.py              # Packet structures
│   ├── constants.py             # Shared constants
│   ├── crc.py                   # CRC-32 implementation
│   └── radio.py                 # SX1262 FSK driver
├── systemd/
│   ├── raptorhab-airborne.service
│   └── raptorhab-ground.service
├── requirements.txt
└── README.md
```

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

## Regulatory Compliance

This system is designed for FCC Part 15.247 compliance:

- **Frequency Band:** 902-928 MHz ISM
- **Modulation:** 2-GFSK, BT=0.5
- **6dB Bandwidth:** >500 kHz (±125 kHz deviation at 200 kbps)
- **Max Power:** 30 dBm (1 Watt) conducted
- **Default Power:** 22 dBm (~160 mW)

Users are responsible for ensuring antenna gain + TX power does not exceed regulatory limits.

## License

MIT License - See LICENSE file for details.

## Contributing

Contributions welcome! Please submit issues and pull requests.
