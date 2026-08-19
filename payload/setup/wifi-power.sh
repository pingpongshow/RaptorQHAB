#!/bin/bash
#
# Turn the WiFi radio off in flight, and back on at boot.
#
# This exists because the payload runs as an unprivileged user. Both obvious
# ways to control the radio are closed to it:
#
#   rfkill block wifi      -> cannot open /dev/rfkill: Permission denied
#   nmcli radio wifi off   -> Not authorized to perform this operation
#
# Measured on the target, not assumed. Without this helper and its sudoers
# rule, the in-flight WiFi cutoff silently does nothing.
#
# Kept deliberately small and argument-free-ish: it is the single command the
# service is allowed to run as root, so its whole surface is these three verbs.
#
set -euo pipefail

# systemd-rfkill saves and restores the soft-block state across reboots
# (/var/lib/systemd/rfkill/*:wlan). That is the opposite of what a payload
# wants: block the radio in flight and the recovered payload boots unreachable.
# Clearing the saved state means a power cycle is always a way back in.
forget_saved_state() {
    rm -f /var/lib/systemd/rfkill/*:wlan 2>/dev/null || true
}

case "${1:-}" in
    off)
        rfkill block wifi
        forget_saved_state
        echo "wifi blocked"
        ;;
    on)
        rfkill unblock wifi
        forget_saved_state
        # NetworkManager can hold its own soft-off independent of rfkill.
        if command -v nmcli >/dev/null 2>&1; then
            nmcli radio wifi on >/dev/null 2>&1 || true
        fi
        echo "wifi unblocked"
        ;;
    status)
        rfkill list wifi
        ;;
    *)
        echo "usage: $0 {off|on|status}" >&2
        exit 2
        ;;
esac
