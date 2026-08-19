#!/usr/bin/env bash
#
# provision_sd.sh — prepare a Raspberry Pi OS SD card for RaptorHAB before it
# has ever booted, from a Mac or a Linux box.
#
# Why this exists
# ---------------
# install.sh needs a booted Pi on a network. That is a poor fit for a flight
# computer: it means putting the payload on your home WiFi, and it means the
# first thing you do with a new card is go hunting for its IP address. On a
# network with client isolation between bands -- which is common, and which has
# already cost this project an afternoon -- the Pi may be online and still
# unreachable from your laptop.
#
# This writes the boot partition only, which is the part a Mac can write. It
# lands the payload's boot configuration, stages the source, and brings the Pi
# up as a USB ethernet device so it is reachable over the cable with no network
# at all.
#
# What it deliberately does not do
# --------------------------------
# It does not install packages. A fresh Pi OS Lite has no picamera2 and no
# python3-venv, and pretending otherwise would produce a card that looks
# provisioned and fails at first boot. With the USB ethernet gadget up you can
# share your laptop's connection over the cable and run install.sh without the
# Pi ever joining WiFi -- see the instructions this prints at the end.
#
set -euo pipefail

HOSTNAME_NEW="raptorhab"
USERNAME=""
PASSWORD=""
WIFI_SSID=""
WIFI_PASSWORD=""
WIFI_COUNTRY="US"
CAMERA_OVERLAY=""
BOOT_PATH=""
SOURCE_DIR=""
USB_ETHERNET=1
AUTO_INSTALL=1
RECORDING_KEY=""
ENCRYPT=1
GENERATE_KEY=0
DRY_RUN=0

usage() {
    cat <<'USAGE'
Usage: provision_sd.sh [options]

  --boot PATH          Boot partition (default: auto-detect bootfs)
  --source PATH        Payload source tree (default: the payload/ directory holding this script)
  --hostname NAME      Hostname to set (default: raptorhab)
  --user NAME          Create this user on first boot (needs --password)
  --password PASS      Password for --user
  --wifi SSID          Optional WiFi network
  --wifi-password PASS WiFi passphrase
  --wifi-country CC    Regulatory domain for WiFi (default: US)
  --camera SENSOR      Camera overlay, e.g. imx219, imx477, imx708, ov5647
  --no-usb-ethernet    Do not enable the USB ethernet gadget
  --no-auto-install    Do not install automatically; stage the source only
  --recording-key PATH Private key for recording encryption (default ~/.raptorhab/recording_key)
  --generate-key       Create the keypair if it does not exist, without asking
  --no-encryption      Do not encrypt recordings on this card
  --dry-run            Show what would be written, change nothing
  -h, --help           This text

Typical first-time card, no WiFi at all:

  ./provision_sd.sh --camera imx219 --user pilot --password 'something good'

Then boot the Pi with a USB cable to the data port, and it appears as a network
interface on your laptop.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --boot)           BOOT_PATH="${2:?--boot needs a path}"; shift ;;
        --source)         SOURCE_DIR="${2:?--source needs a path}"; shift ;;
        --hostname)       HOSTNAME_NEW="${2:?--hostname needs a name}"; shift ;;
        --user)           USERNAME="${2:?--user needs a name}"; shift ;;
        --password)       PASSWORD="${2:?--password needs a value}"; shift ;;
        --wifi)           WIFI_SSID="${2:?--wifi needs an SSID}"; shift ;;
        --wifi-password)  WIFI_PASSWORD="${2:?--wifi-password needs a value}"; shift ;;
        --wifi-country)   WIFI_COUNTRY="${2:?--wifi-country needs a code}"; shift ;;
        --camera)         CAMERA_OVERLAY="${2:?--camera needs a sensor}"; shift ;;
        --no-usb-ethernet) USB_ETHERNET=0 ;;
        --auto-install)   AUTO_INSTALL=1 ;;
        --no-auto-install) AUTO_INSTALL=0 ;;
        --recording-key)  RECORDING_KEY="${2:?--recording-key needs a path}"; shift ;;
        --generate-key)   GENERATE_KEY=1 ;;
        --no-encryption)  ENCRYPT=0 ;;
        --dry-run)        DRY_RUN=1 ;;
        -h|--help)        usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
    shift
done

# macOS writes AppleDouble sidecars (._name) when it copies to a filesystem
# that cannot hold extended attributes, which FAT32 cannot. They are harmless
# to Raspberry Pi OS but they clutter a card an operator may well inspect, and
# COPYFILE_DISABLE keeps tar from burying them inside the archive too.
export COPYFILE_DISABLE=1

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror\033[0m %s\n' "$*" >&2; exit 1; }

echo
echo "RaptorHAB SD card provisioning"
echo "=============================="
echo

# ---------------------------------------------------------------- locate ---

if [[ -z "$BOOT_PATH" ]]; then
    for candidate in /Volumes/bootfs /Volumes/boot \
                     /media/"$USER"/bootfs /media/"$USER"/boot \
                     /run/media/"$USER"/bootfs /run/media/"$USER"/boot; do
        [[ -d "$candidate" ]] && { BOOT_PATH="$candidate"; break; }
    done
fi
[[ -n "$BOOT_PATH" ]] || die "No boot partition found. Insert the card, or pass --boot."
[[ -d "$BOOT_PATH" ]] || die "$BOOT_PATH is not a directory."

# A Raspberry Pi boot partition always has these. Checking stops this script
# from writing its configuration into, say, a camera card.
[[ -f "$BOOT_PATH/config.txt" && -f "$BOOT_PATH/cmdline.txt" ]] \
    || die "$BOOT_PATH does not look like a Raspberry Pi boot partition (no config.txt/cmdline.txt)."

if [[ $DRY_RUN -eq 0 ]]; then
    touch "$BOOT_PATH/.rhwrite" 2>/dev/null \
        || die "$BOOT_PATH is not writable. On macOS the ext4 root is read-only, but the boot partition should not be."
    rm -f "$BOOT_PATH/.rhwrite"
fi
ok "boot partition: $BOOT_PATH"

# Raspberry Pi Imager on Bookworm and later writes a cloud-init user-data file,
# which already sets the hostname, creates the account, configures WiFi and
# enables SSH. Duplicating that here is not merely redundant: firstrun.sh runs
# before cloud-init, so it would create the account with one password and then
# have cloud-init quietly replace it with another. Whichever the operator typed
# into Imager is the one they will try, so cloud-init has to win.
#
# What cloud-init does not do is the payload's boot configuration or staging
# the source, and that is what this script is actually for.
CLOUD_INIT=0
if [[ -f "$BOOT_PATH/user-data" ]]; then
    CLOUD_INIT=1
    ok "cloud-init detected — deferring hostname, account and WiFi to it"
    if grep -q '^hostname:' "$BOOT_PATH/user-data" 2>/dev/null; then
        say "  hostname: $(grep '^hostname:' "$BOOT_PATH/user-data" | head -1 | cut -d: -f2- | tr -d ' ')"
    fi
    if grep -qE '^[[:space:]]*-?[[:space:]]*name:' "$BOOT_PATH/user-data" 2>/dev/null; then
        say "  account:  $(grep -E '^[[:space:]]*-?[[:space:]]*name:' "$BOOT_PATH/user-data" | head -1 | sed 's/.*name:[[:space:]]*//')"
    fi
    if [[ -f "$BOOT_PATH/network-config" ]] && grep -q 'access-points' "$BOOT_PATH/network-config" 2>/dev/null; then
        say "  wifi:     configured"
    fi
fi

# Refuse to quietly re-provision a card that has already been through this,
# unless the operator is watching a dry run.
if [[ -f "$BOOT_PATH/raptorhab-provisioned" && $DRY_RUN -eq 0 ]]; then
    warn "this card was already provisioned on $(cat "$BOOT_PATH/raptorhab-provisioned")"
    warn "continuing will overwrite the staged source and re-apply configuration"
fi

if [[ -z "$SOURCE_DIR" ]]; then
    SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
[[ -f "$SOURCE_DIR/setup/install.sh" ]] \
    || die "No payload source at $SOURCE_DIR (expected setup/install.sh). Pass --source."
ok "payload source: $SOURCE_DIR"

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  would run: %s\n' "$*"
    else
        "$@"
    fi
}

write_file() {
    local path="$1"; shift
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  would write %s (%d bytes)\n' "$path" "${#1}"
    else
        printf '%s' "$1" > "$path"
    fi
}

# ------------------------------------------------------------- config.txt ---

echo
echo "Boot configuration"

CONFIG="$BOOT_PATH/config.txt"

ensure_config_line() {
    local line="$1" note="$2"
    if grep -qxF "$line" "$CONFIG" 2>/dev/null; then
        say "already set: $line"
        return
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  would add: %-32s (%s)\n' "$line" "$note"
        return
    fi
    if ! grep -q '^# --- RaptorHab ---' "$CONFIG"; then
        printf '\n# --- RaptorHab ---\n' >> "$CONFIG"
    fi
    printf '%s\n' "$line" >> "$CONFIG"
    ok "$line  ($note)"
}

# The SX1262 hangs off SPI0. The three gpio= lines force the SPI pins into
# ALT0: without them the pins can come up as plain inputs and every SPI
# transfer silently reads back 0xFF.
ensure_config_line "dtparam=spi=on" "SPI for the radio"
ensure_config_line "gpio=9=a0"      "MISO in ALT0"
ensure_config_line "gpio=10=a0"     "MOSI in ALT0"
ensure_config_line "gpio=11=a0"     "SCLK in ALT0"

# The GPS is on the PL011. Bluetooth owns it by default, leaving the GPS on the
# mini-UART whose baud rate follows the CPU clock -- which is how you get NMEA
# that decodes at idle and turns to noise under load.
ensure_config_line "dtoverlay=disable-bt" "GPS gets the PL011, not the mini-UART"
ensure_config_line "enable_uart=1"        "UART enabled"

if [[ -n "$CAMERA_OVERLAY" ]]; then
    ensure_config_line "camera_auto_detect=0"          "explicit camera, no probing"
    ensure_config_line "dtoverlay=$CAMERA_OVERLAY"     "camera sensor"
fi

if [[ $USB_ETHERNET -eq 1 ]]; then
    ensure_config_line "dtoverlay=dwc2" "USB OTG peripheral mode"
fi

# ------------------------------------------------------------ cmdline.txt ---

echo
echo "Kernel command line"

CMDLINE="$BOOT_PATH/cmdline.txt"
CMDLINE_TEXT="$(tr -d '\n' < "$CMDLINE")"

if [[ $USB_ETHERNET -eq 1 ]]; then
    if [[ "$CMDLINE_TEXT" == *"modules-load=dwc2,g_ether"* ]]; then
        say "USB ethernet gadget already requested"
    else
        # modules-load has to follow rootwait or the gadget comes up before the
        # root filesystem and the kernel drops it.
        if [[ "$CMDLINE_TEXT" == *rootwait* ]]; then
            CMDLINE_TEXT="${CMDLINE_TEXT/rootwait/rootwait modules-load=dwc2,g_ether}"
        else
            CMDLINE_TEXT="$CMDLINE_TEXT modules-load=dwc2,g_ether"
        fi
        ok "USB ethernet gadget enabled (g_ether)"
    fi
fi

# ------------------------------------------------------------- first boot ---

echo
echo "First boot"

if [[ ! -f "$BOOT_PATH/ssh" && ! -f "$BOOT_PATH/ssh.txt" ]]; then
    run touch "$BOOT_PATH/ssh"
    ok "SSH enabled"
else
    say "SSH already enabled"
fi

# ---------------------------------------------------------- recording key ---
#
# Recordings are sealed to an X25519 public key as they are written, and the
# private half stays on the ground station. That protects a payload someone
# else recovers -- but it has a failure mode that is total and silent: if the
# ground station does not hold the matching private key, the images and
# telemetry are not inconvenient to read, they are gone.
#
# That is not hypothetical. A card from this project carries 710 images sealed
# to a public key whose private half was never saved anywhere. Nothing can
# recover them.
#
# So the keypair is established here, at the moment the card is prepared, and
# encryption is only switched on once the pairing is confirmed to exist.

echo
echo "Recording encryption"

RECORDING_PUBLIC=""

if [[ $ENCRYPT -eq 0 ]]; then
    warn "encryption disabled: anyone who recovers this payload can read its"
    warn "images and telemetry"
else
    KEY_PATH="${RECORDING_KEY:-$HOME/.raptorhab/recording_key}"
    KEY_TOOL="$SOURCE_DIR/tools/recording_key.py"
    PYTHON_BIN="$(command -v python3 || true)"

    if [[ -z "$PYTHON_BIN" ]]; then
        die "python3 is needed to handle the recording key; install it or pass --no-encryption"
    fi

    if [[ ! -f "$KEY_PATH" ]]; then
        if [[ $GENERATE_KEY -eq 0 && $DRY_RUN -eq 0 ]]; then
            echo
            echo "  No recording key at $KEY_PATH."
            echo "  Without one, anything this payload encrypts can never be read."
            echo
            read -r -p "  Generate a keypair now? [Y/n] " answer
            case "$answer" in
                [Nn]*) die "refusing to enable encryption without a key; "\
                           "re-run with --no-encryption to record in the clear" ;;
            esac
        fi
        if [[ $DRY_RUN -eq 1 ]]; then
            say "would generate a recording keypair at $KEY_PATH"
        else
            "$PYTHON_BIN" "$KEY_TOOL" --key "$KEY_PATH" generate \
                || die "could not generate a recording keypair"
        fi
    fi

    if [[ $DRY_RUN -eq 0 ]]; then
        # Read the .pub file rather than parsing the human-readable report
        # from `show`, which prints a fingerprint line too -- concatenating
        # them produced a "key" that was two fields glued together.
        PUB_PATH="${KEY_PATH%.*}.pub"
        [[ -f "$PUB_PATH" ]] || PUB_PATH="$KEY_PATH.pub"
        if [[ -f "$PUB_PATH" ]]; then
            RECORDING_PUBLIC="$(tr -d ' \r\n' < "$PUB_PATH")"
        else
            RECORDING_PUBLIC="$("$PYTHON_BIN" "$KEY_TOOL" --key "$KEY_PATH" show 2>/dev/null \
                                | awk '/Public key/ {print $NF}' | tr -d ' \r\n')"
        fi
        [[ -n "$RECORDING_PUBLIC" ]] \
            || die "could not read the public key from $KEY_PATH"
        ok "sealing to $RECORDING_PUBLIC"
        ok "private key stays at $KEY_PATH and never goes near the card"
        warn "back that file up now. There is no recovery if it is lost, and"
        warn "every recording sealed to it becomes unreadable."
    fi
fi

FIRSTRUN="$BOOT_PATH/firstrun.sh"
FIRSTRUN_BODY="#!/bin/bash
# Generated by RaptorHAB provision_sd.sh. Runs once, as root, on first boot.
set +e
LOG=/var/log/raptorhab-firstrun.log
exec >>\$LOG 2>&1
echo \"--- RaptorHAB first boot: \$(date) ---\"
TARGET_USER=\"$USERNAME\"

$( [[ $CLOUD_INIT -eq 1 ]] && echo "# Hostname left to cloud-init." || cat <<HOSTBLOCK
# Hostname
CURRENT=\$(cat /etc/hostname | tr -d ' \\t\\n\\r')
echo '$HOSTNAME_NEW' > /etc/hostname
sed -i "s/127.0.1.1.*\$CURRENT/127.0.1.1\\t$HOSTNAME_NEW/g" /etc/hosts
echo "hostname set to $HOSTNAME_NEW"
HOSTBLOCK
)
"

if [[ -n "$USERNAME" && $CLOUD_INIT -eq 1 ]]; then
    warn "--user ignored: cloud-init already defines the account, and creating"
    warn "it here first would leave you with a password Imager did not set"
    USERNAME=""
fi

if [[ -n "$USERNAME" ]]; then
    [[ -n "$PASSWORD" ]] || die "--user needs --password"
    if command -v openssl >/dev/null 2>&1; then
        PW_HASH="$(echo "$PASSWORD" | openssl passwd -6 -stdin)"
    else
        die "openssl is needed to hash the password; install it or omit --user"
    fi
    FIRSTRUN_BODY+="
# Account. The password is stored here already hashed -- the plain text never
# reaches the card.
if ! id -u '$USERNAME' >/dev/null 2>&1; then
    useradd -m -s /bin/bash '$USERNAME'
    for g in adm dialout sudo video gpio spi i2c plugdev; do
        getent group \$g >/dev/null && usermod -aG \$g '$USERNAME'
    done
fi
echo '$USERNAME:$PW_HASH' | chpasswd -e
echo '$USERNAME ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/010-raptorhab
chmod 440 /etc/sudoers.d/010-raptorhab
echo \"user $USERNAME created\"
"
    ok "user '$USERNAME' will be created on first boot"
fi

if [[ -n "$WIFI_SSID" && $CLOUD_INIT -eq 1 ]]; then
    warn "--wifi ignored: cloud-init already carries the network configuration"
    WIFI_SSID=""
fi

if [[ -n "$WIFI_SSID" ]]; then
    FIRSTRUN_BODY+="
# WiFi, written as a NetworkManager keyfile (Pi OS Bookworm and later).
raspi-config nonint do_wifi_country '$WIFI_COUNTRY' 2>/dev/null
nmcli connection add type wifi con-name 'raptorhab' ifname wlan0 \\
      ssid '$WIFI_SSID' 2>/dev/null
nmcli connection modify 'raptorhab' \\
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk '$WIFI_PASSWORD' 2>/dev/null
nmcli connection up 'raptorhab' 2>/dev/null
echo 'wifi configured for $WIFI_SSID'
"
    ok "WiFi '$WIFI_SSID' will be configured"
elif [[ $CLOUD_INIT -eq 1 ]]; then
    say "WiFi comes from cloud-init; the USB gadget is there as a fallback"
else
    say "no WiFi configured — reach the Pi over the USB cable"
fi

FIRSTRUN_BODY+="
# Staged payload source. The archive has a single raptorhab-src/ root, so this
# lands at /opt/raptorhab-src regardless of where it was built.
SRC_TAR=/boot/firmware/raptorhab-src.tar.gz
[ -f \$SRC_TAR ] || SRC_TAR=/boot/raptorhab-src.tar.gz
if [ -f \$SRC_TAR ]; then
    rm -rf /opt/raptorhab-src
    tar -xzf \$SRC_TAR -C /opt
    chmod +x /opt/raptorhab-src/setup/install.sh 2>/dev/null
    if [ -n \"\${TARGET_USER:-}\" ]; then
        chown -R \$TARGET_USER:\$TARGET_USER /opt/raptorhab-src 2>/dev/null
    fi
    echo \"payload source unpacked to /opt/raptorhab-src\"
else
    echo \"WARNING: no staged source found on the boot partition\"
fi
"

if [[ $AUTO_INSTALL -eq 1 ]]; then
    # The install cannot run from firstrun.sh. firstrun.sh is invoked by
    # systemd.run before the network is up and before cloud-init has applied
    # the WiFi configuration, so apt would have nothing to talk to. Running it
    # there fails every time, on a card that looks correctly provisioned.
    #
    # Instead firstrun.sh drops a one-shot unit that waits for the network and
    # for cloud-init to finish, installs, and then disables itself. The log it
    # leaves behind is the whole point: an unattended install that fails
    # silently is worse than one that never started.
    #
    # --usb-ethernet is deliberate. The card boots with g_ether so the Pi is
    # reachable before anything is installed, and a UDC takes exactly one
    # gadget driver -- so the installer's console cannot bind while g_ether
    # holds it. Installing the composite gadget with both CDC-ECM and CDC-ACM
    # replaces g_ether with something that keeps the network link *and* adds
    # the console. Without it, installing over the USB link would cut the
    # branch it is sitting on.
    FIRSTRUN_BODY+="
RECORDING_PUBLIC_ESCAPED='$RECORDING_PUBLIC'

# Post-install step: switch recording encryption on with the public key this
# card was prepared for. Written as its own script because embedding it in the
# unit means three levels of quoting, and quoting bugs in a file you cannot
# test until first boot are expensive.
cat > /usr/local/sbin/raptorhab-postinstall <<'POSTEOF'
#!/bin/bash
CONFIG=/var/lib/raptorhab/config/airborne.json
PUBKEY=\"__RECORDING_PUBLIC__\"
if [ -n \"\$PUBKEY\" ] && [ -f \"\$CONFIG\" ]; then
    python3 - \"\$CONFIG\" \"\$PUBKEY\" <<'PY'
import json, os, pwd, sys
path, pubkey = sys.argv[1], sys.argv[2]
with open(path) as handle:
    config = json.load(handle)
config[\"recording_public_key\"] = pubkey
config[\"recording_encryption_enabled\"] = True
with open(path, \"w\") as handle:
    json.dump(config, handle, indent=2, sort_keys=True)
try:
    entry = pwd.getpwnam(\"raptorhab\")
    os.chown(path, entry.pw_uid, entry.pw_gid)
except KeyError:
    pass
print(\"recording encryption enabled for\", pubkey)
PY
    systemctl restart raptorhab-airborne 2>/dev/null
fi
systemctl disable raptorhab-firstinstall.service 2>/dev/null
exit 0
POSTEOF
chmod +x /usr/local/sbin/raptorhab-postinstall
sed -i \"s|__RECORDING_PUBLIC__|\$RECORDING_PUBLIC_ESCAPED|\" /usr/local/sbin/raptorhab-postinstall
echo \"post-install step written\"

# Deferred install, once there is actually a network to install from.
cat > /etc/systemd/system/raptorhab-firstinstall.service <<'UNIT'
[Unit]
Description=RaptorHAB first-boot installation
After=network-online.target cloud-init.service
Wants=network-online.target
ConditionPathExists=/opt/raptorhab-src/setup/install.sh
ConditionPathExists=!/opt/raptorhab/.venv/bin/python

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=3600
ExecStart=/opt/raptorhab-src/setup/install.sh --usb-gadget --usb-ethernet ${CAMERA_OVERLAY:+--camera $CAMERA_OVERLAY}
ExecStartPost=/usr/local/sbin/raptorhab-postinstall
StandardOutput=append:/var/log/raptorhab-install.log
StandardError=append:/var/log/raptorhab-install.log

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable raptorhab-firstinstall.service
echo 'deferred install unit enabled; it runs once the network is up'
"
    say "installer will run automatically once the network is up on first boot"
fi

FIRSTRUN_BODY+="
# Remove ourselves from the kernel command line so this runs exactly once.
sed -i 's| systemd.run=[^ ]*||g; s| systemd.run_success_action=[^ ]*||g; s| systemd.unit=[^ ]*||g' /boot/firmware/cmdline.txt
rm -f /boot/firmware/firstrun.sh
echo '--- first boot complete ---'
exit 0
"

write_file "$FIRSTRUN" "$FIRSTRUN_BODY"
run chmod +x "$FIRSTRUN"
ok "firstrun.sh written"

# Hook firstrun into the kernel command line, the same way Raspberry Pi Imager
# does. firstrun.sh strips these again when it finishes.
if [[ "$CMDLINE_TEXT" != *"systemd.run="* ]]; then
    CMDLINE_TEXT="$CMDLINE_TEXT systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target"
    ok "first-boot hook added to cmdline.txt"
fi

write_file "$CMDLINE" "$CMDLINE_TEXT
"

# ---------------------------------------------------------- stage source ---

echo
echo "Staging payload source"

if [[ $DRY_RUN -eq 1 ]]; then
    say "would stage $SOURCE_DIR as raptorhab-src.tar.gz"
else
    TARBALL="$BOOT_PATH/raptorhab-src.tar.gz"

    # Stage through a directory literally named raptorhab-src so the archive
    # has one predictable top-level entry. GNU tar could do this with
    # --transform, but macOS ships bsdtar, which cannot -- and the failure is
    # silent: the archive still builds, just flattened, and the payload ends up
    # scattered across /opt with no install.sh where firstrun.sh looks for it.
    STAGE="$(mktemp -d)"
    trap 'rm -rf "$STAGE"' EXIT
    mkdir -p "$STAGE/raptorhab-src"
    # --no-xattrs keeps macOS provenance attributes out of the archive. They
    # are harmless, but bsdtar records them as extended headers and GNU tar on
    # the Pi prints a warning for every single file it does not recognise --
    # dozens of lines of noise in the first-boot log, which is exactly where
    # someone will later be looking for a real error.
    TAR_FLAGS=()
    if tar --no-xattrs -cf /dev/null -T /dev/null 2>/dev/null; then
        TAR_FLAGS+=(--no-xattrs)
    fi
    (cd "$SOURCE_DIR" && tar "${TAR_FLAGS[@]}" -cf - \
        --exclude '__pycache__' --exclude '.venv' --exclude '*.pyc' \
        --exclude '.pytest_cache' --exclude '.DS_Store' --exclude '*.egg-info' \
        .) | (cd "$STAGE/raptorhab-src" && tar -xf -)
    tar "${TAR_FLAGS[@]}" -czf "$TARBALL" -C "$STAGE" raptorhab-src

    # Prove the layout is what firstrun.sh will expect, rather than trusting it.
    if ! tar -tzf "$TARBALL" | grep -q '^raptorhab-src/setup/install.sh$'; then
        die "staged archive is missing raptorhab-src/setup/install.sh — refusing to ship a card that cannot install"
    fi
    ok "staged $(du -h "$TARBALL" | cut -f1) to $(basename "$TARBALL")"
    date -u '+%Y-%m-%dT%H:%M:%SZ' > "$BOOT_PATH/raptorhab-provisioned"
fi

# Sweep the sidecars macOS creates as a side effect of writing to FAT32.
if [[ $DRY_RUN -eq 0 ]]; then
    if command -v dot_clean >/dev/null 2>&1; then
        dot_clean -m "$BOOT_PATH" 2>/dev/null || true
    fi
    rm -f "$BOOT_PATH"/._* "$BOOT_PATH"/.DS_Store 2>/dev/null || true
    ok "removed macOS sidecar files"
fi

# ------------------------------------------------------------------ done ---

cat <<NEXT

Done.

Eject the card, put it in the Pi, and connect a USB cable to the Pi's **data**
port (the inner one on a Zero 2 W, not the one marked PWR).

First boot takes a couple of minutes: the filesystem is resized and the Pi
reboots once by itself.

Then, with no WiFi involved:

  1. A new network interface appears on your machine (macOS: System Settings >
     Network, an "RNDIS/Ethernet Gadget"). Give it a manual address such as
     10.55.0.2/24.
  2. ssh ${USERNAME:-<user>}@10.55.0.1        (or raptorhab.local)

To run the installer, the Pi needs internet for apt. Share your connection over
that USB interface -- macOS: System Settings > General > Sharing > Internet
Sharing, from Wi-Fi to the gadget interface -- then:

  sudo /opt/raptorhab-src/setup/install.sh --usb-gadget${CAMERA_OVERLAY:+ --camera $CAMERA_OVERLAY}

The payload never has to join your WiFi.

NEXT
