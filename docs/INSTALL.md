# Installing RaptorHab on a Raspberry Pi Zero 2 W

From a blank SD card to a payload that transmits on boot.

---

## 1. Flash the OS

Use **Raspberry Pi OS Lite (64-bit)**, Bookworm or later.

The 64-bit part is not optional. The bundled RaptorQ wheel is
`linux_aarch64`, and RaptorQ is load-bearing: the ground station has no
decoder for the LT fallback, so a 32-bit install would transmit images that
nothing can reconstruct. The installer refuses to run on 32-bit for exactly
this reason.

Lite rather than Desktop: the payload has no display, and the desktop stack
costs RAM the camera pipeline wants on a 512 MB board.

In Raspberry Pi Imager, open the gear icon before writing and set:

- **hostname** — `raptorhab` makes it easy to find
- **username and password** — you will need these to log in
- **Wi-Fi** — your bench network, for the install
- **enable SSH**

The Wi-Fi is only for installation. Nothing in flight needs a network.

---

## 2. Get the code onto the Pi

Boot the Pi, find it on the network, and copy the `Pi/` directory across.

From your Mac, in the RaptorHAB project directory:

```bash
rsync -av --exclude '__pycache__' --exclude '.venv' Pi/ raptorhab.local:~/raptorhab/
```

If `raptorhab.local` does not resolve, use the address from your router, or
`ssh <user>@<ip>`.

Working from a git clone instead:

```bash
ssh raptorhab.local 'git clone https://github.com/YOURNAME/RaptorHAB.git && cd RaptorHAB'
```

---

## 3. Run the installer

```bash
ssh raptorhab.local
```

```bash
cd ~/raptorhab && sudo ./setup/install.sh
```

Add `--usb-gadget` to also enable the USB serial console described below:

```bash
sudo ./setup/install.sh --usb-gadget
```

It takes a few minutes on a Zero 2 W, is safe to re-run, and reports every
change it makes. What it does:

| | |
|---|---|
| **Packages** | `python3-picamera2` and the libcamera stack from apt — never pip, because the pip build does not link against the system libcamera and will not see the sensor |
| **Service user** | Creates `raptorhab`, in the `spi`, `gpio`, `dialout`, `video` and `i2c` groups. The payload does not run as root |
| **Code** | Syncs to `/opt/raptorhab` |
| **State** | Creates `/var/lib/raptorhab/{images,logs,config}`, owned by the service user. `config/` is `0750` because it holds Meshtastic channel keys |
| **Virtualenv** | `/opt/raptorhab/.venv`, built `--system-site-packages` so picamera2 is visible |
| **RaptorQ** | Installs the bundled wheel, then *verifies it imports*. Building from source on a Zero 2 W takes the better part of an hour |
| **Boot config** | SPI, the three ALT0 `gpio=` lines, UART, `disable-bt`, camera |
| **Serial console** | Removes it from `cmdline.txt` — it fights the GPS for `/dev/serial0` |
| **Service** | Installs and enables `raptorhab-airborne` |

Then reboot and verify:

```bash
sudo reboot
```

```bash
sudo /opt/raptorhab/setup/install.sh --check
```

`--check` changes nothing. It confirms the venv, that raptorq imports, that
`/dev/spidev0.0` and `/dev/serial0` exist, and prints the resolved config.

---

## 4. Configure

```bash
sudo -u raptorhab /opt/raptorhab/.venv/bin/python -m airborne.main --print-config
```

Edit `/var/lib/raptorhab/config/airborne.json`, or set values from the command
line:

```bash
sudo -u raptorhab /opt/raptorhab/.venv/bin/python -m airborne.main --callsign KX0ABC --save-config
```

At minimum, set your **callsign**, confirm **`radio_frequency_mhz` matches the
ground modem**, and decide whether `meshtastic_enabled` should be on.

Every parameter, with its range and whether it needs a restart:

```bash
/opt/raptorhab/.venv/bin/python -m airborne.main --print-schema
```

---

## 5. Run it

**Fit an antenna first.** The payload transmits as soon as it starts, and
transmitting into an open connector can damage the PA.

```bash
sudo systemctl start raptorhab-airborne
```

```bash
journalctl -u raptorhab-airborne -f
```

It is enabled at boot, so from here on the payload comes up on power-on.

---

## The USB serial console

### Does USB OTG break anything?

**No — with one real trade-off and two things to know.**

`dtoverlay=dwc2` switches the Pi Zero's data port from host mode to
peripheral mode. What that does and does not affect:

**Unaffected.** SPI, GPIO, the UART, I²C, and the camera are all separate
peripherals. The SX1262 HAT and the L76K GPS do not go near the USB
controller. Boot time, CPU, and memory are unchanged in any way you could
measure. When nothing is plugged into the port, the gadget simply sits idle.

**The trade-off.** In peripheral mode that port can no longer be a USB *host*,
so no keyboard, storage, or USB Wi-Fi dongle on it. For this payload that
costs nothing: the Zero 2 W has Wi-Fi and Bluetooth built in, and the port is
otherwise unused in flight.

**Two things to know.**

*Power.* On the Pi Zero the data port's VBUS is tied to the same 5 V rail as
the power port. Plugging into your Mac will therefore power the board. That is
convenient on the bench, but check your flight power wiring before connecting
both at once — you do not want the Mac back-feeding a battery. If in doubt,
disconnect the flight battery while working over USB, or use a data-only cable
with VBUS lifted.

*Ethernet gadgets.* This setup deliberately uses **CDC-ACM serial only**, not
`g_ether`. An ethernet gadget creates a network interface that
`systemd-networkd-wait-online` will sit and wait for, adding tens of seconds
to every boot when nothing is plugged in. On a payload that has to come up
reliably on battery, that is a bad trade for a convenience you would use on
the bench. Serial has no such failure mode.

### Using it

Enable during install with `--usb-gadget`, or afterwards:

```bash
sudo /opt/raptorhab/setup/install.sh --usb-gadget && sudo reboot
```

Connect the Mac to the Pi's **data** port — the one nearer the middle of the
board, marked `USB`, not `PWR IN`. Then:

```bash
ls /dev/cu.usbmodem*
```

```bash
screen /dev/cu.usbmodem14201 115200
```

You get a login prompt. Log in with the account you created in the Imager.

### The companion app

With `--usb-gadget`, the installer also enables `raptorhab-usbconsole`, which
serves the configuration API and terminal the macOS app talks to. Open the app
and use the **Config** and **Console** tabs; the app finds the payload by its
USB product string.

Note the two are mutually exclusive on the same port: the installer disables
any plain `getty` on `ttyGS0`, because the console service owns it. If you want
a bare `screen` session instead of the app, stop the service first:

```bash
sudo systemctl stop raptorhab-usbconsole
```

The service runs as root, unlike the flight software, because it offers a login
shell — and the whole point is that the shell belongs to whoever is holding the
cable. It refuses to bind to any device other than the USB gadget TTY, so that
privilege is never reachable over the radio.

---

## Reference

### Files

| Path | What |
|---|---|
| `/opt/raptorhab` | Code, read-only to the service |
| `/opt/raptorhab/.venv` | Python environment |
| `/var/lib/raptorhab/config/airborne.json` | Configuration, `0750` |
| `/var/lib/raptorhab/images` | Captured images |
| `/var/lib/raptorhab/logs` | Flight logs |
| `/etc/systemd/system/raptorhab-airborne.service` | The unit |

### Boot settings the installer adds

```
dtparam=spi=on
gpio=9=a0
gpio=10=a0
gpio=11=a0
enable_uart=1
dtoverlay=disable-bt
camera_auto_detect=1
dtoverlay=dwc2          # only with --usb-gadget
```

The three `gpio=` lines put the SPI pins in ALT0. Without them `spidev` opens
successfully and the radio never answers — a confusing failure worth knowing
about. `disable-bt` matters because on a Pi with Bluetooth the good PL011 UART
is wired to the BT modem by default and `/dev/serial0` points at the far less
reliable mini-UART; the overlay swaps them back.

The original `config.txt` is saved as `config.txt.raptorhab-backup` before the
first change.

### Everyday commands

```bash
sudo systemctl status raptorhab-airborne
```

```bash
journalctl -u raptorhab-airborne -f
```

```bash
sudo systemctl restart raptorhab-airborne
```

```bash
sudo /opt/raptorhab/setup/install.sh --check
```

### Bench-testing the radio

Stop the payload first — two processes cannot share the SPI bus:

```bash
sudo systemctl stop raptorhab-airborne
```

```bash
sudo /opt/raptorhab/.venv/bin/python /opt/raptorhab/tools/bench_lora.py rx --duration 120
```

```bash
sudo /opt/raptorhab/.venv/bin/python /opt/raptorhab/tools/bench_lora.py switch --count 50
```

### Upgrading

Re-sync the code and re-run the installer. It preserves your config and state:

```bash
rsync -av --exclude '__pycache__' --exclude '.venv' Pi/ raptorhab.local:~/raptorhab/
```

```bash
ssh raptorhab.local 'cd ~/raptorhab && sudo ./setup/install.sh && sudo systemctl restart raptorhab-airborne'
```

---

## Troubleshooting

**The service will not start.** `journalctl -u raptorhab-airborne -n 50`. The
most common cause on a fresh install is a missing reboot after the boot
settings changed.

**"RaptorQ is not available".** The payload refuses to start rather than
transmit images nothing can decode. Confirm with
`/opt/raptorhab/.venv/bin/python -c 'import raptorq'`. Almost always a 32-bit
OS — check `uname -m` reads `aarch64`.

**The radio never responds.** Check the three `gpio=...=a0` lines are present
and that you rebooted. Then `ls -l /dev/spidev*`.

**No GPS fix.** `cat /dev/serial0` should show NMEA sentences. If it is silent,
the serial console is probably still claiming the port: check `cmdline.txt`
has no `console=serial0`. A cold GPS needs several minutes and a clear view of
the sky.

**No camera.** `libcamera-hello --list-cameras`. If the venv cannot see it,
it was built without `--system-site-packages`; delete `/opt/raptorhab/.venv`
and re-run the installer.

**Nothing on `/dev/cu.usbmodem*`.** Confirm you are on the data port, not
`PWR IN`. On the Pi, `ls /sys/class/udc` should be non-empty — if it is empty,
`dtoverlay=dwc2` has not taken effect yet, so reboot. Then
`systemctl status raptorhab-usb-gadget`.
