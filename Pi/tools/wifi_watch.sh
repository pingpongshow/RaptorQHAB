#!/bin/bash
#
# Record what the WiFi radio thought, so the next time the payload goes
# unreachable there is evidence instead of a hypothesis.
#
# This exists because four plausible explanations for that fault were tested
# and all four were wrong:
#
#   AP client isolation   -- disproved: the payload can ping the Mac that
#                            cannot reach it, and isolation blocks both ways.
#   802.11 power save     -- disproved: `iw dev wlan0 get power_save` reports
#                            off, and NetworkManager resolves wifi.powersave=2.
#   SDIO runtime suspend  -- disproved: runtime_status is "unsupported".
#   Regulatory domain     -- disproved: phy0 is self-managed (country 99), so
#                            the global "country 00" and its thousands of
#                            REGDOM-CHANGE messages are cosmetic.
#
# The fault is real but intermittent, and would not reproduce during eight
# minutes of deliberate silence. So: watch passively and wait for it.
#
# Deliberately passive. It sends nothing, because generating traffic is exactly
# what makes the payload reachable again -- a probe here would paper over the
# thing being investigated. Everything below is read from the kernel.
#
set -u
IFACE="${IFACE:-wlan0}"
LOG="${LOG:-/var/lib/raptorhab/wifi_watch.log}"
INTERVAL="${INTERVAL:-20}"
IW=/usr/sbin/iw

mkdir -p "$(dirname "$LOG")"

# iw indents each field with a tab and separates label from value with a colon
# and more whitespace: "\tinactive time:\t0 ms". Splitting on tabs puts an empty
# string in $1 and the *label* in $2, which is how the first version of this
# silently recorded "?" for everything. Anchor on the label instead.
#
# brcmfmac reports only a subset of the fields iw knows about -- no tx-retry or
# beacon-loss counters -- so those are not collected.
field() {
    sed -n "s/^[[:space:]]*$1:[[:space:]]*\(-\{0,1\}[0-9]\{1,\}\).*/\1/p" <<< "$2" | head -1
}

while true; do
    now=$(date -Is)

    read -r rx tx < <(awk -v i="$IFACE:" '$1==i {print $2, $10}' /proc/net/dev)
    rx=${rx:-0}; tx=${tx:-0}

    station=$($IW dev "$IFACE" station dump 2>/dev/null)
    link=$($IW dev "$IFACE" link 2>/dev/null)

    # "inactive time" is how long since the AP last heard anything from us,
    # which is the number that matters if association state is aging out.
    # inactive time is the number that matters if association state is aging:
    # how long since the AP last heard a frame from us.
    inactive=$(field "inactive time" "$station")
    signal=$(field "signal" "$station")
    failed=$(field "tx failed" "$station")
    # Resets on reassociation, so a drop here means the link was rebuilt even
    # if nothing logged a disconnect.
    connected=$(field "connected time" "$station")
    bssid=$(sed -n 's/.*Connected to \([0-9a-f:]\+\).*/\1/p' <<< "$link" | head -1)
    ps=$($IW dev "$IFACE" get power_save 2>/dev/null | sed 's/.*: //')
    ipv4=$(ip -4 -o addr show "$IFACE" 2>/dev/null | awk '{print $4}' | head -1)

    printf '%s rx=%s tx=%s inactive_ms=%s signal_dbm=%s tx_failed=%s assoc_age_s=%s bssid=%s ps=%s ip=%s\n' \
        "$now" "$rx" "$tx" "${inactive:-?}" "${signal:-?}" "${failed:-?}" \
        "${connected:-?}" "${bssid:-none}" "${ps:-?}" "${ipv4:-none}" >> "$LOG"

    # Keep it bounded; this runs for weeks on a card with limited space.
    if [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 2000000 ]; then
        tail -n 5000 "$LOG" > "$LOG.trim" && mv "$LOG.trim" "$LOG"
    fi

    sleep "$INTERVAL"
done
