#!/usr/bin/env bash
#
# RaptorHab airborne payload installer for Raspberry Pi OS.
#
# Idempotent: safe to re-run to upgrade an existing install. Everything it
# changes is reported, and nothing is changed without saying so.
#
#   sudo ./install.sh                  # install or upgrade
#   sudo ./install.sh --usb-gadget     # ...and enable the USB serial console
#   sudo ./install.sh --check          # verify an existing install, change nothing
#   sudo ./install.sh --camera imx219  # name the camera when auto-detect fails
#
# See docs/INSTALL.md for the whole story, including what needs a reboot.

set -euo pipefail

CODE_DIR="/opt/raptorhab"
STATE_DIR="/var/lib/raptorhab"
SERVICE_USER="raptorhab"
VENV="${CODE_DIR}/.venv"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENABLE_USB_GADGET=0
CHECK_ONLY=0
NEEDS_REBOOT=0
CAMERA_OVERLAY=""

# --------------------------------------------------------------------------

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'

say()  { printf '%s==>%s %s\n' "$BOLD" "$OFF" "$*"; }
ok()   { printf '  %s[ ok ]%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '  %s[warn]%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '  %s[fail]%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

reboot_needed() { NEEDS_REBOOT=1; warn "$1 (takes effect after reboot)"; }

usage() {
    sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --usb-gadget) ENABLE_USB_GADGET=1 ;;
        --check)      CHECK_ONLY=1 ;;
        --camera)     CAMERA_OVERLAY="${2:?--camera needs a sensor name}"; shift ;;
        --camera=*)   CAMERA_OVERLAY="${1#*=}" ;;
        -h|--help)    usage ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

[[ $EUID -eq 0 ]] || die "run with sudo"

# --------------------------------------------------------------------------
# Where is config.txt? Bookworm moved it.
# --------------------------------------------------------------------------

if   [[ -f /boot/firmware/config.txt ]]; then BOOT_CONFIG=/boot/firmware/config.txt
elif [[ -f /boot/config.txt ]];          then BOOT_CONFIG=/boot/config.txt
else die "cannot find config.txt; is this Raspberry Pi OS?"
fi
BOOT_DIR="$(dirname "$BOOT_CONFIG")"

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

say "Checking the platform"

ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" ]]; then
    die "this is $ARCH, but the bundled RaptorQ wheel is linux_aarch64.
       Flash the 64-bit Raspberry Pi OS. RaptorQ is not optional: the ground
       station cannot decode the LT fallback, so a 32-bit install would
       transmit images that nothing can reconstruct."
fi
ok "64-bit ARM"

MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
ok "$MODEL"

TOTAL_MB=$(($(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024))
[[ $TOTAL_MB -ge 400 ]] || warn "only ${TOTAL_MB} MB RAM detected"

if [[ $CHECK_ONLY -eq 0 ]]; then
    FREE_MB=$(df -Pm / | awk 'NR==2 {print $4}')
    [[ $FREE_MB -ge 500 ]] || die "only ${FREE_MB} MB free on /; need ~500 MB"
    ok "${FREE_MB} MB free"
fi

# --------------------------------------------------------------------------
# Check-only mode
# --------------------------------------------------------------------------

if [[ $CHECK_ONLY -eq 1 ]]; then
    say "Verifying the installation"

    [[ -d "$CODE_DIR" ]] && ok "code at $CODE_DIR" || die "not installed"
    [[ -x "$VENV/bin/python" ]] && ok "virtualenv" || die "missing virtualenv"

    "$VENV/bin/python" -c 'import raptorq' 2>/dev/null \
        && ok "raptorq importable" \
        || die "raptorq missing: the payload will refuse to start"

    for mod in spidev serial; do
        "$VENV/bin/python" -c "import $mod" 2>/dev/null \
            && ok "$mod" || warn "$mod not importable"
    done

    "$VENV/bin/python" -c 'from picamera2 import Picamera2' 2>/dev/null \
        && ok "picamera2" || warn "picamera2 not importable (camera disabled)"

    CAMERA_TOOL=$(command -v rpicam-hello || command -v libcamera-hello \
        || ls /usr/bin/rpicam-hello 2>/dev/null || true)
    if [[ -n "$CAMERA_TOOL" ]]; then
        camera_list=$("$CAMERA_TOOL" --list-cameras 2>&1 || true)
        if [[ "$camera_list" == *"Available cameras"* ]]; then
            sensor=$(printf '%s' "$camera_list" | grep -m1 -oE '(imx|ov)[0-9]+' || true)
            ok "camera: ${sensor:-detected}"
        else
            warn "no camera detected; re-run with --camera <sensor> if one is fitted"
        fi
    fi

    [[ -e /dev/spidev0.0 ]] && ok "/dev/spidev0.0" || warn "SPI device missing"
    [[ -e /dev/serial0 ]]   && ok "/dev/serial0"   || warn "GPS serial missing"

    systemctl is-enabled --quiet raptorhab-airborne \
        && ok "service enabled" || warn "service not enabled"
    systemctl is-active --quiet raptorhab-airborne \
        && ok "service running" || warn "service not running"

    say "Resolved configuration"
    (cd "$CODE_DIR" && sudo -u "$SERVICE_USER" "$VENV/bin/python" \
        -m airborne.main --print-config 2>/dev/null | head -25) \
        || warn "could not read config"
    exit 0
fi

# --------------------------------------------------------------------------
# Packages
# --------------------------------------------------------------------------

say "Installing system packages"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# picamera2 and its libcamera stack come from apt, never pip -- the pip build
# does not link against the system libcamera and will not see the sensor.
REQUIRED_PACKAGES="python3 python3-venv python3-pip python3-dev git build-essential rsync"

# Optional: nice to have, but their names drift between Debian releases and
# none of them is load-bearing. libatlas-base-dev, for instance, was dropped
# in Debian 13. Failing the whole install over a renamed BLAS package would be
# absurd, so these are installed individually and skipped when unavailable.
OPTIONAL_PACKAGES="python3-libcamera python3-picamera2 python3-numpy python3-pil libopenblas-dev"

apt-get install -y -qq $REQUIRED_PACKAGES >/dev/null \
    || die "could not install required packages: $REQUIRED_PACKAGES"
ok "required packages installed"

MISSING_OPTIONAL=""
for pkg in $OPTIONAL_PACKAGES; do
    if apt-get install -y -qq "$pkg" >/dev/null 2>&1; then
        continue
    fi
    MISSING_OPTIONAL="$MISSING_OPTIONAL $pkg"
done

if [[ -n "$MISSING_OPTIONAL" ]]; then
    warn "not available on this release:$MISSING_OPTIONAL"
    case "$MISSING_OPTIONAL" in
        *picamera2*|*libcamera*)
            warn "the camera will not work without picamera2; the payload will "
            warn "still fly and transmit telemetry, but capture no images"
            ;;
    esac
else
    ok "optional packages installed"
fi

# --------------------------------------------------------------------------
# Service user
# --------------------------------------------------------------------------

say "Setting up the service account"

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/"$SERVICE_USER" \
            --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "created user $SERVICE_USER"
else
    ok "user $SERVICE_USER exists"
fi

# The payload talks to the radio over SPI and GPIO, to the GPS over the UART,
# and to the camera over video. Not all groups exist on every image.
for grp in spi gpio dialout video i2c; do
    if getent group "$grp" >/dev/null; then
        usermod -aG "$grp" "$SERVICE_USER"
    fi
done
ok "group membership granted"

# --------------------------------------------------------------------------
# Code
# --------------------------------------------------------------------------

say "Installing code to $CODE_DIR"

mkdir -p "$CODE_DIR"
# --delete keeps an upgrade from leaving stale modules behind, but the venv
# and any local edits under .venv must survive.
rsync -a --delete \
    --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'tests' \
    "$REPO_ROOT"/ "$CODE_DIR"/
chown -R root:root "$CODE_DIR"
ok "code synced"

# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

say "Creating state directories under $STATE_DIR"

# 0750 on config: it holds Meshtastic channel pre-shared keys.
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$STATE_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$STATE_DIR/images"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$STATE_DIR/logs"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$STATE_DIR/config"
ok "state directories ready"

# --------------------------------------------------------------------------
# Python environment
# --------------------------------------------------------------------------

say "Building the Python environment"

# --system-site-packages is required: picamera2 and libcamera are apt-installed
# C extensions that cannot be pip-installed into an isolated venv.
if [[ ! -x "$VENV/bin/python" ]]; then
    python3 -m venv --system-site-packages "$VENV"
    ok "virtualenv created"
else
    ok "virtualenv exists"
fi

"$VENV/bin/pip" install --quiet --upgrade pip wheel

# The bundled wheel avoids a Rust toolchain build on a Pi Zero, which takes
# the better part of an hour if it succeeds at all.
WHEEL=$(find "$CODE_DIR/raptor_wheel" -name 'raptorq-*.whl' 2>/dev/null | head -1)
if [[ -n "$WHEEL" ]]; then
    "$VENV/bin/pip" install --quiet "$WHEEL" || die "bundled raptorq wheel rejected"
    ok "raptorq from the bundled wheel"
else
    warn "no bundled wheel; building raptorq from source (this is slow)"
    "$VENV/bin/pip" install --quiet raptorq || die "raptorq install failed"
fi

"$VENV/bin/pip" install --quiet RPi.GPIO spidev pyserial
ok "python dependencies installed"

# raptorq is load-bearing: the payload refuses to start without it, because
# the ground station has no decoder for the LT fallback.
"$VENV/bin/python" -c 'import raptorq' \
    || die "raptorq installed but not importable"
ok "raptorq verified"

# --------------------------------------------------------------------------
# Boot configuration
# --------------------------------------------------------------------------

say "Configuring $BOOT_CONFIG"

backup_boot_config() {
    [[ -f "${BOOT_CONFIG}.raptorhab-backup" ]] && return
    cp "$BOOT_CONFIG" "${BOOT_CONFIG}.raptorhab-backup"
    ok "backed up to ${BOOT_CONFIG}.raptorhab-backup"
}

ensure_boot_line() {
    local line="$1" description="$2"
    if grep -qxF "$line" "$BOOT_CONFIG"; then
        ok "$description"
        return
    fi
    backup_boot_config
    if ! grep -q '^# --- RaptorHab ---' "$BOOT_CONFIG"; then
        printf '\n# --- RaptorHab ---\n' >> "$BOOT_CONFIG"
    fi
    printf '%s\n' "$line" >> "$BOOT_CONFIG"
    reboot_needed "$description"
}

# SPI for the SX1262. The three gpio= lines put the SPI pins in ALT0; without
# them spidev opens successfully but the radio never responds.
ensure_boot_line "dtparam=spi=on" "SPI enabled"
ensure_boot_line "gpio=9=a0"      "GPIO 9 (MISO) in ALT0"
ensure_boot_line "gpio=10=a0"     "GPIO 10 (MOSI) in ALT0"
ensure_boot_line "gpio=11=a0"     "GPIO 11 (SCLK) in ALT0"

# The L76K GPS is on the PL011 UART. On a Pi with Bluetooth, PL011 is wired to
# the BT modem by default and /dev/serial0 points at the far less reliable
# mini-UART; disable-bt swaps them back.
ensure_boot_line "enable_uart=1"       "UART enabled"
ensure_boot_line "dtoverlay=disable-bt" "Bluetooth detached from the PL011 UART"

ensure_boot_line "camera_auto_detect=1" "camera auto-detect"

# Auto-detection probes the sensor over the firmware's I2C bus at boot, and a
# fair number of modules -- third-party IMX219 boards especially, and anything
# behind an adapter cable -- simply do not answer that probe even though they
# work perfectly once a driver is bound. The symptom is a camera that is
# physically fine and completely invisible: `detected=0`, no CSI activity, and
# picamera2 reporting an empty list.
#
# So if auto-detect found nothing, name the sensor explicitly. --camera picks
# the module; the default matches the Camera Module v2.
if [[ -n "$CAMERA_OVERLAY" ]]; then
    ensure_boot_line "dtoverlay=$CAMERA_OVERLAY" "explicit $CAMERA_OVERLAY camera overlay"
else
    CAMERA_TOOL=$(command -v rpicam-hello || command -v libcamera-hello \
        || ls /usr/bin/rpicam-hello 2>/dev/null || true)
    camera_list=""
    [[ -n "$CAMERA_TOOL" ]] && camera_list=$("$CAMERA_TOOL" --list-cameras 2>&1 || true)
fi

if [[ -z "$CAMERA_OVERLAY" && -n "${CAMERA_TOOL:-}" \
        && "${camera_list:-}" != *"Available cameras"* ]]; then
    warn "no camera auto-detected"
    warn "if one is fitted, re-run with --camera imx219 (v2), ov5647 (v1),"
    warn "imx708 (v3) or imx477 (HQ) to name it explicitly"
fi

if [[ $ENABLE_USB_GADGET -eq 1 ]]; then
    ensure_boot_line "dtoverlay=dwc2" "USB OTG peripheral mode"
fi

# The serial console owns /dev/serial0 and will fight the GPS for it.
if [[ -f "$BOOT_DIR/cmdline.txt" ]] && grep -q 'console=serial0' "$BOOT_DIR/cmdline.txt"; then
    cp "$BOOT_DIR/cmdline.txt" "$BOOT_DIR/cmdline.txt.raptorhab-backup"
    sed -i 's/console=serial0,[0-9]* //' "$BOOT_DIR/cmdline.txt"
    reboot_needed "serial console removed from cmdline.txt (it conflicts with the GPS)"
else
    ok "serial console not claiming the GPS port"
fi

if systemctl is-enabled --quiet serial-getty@ttyAMA0.service 2>/dev/null; then
    systemctl disable --now serial-getty@ttyAMA0.service >/dev/null 2>&1 || true
    ok "serial-getty on ttyAMA0 disabled"
fi

# --------------------------------------------------------------------------
# USB gadget (optional)
# --------------------------------------------------------------------------

if [[ $ENABLE_USB_GADGET -eq 1 ]]; then
    say "Enabling the USB serial console"

    if ! grep -q '^dwc2' /etc/modules 2>/dev/null; then
        backup_boot_config
        printf 'dwc2\n' >> /etc/modules
        reboot_needed "dwc2 module added to /etc/modules"
    fi

    install -m 0755 "$CODE_DIR/setup/usb-gadget.sh" /usr/local/sbin/raptorhab-usb-gadget
    install -m 0644 "$CODE_DIR/setup/raptorhab-usb-gadget.service" \
        /etc/systemd/system/raptorhab-usb-gadget.service
    systemctl daemon-reload
    systemctl enable raptorhab-usb-gadget.service >/dev/null
    ok "gadget service installed and enabled"

    # The configuration and terminal service the companion app talks to.
    install -m 0644 "$CODE_DIR/systemd/raptorhab-usbconsole.service" \
        /etc/systemd/system/raptorhab-usbconsole.service
    systemctl daemon-reload
    systemctl enable raptorhab-usbconsole.service >/dev/null
    ok "configuration and terminal service enabled"

    # A plain login shell on the same TTY would fight the console service for
    # it, so only one may own /dev/ttyGS0.
    if systemctl is-enabled --quiet serial-getty@ttyGS0.service 2>/dev/null; then
        systemctl disable --now serial-getty@ttyGS0.service >/dev/null 2>&1 || true
        warn "disabled the plain getty on ttyGS0; the console service owns it now"
    fi
fi

# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------

say "Installing the payload service"

install -m 0644 "$CODE_DIR/systemd/raptorhab-airborne.service" \
    /etc/systemd/system/raptorhab-airborne.service
systemctl daemon-reload
systemctl enable raptorhab-airborne.service >/dev/null
ok "raptorhab-airborne enabled"

# Seed a config file so the first run does not start from bare defaults, and
# so the operator has something to edit.
if [[ ! -f "$STATE_DIR/config/airborne.json" ]]; then
    # Must run from $CODE_DIR: the installer's own working directory is the
    # invoking user's home, which a system account cannot read, so `-m
    # airborne.main` would fail to import from there.
    if config_error=$(cd "$CODE_DIR" && sudo -u "$SERVICE_USER" \
            "$VENV/bin/python" -m airborne.main --save-config 2>&1 >/dev/null); then
        ok "initial config written"
    else
        warn "could not write the initial config: ${config_error##*$'\n'}"
    fi
else
    ok "existing config preserved"
fi

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------

echo
say "Installation complete"
echo
echo "  Code       $CODE_DIR"
echo "  State      $STATE_DIR    (images, logs, config)"
echo "  Config     $STATE_DIR/config/airborne.json"
echo "  Service    raptorhab-airborne"
echo
echo "  Settings   sudo -u $SERVICE_USER $VENV/bin/python -m airborne.main --print-config"
echo "  Schema     $VENV/bin/python -m airborne.main --print-schema"
echo "  Logs       journalctl -u raptorhab-airborne -f"
echo "  Verify     sudo $0 --check"
echo

if [[ $NEEDS_REBOOT -eq 1 ]]; then
    printf '%s  Reboot required before the payload will run.%s\n' "$YELLOW$BOLD" "$OFF"
    echo "  Boot settings changed (SPI, UART, or USB). Then:"
    echo
    echo "      sudo reboot"
    echo "      sudo $0 --check"
else
    echo "  Start it with:"
    echo
    echo "      sudo systemctl start raptorhab-airborne"
fi
echo
printf '%s  The payload transmits on boot. Fit an antenna before starting it.%s\n' \
    "$BOLD" "$OFF"
echo
