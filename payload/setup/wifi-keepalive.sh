#!/bin/bash
#
# Keep the payload reachable over WiFi by not being silent on it.
#
# Diagnosed from raptorhab-wifi-watch over a seven-hour outage. Throughout it
# the radio was in perfect health -- signal -24 dBm, tx_failed 0 for the entire
# log, one unbroken 45,560-second association, power save off -- and not one
# inbound packet arrived, not even the broadcast ARP that precedes a ping. The
# access point had stopped delivering to this station while still acking its
# frames at the 802.11 layer.
#
# What made the payload a candidate for that: it put 4,101 bytes on WiFi in
# seven hours, with 97% of twenty-second samples showing zero transmit.
# Everything it does is LoRa and the USB gadget, so wlan0 sits silent for
# hours. An access point that ages out a station it has not heard from will
# age this one out every time.
#
# This is a mitigation, not a cure. The fault is at the access point -- a Pi
# that is associated and acking should be reachable. But a payload whose whole
# purpose is to be found should not depend on someone else's bridge table
# remembering it, and one small packet every thirty seconds is a cheap way not
# to.
#
# Costs nothing in flight: the WiFi radio is switched off after launch, so
# there is no gateway to reach and this does nothing.
#
set -u
INTERVAL="${INTERVAL:-30}"
IFACE="${IFACE:-wlan0}"

while true; do
    # The gateway on our own interface, re-read each time: it changes when the
    # network does, and a keepalive aimed at a stale address keeps nothing
    # alive.
    gateway=$(ip route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')

    if [ -n "$gateway" ]; then
        # One packet, one second. Failure is not an error worth logging -- the
        # gateway may be busy, and raptorhab-wifi-watch is the thing recording
        # the link's health.
        ping -c 1 -W 1 -I "$IFACE" "$gateway" >/dev/null 2>&1 || true
    fi

    sleep "$INTERVAL"
done
