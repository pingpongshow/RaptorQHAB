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

# CDC-ECM adds a point-to-point network link over the same cable. Off by
# default and deliberately so: an ethernet gadget creates an interface that
# systemd-networkd-wait-online will block on, adding tens of seconds to every
# boot when nothing is plugged in -- a bad trade on a battery-powered payload.
#
# It is invaluable on the bench though, and it is the only way in when the Pi
# and the workstation land on Wi-Fi bands that cannot see each other. Enable
# with RAPTORHAB_USB_ETHERNET=1, which the installer sets via a drop-in.
USB_ETHERNET="${RAPTORHAB_USB_ETHERNET:-0}"

# Locally-administered MAC addresses, stable so the host does not invent a new
# interface on every reconnect.
ECM_HOST_MAC="02:1a:11:00:00:01"
ECM_SELF_MAC="02:1a:11:00:00:02"
ECM_ADDRESS="10.55.0.1/24"

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
    # A card prepared by provision_sd.sh boots with g_ether, so the Pi is
    # reachable over USB before anything is installed. That module binds the
    # USB device controller, and a UDC can only have one gadget driver bound
    # at a time -- so libcomposite would silently fail to bind and the console
    # would never appear. Hand the controller over rather than fighting for it.
    if lsmod 2>/dev/null | grep -qE '^(g_ether|usb_f_ecm|g_serial|g_cdc) '; then
        echo "releasing the bootstrap USB gadget so libcomposite can bind"
        modprobe -r g_ether 2>/dev/null || true
        modprobe -r g_serial 2>/dev/null || true
        modprobe -r g_cdc 2>/dev/null || true
    fi

    # Stop it coming back on the next boot, for the same reason.
    if [[ -f /boot/firmware/cmdline.txt ]] && \
       grep -q 'modules-load=dwc2,g_ether' /boot/firmware/cmdline.txt; then
        sed -i 's/ modules-load=dwc2,g_ether//' /boot/firmware/cmdline.txt
        echo "removed the bootstrap g_ether from cmdline.txt; libcomposite owns the port now"
    fi

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

    if [[ "$USB_ETHERNET" == "1" ]]; then
        mkdir -p functions/ecm.usb0
        echo "$ECM_HOST_MAC" > functions/ecm.usb0/host_addr
        echo "$ECM_SELF_MAC" > functions/ecm.usb0/dev_addr
        ln -s functions/ecm.usb0 configs/c.1/
        echo "CDC-ECM enabled"
    fi

    # Binding to the UDC is what actually presents the device to the host.
    UDC="$(ls /sys/class/udc | head -1)"
    if [[ -z "$UDC" ]]; then
        echo "no UDC found: is dtoverlay=dwc2 set and the dwc2 module loaded?" >&2
        exit 1
    fi
    echo "$UDC" > UDC

    if [[ "$USB_ETHERNET" == "1" ]]; then
        # Give the link a static address. DHCP would need a server aboard the
        # payload, which is more moving parts than a two-host cable warrants.
        for _ in $(seq 20); do
            iface=$(ls /sys/class/net | grep -E '^usb[0-9]+$' | head -1 || true)
            [[ -n "$iface" ]] && break
            sleep 0.25
        done
        if [[ -n "${iface:-}" ]]; then
            ip addr flush dev "$iface" 2>/dev/null || true
            ip addr add "$ECM_ADDRESS" dev "$iface"
            ip link set "$iface" up
            echo "USB ethernet up on $iface at $ECM_ADDRESS"
        else
            echo "warning: CDC-ECM requested but no usb* interface appeared" >&2
        fi
    fi

    echo "USB gadget up on $UDC as /dev/ttyGS0"
    ;;

stop)
    [[ -d "$GADGET_DIR" ]] || exit 0
    cd "$GADGET_DIR"

    # Unbind first, or the directories are busy and removal fails.
    echo "" > UDC 2>/dev/null || true

    rm -f configs/c.1/acm.usb0
    rm -f configs/c.1/ecm.usb0
    rmdir configs/c.1/strings/0x409 2>/dev/null || true
    rmdir configs/c.1 2>/dev/null || true
    rmdir functions/acm.usb0 2>/dev/null || true
    rmdir functions/ecm.usb0 2>/dev/null || true
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
