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
#   sudo ./install.sh --usb-ethernet   # ...and a network link over the same cable
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
KEEP_FIREWALL=0
NEEDS_REBOOT=0
CAMERA_OVERLAY=""
ENABLE_USB_ETHERNET=0

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
  --usb-ethernet) ENABLE_USB_ETHERNET=1; ENABLE_USB_GADGET=1 ;;
        --keep-firewall) KEEP_FIREWALL=1 ;;
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

    # Every one of these has failed silently on real hardware. A payload that
    # is up and transmitting while unreachable looks identical to a dead one
    # from the ground, so check them rather than assume.

    if command -v iw >/dev/null 2>&1 && [[ -d /sys/class/net/wlan0 ]]; then
        # iw prints "Power save: off" with a capital P; matching case
        # sensitively here reported the opposite of the truth.
        if iw dev wlan0 get power_save 2>/dev/null | grep -qi "power save: off"; then
            ok "WiFi power saving off"
        else
            warn "WiFi power saving is ON: the Pi will hold its association and"
            warn "its DHCP lease while dropping inbound packets, which looks"
            warn "exactly like a hung payload"
        fi
    fi

    if [[ -d /sys/kernel/config/usb_gadget/raptorhab ]]; then
        if [[ -s /sys/kernel/config/usb_gadget/raptorhab/UDC ]]; then
            ok "USB gadget bound to $(cat /sys/kernel/config/usb_gadget/raptorhab/UDC)"
        else
            warn "USB gadget is configured but not bound to a controller;"
            warn "the console will not appear on the host. Check whether"
            warn "g_ether or another gadget module holds the UDC."
        fi
    elif [[ $ENABLE_USB_GADGET -eq 1 ]]; then
        warn "USB gadget was requested but no gadget is configured"
    fi

    if grep -q "g_ether" /boot/firmware/cmdline.txt 2>/dev/null; then
        warn "cmdline.txt still loads g_ether; it will take the USB controller"
        warn "at boot and the payload console will never bind"
    fi

    if [[ -f /etc/NetworkManager/conf.d/10-raptorhab-usb0-unmanaged.conf ]]; then
        ok "usb0 left to the gadget script"
    elif [[ -d /sys/class/net/usb0 ]]; then
        warn "NetworkManager may be managing usb0; it will DHCP against a cable"
        warn "with no server and leave the interface without an address"
    fi

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

    # A drop-in rather than editing the unit, so re-running the installer does
    # not silently turn the network link on or off behind the operator.
    if [[ $ENABLE_USB_ETHERNET -eq 1 ]]; then
        install -d /etc/systemd/system/raptorhab-usb-gadget.service.d
        printf '[Service]\nEnvironment=RAPTORHAB_USB_ETHERNET=1\n' \
            > /etc/systemd/system/raptorhab-usb-gadget.service.d/ethernet.conf
        ok "USB ethernet enabled (payload will be 10.55.0.1)"
    else
        rm -f /etc/systemd/system/raptorhab-usb-gadget.service.d/ethernet.conf
    fi
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
# Network behaviour that costs you the payload
# --------------------------------------------------------------------------
#
# Two defaults on a Pi Zero 2 W will make a working payload look like a dead
# one. Both were diagnosed from a recovered card after the Pi ran perfectly for
# an hour while being completely unreachable.
#
# WiFi power save: the adapter stays associated and keeps its DHCP lease, so
# the Pi believes it is online and the access point lists it as connected --
# but inbound frames are dropped while it sleeps. The symptom is a Pi that
# answers for a minute after boot and then silently stops, with nothing in any
# log to explain it, because from the Pi's point of view nothing happened.
#
# NetworkManager and usb0: the gadget interface is given a static address by
# the gadget script. If NetworkManager also claims it -- which it does, because
# Raspberry Pi Imager writes a netplan ethernet profile with an empty match,
# and an empty match matches everything -- it runs DHCP against a link that
# has no server, and the interface ends up with no usable address at all.

say "Adjusting network defaults that break payload reachability"

install -d -m 0755 /etc/NetworkManager/conf.d

cat > /etc/NetworkManager/conf.d/10-raptorhab-wifi-powersave.conf <<'NMCONF'
# 2 = disable power saving. A sleeping adapter loses inbound packets while
# still holding its association and lease, which is indistinguishable from a
# hung payload right up until you recover the card.
[connection]
wifi.powersave = 2
NMCONF
ok "WiFi power saving disabled"

cat > /etc/NetworkManager/conf.d/10-raptorhab-usb0-unmanaged.conf <<'NMCONF'
# The USB gadget interface is configured by raptorhab-usb-gadget, which gives
# it a static address. NetworkManager must not also try to DHCP it: there is no
# server on the other end of a USB cable, and the attempt leaves the interface
# with no address rather than the one we assigned.
[keyfile]
unmanaged-devices=interface-name:usb0;interface-name:usb1
NMCONF
ok "usb0 left to the gadget script, not NetworkManager"

if systemctl is-active NetworkManager >/dev/null 2>&1; then
    systemctl reload NetworkManager >/dev/null 2>&1 || true
fi

# --------------------------------------------------------------------------
# Local firewall
# --------------------------------------------------------------------------
#
# Raspberry Pi OS ships without a firewall, so on a clean install this finds
# nothing and says so. It exists because a payload that has been used for
# something else first may carry one, and a host firewall on a flight computer
# is a poor trade: the machine has no route to the internet in flight, its only
# services are a USB console and SSH on a bench network, and a rule that blocks
# the ground station during recovery costs far more than it protects.
#
# Pass --keep-firewall to leave whatever is configured alone.

say "Checking for a local firewall"

FIREWALL_FOUND=0

if [[ $KEEP_FIREWALL -eq 1 ]]; then
    ok "leaving firewall configuration alone (--keep-firewall)"
else
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
        FIREWALL_FOUND=1
        ufw --force disable >/dev/null 2>&1 || true
        systemctl disable --now ufw >/dev/null 2>&1 || true
        systemctl mask ufw >/dev/null 2>&1 || true
        warn "ufw was active; disabled and masked so it cannot come back"
    fi

    if systemctl is-enabled nftables >/dev/null 2>&1 || \
       systemctl is-active nftables >/dev/null 2>&1; then
        # Only act if it actually carries rules. An enabled-but-empty nftables
        # blocks nothing, and disabling it would be noise.
        if nft list ruleset 2>/dev/null | grep -qE "drop|reject"; then
            FIREWALL_FOUND=1
            systemctl disable --now nftables >/dev/null 2>&1 || true
            warn "nftables carried blocking rules; disabled"
        fi
    fi

    if command -v iptables >/dev/null 2>&1; then
        if iptables -S 2>/dev/null | grep -qE "^-P (INPUT|FORWARD) DROP|^-A INPUT .*(DROP|REJECT)"; then
            FIREWALL_FOUND=1
            iptables -P INPUT ACCEPT 2>/dev/null || true
            iptables -P FORWARD ACCEPT 2>/dev/null || true
            iptables -F 2>/dev/null || true
            warn "iptables had blocking rules; flushed and set to ACCEPT"
        fi
    fi

    if [[ $FIREWALL_FOUND -eq 0 ]]; then
        ok "no firewall is blocking anything"
    fi
fi

# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------

say "Installing the payload service"

# --- in-flight WiFi cutoff -------------------------------------------------
#
# The payload runs unprivileged and cannot control the radio itself. Measured
# on a stock image: rfkill gives "cannot open /dev/rfkill: Permission denied"
# and nmcli gives "Not authorized to perform this operation". Without the
# helper and this sudoers rule the cutoff silently does nothing, which is the
# worst outcome -- the operator plans for the saving and never gets it.
step "Installing the WiFi power helper"
install -m 0755 "$CODE_DIR/setup/wifi-power.sh" /usr/local/sbin/raptorhab-wifi-power

# Diagnostics the operator runs from their own machine, not the Pi -- but they
# live in the installed tree so a recovered card carries them too.
install -d -m 0755 /opt/raptorhab/tools
for helper in find_payload.sh wifi_watch.sh gps_doctor.py recording_key.py; do
    [ -f "$CODE_DIR/tools/$helper" ] && \
        install -m 0755 "$CODE_DIR/tools/$helper" "/opt/raptorhab/tools/$helper"
done

# No sudoers rule, deliberately. The payload unit sets NoNewPrivileges=true, so
# sudo refuses outright ("the no new privileges flag is set") and a sudoers
# grant would be dead weight. Instead the payload writes a request file in its
# own state directory and this path unit -- already root -- acts on it. The
# payload gains no ability to run anything as root, only to ask for one
# specific action, which is strictly less privilege than sudo would have been.
install -m 0644 "$CODE_DIR/setup/raptorhab-wifi-off.path" \
    /etc/systemd/system/raptorhab-wifi-off.path
install -m 0644 "$CODE_DIR/setup/raptorhab-wifi-off.service" \
    /etc/systemd/system/raptorhab-wifi-off.service

# A request left over from a previous flight would fire the moment the watcher
# starts, taking WiFi down on the bench.
rm -f "$STATE_DIR/wifi-off.request"

systemctl daemon-reload
systemctl enable --now raptorhab-wifi-off.path >/dev/null 2>&1 || true
ok "in-flight WiFi cutoff watcher enabled"

# Restore WiFi at every boot, independently of the payload. systemd-rfkill
# saves the soft-block state and restores it on the next boot, so a payload
# blocked in flight comes back blocked. The payload unblocks on startup too,
# but only if it starts -- and "the payload is broken" is exactly when being
# able to reach the Pi matters most.
# Passive record of the WiFi radio's state. The payload has an intermittent
# fault where it stays associated and can reach the network while nothing can
# reach it, and four plausible explanations have already been tested and
# disproved -- see tools/wifi_watch.sh. Until it is understood, log the
# evidence rather than guess again.
install -m 0644 "$CODE_DIR/setup/raptorhab-wifi-watch.service" \
    /etc/systemd/system/raptorhab-wifi-watch.service
systemctl daemon-reload
systemctl enable raptorhab-wifi-watch.service >/dev/null 2>&1 || true
ok "wlan0 state watcher enabled"

install -m 0644 "$CODE_DIR/setup/raptorhab-wifi-restore.service" \
    /etc/systemd/system/raptorhab-wifi-restore.service
systemctl daemon-reload
systemctl enable raptorhab-wifi-restore.service >/dev/null 2>&1 || true
ok "WiFi restore-at-boot enabled"

/usr/local/sbin/raptorhab-wifi-power on >/dev/null 2>&1 || true

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
