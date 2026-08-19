# Installing RaptorHab on a Raspberry Pi Zero 2 W

From a blank SD card to a payload that transmits on boot.

---

## Two ways in

| | Over the network | From the SD card |
|---|---|---|
| Pi joins your WiFi | yes | not required |
| Needs the Pi's IP address | yes | no |
| Payload source | copied over SSH | staged on the card before first boot |
| Boot configuration | written by the installer, needs a reboot | already correct on first boot |
| Reachable if WiFi fails | no | yes, over the USB cable |

The network path is sections 1–5 below. The card path is
[Provisioning the card before first boot](#provisioning-the-card-before-first-boot),
and it is the better one if you would rather not put a flight computer on your
home WiFi, or if your access point isolates clients from each other — which is
common, and which makes a Pi that is online and still unreachable from the
laptop next to it.

Either way you finish in the same place, running `install.sh` on the Pi.

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

## Provisioning the card before first boot

`install.sh` needs a booted Pi with a working network, because it installs
packages. That is a poor fit for a flight computer: it means putting the
payload on your home WiFi, and it means the first thing you do with a new card
is go looking for its IP address. On a network with client isolation between
bands -- common, and something this project has already lost an afternoon to --
the Pi can be online and still unreachable from your laptop.

### Let Imager own your account and WiFi

Set the username, password, WiFi and hostname **in Raspberry Pi Imager**, not
here. Those are yours and they differ per person and per network; this script
has no business guessing them.

On Pi OS Bookworm and later, Imager writes that customisation as cloud-init
(`user-data` and `network-config` on the boot partition). `provision_sd.sh`
detects those files and deliberately stands aside — it will refuse `--user` and
`--wifi` and tell you so, because `firstrun.sh` runs *before* cloud-init and
would otherwise create the account with one password only for cloud-init to
replace it with another. You would type the password you set in Imager, it
would work, and the one you passed here would have quietly evaporated.

So the division is:

| Set in Imager | Set by this script |
|---|---|
| username and password | `config.txt`: SPI, GPIO ALT0, `disable-bt`, camera overlay |
| WiFi network | USB ethernet gadget, so the Pi is reachable without WiFi |
| hostname, locale, timezone | the payload source, staged for the installer |

### Running it

`Pi/tools/provision_sd.sh` prepares the card before it has ever booted. Put the
freshly flashed card in your Mac or Linux machine and run:

```bash
./Pi/tools/provision_sd.sh --camera imx219
```

If the card has no cloud-init on it — an older Pi OS, or a card flashed without
Imager's customisation — then `--user`, `--password` and `--wifi` do apply, and
you will need at least an account to log in with.

It writes the boot partition only — on a Mac the ext4 root mounts read-only, so
anything needing the root filesystem has to happen on the Pi. It lands the
payload's `config.txt` changes, stages the source as a tarball, enables SSH, and
turns on the **USB ethernet gadget**.

It is safe to run twice. Every change is idempotent: it reports lines that are
already present rather than adding them again, and it warns if the card has
been provisioned before.

### First boot

Eject the card, put it in the Pi, and connect a cable to the Pi's **data**
port — the inner socket on a Zero 2 W, not the one marked PWR.

First boot does more than usual: the filesystem is resized, `firstrun.sh` runs
and reboots, then cloud-init applies your account and WiFi. Allow three or four
minutes before expecting it to answer.

Then either:

```bash
ssh <your-imager-username>@raptorhab.local
```

or, if WiFi did not come up, over the cable — **the same command**:

```bash
ssh <your-imager-username>@raptorhab.local
```

The payload advertises itself over the USB link, so this works with nothing
configured on your machine and no administrator password. Verified: with WiFi
up *and* the cable in, this connects over USB.

Two things that will mislead you:

- **`ping raptorhab.local` fails while `ssh raptorhab.local` works.** `ping`
  asks for an IPv4 address and the USB link only has a usable IPv6 one. The
  payload is not down; use `ping6 raptorhab.local` if you want to check.
- **`ssh 10.55.0.1` works only if the card was prepared with
  `--usb-ethernet`,** which installs a small DHCP server so your machine is
  given an address on the cable (`10.55.0.10`–`.20`). Without it your machine
  self-assigns `169.254.x.x`, the two ends sit on different subnets, and only
  the `raptorhab.local` route works. Either way you never need to configure an
  address by hand, which earlier versions of this page told you to do.

  That DHCP server hands out **no gateway, no DNS and no NTP server** — it
  gives an address and nothing else. Plugging a payload into a laptop must not
  change how that laptop reaches the internet, or what it thinks the time is.

If mDNS is unavailable — Windows without Bonjour, or two payloads on one desk —
find it directly:

```bash
./Pi/tools/find_payload.sh <your-username>
```

which prints the exact `ssh` command, for example:

```
Payload found on en15 at fe80::1a:11ff:fe00:2%en15

  ssh stephen@fe80::1a:11ff:fe00:2%en15
```

It asks every device on the cable to identify itself, which needs no addresses
and no privileges.

### Checking that provisioning worked

```bash
ls /opt/raptorhab-src/setup/install.sh
cat /var/log/raptorhab-firstrun.log
```

The log is the first place to look if anything is missing. `firstrun.sh` writes
everything it does there, then deletes itself and removes its own hook from
`cmdline.txt` so it runs exactly once.

Two quick sanity checks that the boot configuration took:

```bash
ls /dev/spidev0.0          # SPI for the radio
ls -l /dev/serial0         # should point at ttyAMA0, not ttyS0
```

`serial0` pointing at `ttyS0` means `disable-bt` did not apply — the GPS would
be on the mini-UART, whose baud rate follows the CPU clock, which is how you
get NMEA that decodes at idle and turns to noise under load.

### The one thing it cannot do offline

Installing packages. A fresh Pi OS Lite has no `picamera2` and no
`python3-venv`, and no amount of card preparation conjures them up. The USB
ethernet link solves this without WiFi: share your laptop's connection over
that interface (macOS: System Settings > General > Sharing > Internet Sharing,
from Wi-Fi to the gadget interface), then run the installer over SSH:

```bash
sudo /opt/raptorhab-src/setup/install.sh --usb-gadget --camera imx219
```

The payload never joins your WiFi.

A genuinely air-gapped install would mean staging every `.deb` on the card.
That is possible, but it breaks whenever Raspberry Pi OS moves a dependency,
and a provisioning path that works until it silently doesn't is worse than one
that is honest about needing a wire.

### Options

| Option | Effect |
|---|---|
| `--boot PATH` | Boot partition (auto-detects `bootfs`) |
| `--source PATH` | Payload tree to stage (defaults to the `Pi/` directory holding the script) |
| `--hostname NAME` | Hostname, default `raptorhab` |
| `--user NAME` / `--password PASS` | Create an account, **only if the card has no cloud-init**. The password is hashed before it is written; the plain text never lands on the card. |
| `--wifi SSID` / `--wifi-password` / `--wifi-country` | Optional WiFi, **only if the card has no cloud-init**. |
| `--camera SENSOR` | Camera overlay: `imx219`, `imx477`, `imx708`, `ov5647` |
| `--no-usb-ethernet` | Leave the USB gadget off |
| `--auto-install` | Run the installer on first boot. Needs a network at that moment. |
| `--dry-run` | Show every change, write nothing |

`--dry-run` first is worth the ten seconds. It prints exactly which lines it
would add and which are already present.

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

## What the USB cable carries

Three things, over one cable:

| | What it is | How you use it |
|---|---|---|
| **CDC-ECM** ethernet | A network link to the payload | `ssh raptorhab.local` — this is the one you want |
| **CDC-ACM** serial | The configuration and terminal channel the macOS app speaks | Automatic; the app finds it |
| Power | The Pi runs from it | Nothing to do |

The ethernet half is optional and only appears if the card was prepared with
`--usb-ethernet`. The serial half is always present.

**There is no login prompt on the serial port.** The installer deliberately
disables `serial-getty@ttyGS0` because the configuration service owns that
device and only one process may. Shell access is over SSH, on the ethernet
half.

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

## Protecting flight recordings

The SD card is not encrypted and on a balloon cannot usefully be — the payload
boots unattended, so any key it could use would travel with it.

Instead, seal recordings to a public key the payload cannot read back.
Generate a keypair on your Mac:

```bash
python3 Pi/tools/recording_key.py generate
```

Set `recording_encryption_enabled` and `recording_public_key` on the payload,
then after recovery:

```bash
python3 Pi/tools/recording_key.py decrypt /Volumes/SDCARD/var/lib/raptorhab --out ./recovered
```

Separately, audit the credentials already on the card:

```bash
sudo /opt/raptorhab/tools/preflight_secrets.py --sanitize --keep-wifi
```

Full reasoning in [SECURITY.md](SECURITY.md).

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

## Recording encryption keys

Images and telemetry are sealed to an X25519 public key as the payload writes
them. The payload holds only the public half, so a stranger who recovers the
balloon gets ciphertext. The private half lives on your ground station and
never flies.

That protection has one failure mode, and it is total: **if you do not hold the
private key, the recordings are gone.** Not awkward to read — gone. No amount
of later effort recovers them.

This is not hypothetical. A card from this project carries 710 images sealed to
a public key whose private half was never saved. They are unreadable and always
will be.

### The short version

`provision_sd.sh` handles it. If there is no keypair it offers to make one,
saves the private half to `~/.raptorhab/recording_key` with mode `0600`, puts
only the public half on the card, and enables encryption on first boot:

```
ok   sealing to XhdssUXd+ZymEOS/Gb9zSOE7usnX3x8WTF7Jd3GP4XE=
ok   private key stays at ~/.raptorhab/recording_key and never goes near the card
warn back that file up now
```

Pass `--no-encryption` to record in the clear, or `--recording-key PATH` to use
a key kept somewhere else.

### Doing it by hand

Generate a keypair on the **ground station**, never on the payload:

```bash
python3 Pi/tools/recording_key.py generate
```

That writes the private key to `~/.raptorhab/recording_key` (mode `0600`) and
the public key alongside it as `recording_key.pub`, then prints the value to
configure.

Put **only the public half** on the payload — through the macOS app, the Python
ground station, or by editing the config directly:

```json
"recording_encryption_enabled": true,
"recording_public_key": "XhdssUXd+ZymEOS/Gb9zSOE7usnX3x8WTF7Jd3GP4XE="
```

Confirm the payload is sealing to a key you can open before you fly:

```bash
python3 Pi/tools/recording_key.py verify XhdssUXd+ZymEOS/Gb9zSOE7usnX3x8WTF7Jd3GP4XE=
```

### Reading recordings afterwards

Full instructions, including the file format and what to do when decryption
fails, are in [SECURITY.md](SECURITY.md#decrypting-by-hand).

From a recovered card, or from files pulled off over USB:

```bash
python3 Pi/tools/recording_key.py decrypt /Volumes/rootfs/var/lib/raptorhab/images --out ./recovered
```

The Python ground station does the same thing with a survey first, which
reports whether the card is readable **before** you spend time copying:

```python
from raptorhabgs.core.sd_import import survey_card, import_files, load_private_key
survey = survey_card("/Volumes/rootfs")
print(survey.as_dict())        # includes key_matches and readable
import_files(survey.images, "./recovered", load_private_key())
```

### Backing it up

The private key is 32 bytes. Copy `~/.raptorhab/recording_key` somewhere that
is not the machine that generated it — a password manager, an encrypted USB
stick, anywhere you would keep an SSH key.

Regenerating is not a recovery: a new keypair cannot open anything sealed to
the old one. `recording_key.py generate` refuses to overwrite an existing key
without `--force` for exactly that reason.

Losing the payload costs you a payload. Losing this file costs you every flight
it ever recorded.

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

**No camera.** First `rpicam-hello --list-cameras`. If that lists nothing but
a camera is physically fitted, auto-detection has failed to probe it — which
happens with a fair number of modules, third-party IMX219 boards especially,
and anything behind an adapter cable. The camera is fine; it just needs
naming:

```bash
sudo /opt/raptorhab/setup/install.sh --camera imx219 && sudo reboot
```

Use `imx219` for Camera Module v2, `ov5647` for v1, `imx708` for v3, `imx477`
for the HQ camera. Confirm afterwards with `sudo ./setup/install.sh --check`,
which reports the sensor by name.

If `rpicam-hello` sees the camera but the payload does not, the venv was built
without `--system-site-packages`; delete `/opt/raptorhab/.venv` and re-run the
installer.

**Nothing on `/dev/cu.usbmodem*`.** Confirm you are on the data port, not
`PWR IN`. On the Pi, `ls /sys/class/udc` should be non-empty — if it is empty,
`dtoverlay=dwc2` has not taken effect yet, so reboot. Then
`systemctl status raptorhab-usb-gadget`.


