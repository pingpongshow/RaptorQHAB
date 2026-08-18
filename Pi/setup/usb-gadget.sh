#!/usr/bin/env bash
#
# Bring up the Pi Zero's USB port as a CDC-ACM serial gadget.
#
# The Mac then sees a /dev/cu.usbmodem* device. That is enough for a terminal
# today, and it is the transport the Phase 2 configuration app will use.
#
# Why libcomposite rather than the simpler g_serial module: g_serial gives no
# control over the USB descriptors, so every RaptorHab payload would present
# the same generic Linux VID/PID as every other gadget on the bus. The Mac app
# needs to tell a RaptorHab payload apart from a Heltec modem and a Meshtastic
# node, and a distinct product string is how it does that.

set -euo pipefail

GADGET_DIR="/sys/kernel/config/usb_gadget/raptorhab"

# 0x1d6b/0x0104 is the Linux Foundation's multifunction composite gadget ID.
# It is the correct choice for a device without its own USB-IF vendor ID:
# honest about what it is, and stable enough for the host to match on.
VENDOR_ID="0x1d6b"
PRODUCT_ID="0x0104"

SERIAL="$(tr -d '\0' < /proc/device-tree/serial-number 2>/dev/null || echo 000000)"
MANUFACTURER="RaptorHab"
PRODUCT="RaptorHab Payload"

case "${1:-start}" in
start)
    modprobe libcomposite

    if [[ -d "$GADGET_DIR" ]]; then
        echo "gadget already configured"
        exit 0
    fi

    mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config

    mkdir -p "$GADGET_DIR"
    cd "$GADGET_DIR"

    echo "$VENDOR_ID"  > idVendor
    echo "$PRODUCT_ID" > idProduct
    echo 0x0100        > bcdDevice   # v1.0.0
    echo 0x0200        > bcdUSB      # USB 2.0

    mkdir -p strings/0x409
    echo "$SERIAL"       > strings/0x409/serialnumber
    echo "$MANUFACTURER" > strings/0x409/manufacturer
    echo "$PRODUCT"      > strings/0x409/product

    mkdir -p configs/c.1/strings/0x409
    echo "CDC ACM" > configs/c.1/strings/0x409/configuration
    # The Pi draws its own power; this is the descriptor's advertised budget.
    echo 250 > configs/c.1/MaxPower

    mkdir -p functions/acm.usb0
    ln -s functions/acm.usb0 configs/c.1/

    # Binding to the UDC is what actually presents the device to the host.
    UDC="$(ls /sys/class/udc | head -1)"
    if [[ -z "$UDC" ]]; then
        echo "no UDC found: is dtoverlay=dwc2 set and the dwc2 module loaded?" >&2
        exit 1
    fi
    echo "$UDC" > UDC

    echo "USB gadget up on $UDC as /dev/ttyGS0"
    ;;

stop)
    [[ -d "$GADGET_DIR" ]] || exit 0
    cd "$GADGET_DIR"

    # Unbind first, or the directories are busy and removal fails.
    echo "" > UDC 2>/dev/null || true

    rm -f configs/c.1/acm.usb0
    rmdir configs/c.1/strings/0x409 2>/dev/null || true
    rmdir configs/c.1 2>/dev/null || true
    rmdir functions/acm.usb0 2>/dev/null || true
    rmdir strings/0x409 2>/dev/null || true
    cd /
    rmdir "$GADGET_DIR" 2>/dev/null || true

    echo "USB gadget torn down"
    ;;

*)
    echo "usage: $0 {start|stop}" >&2
    exit 1
    ;;
esac
