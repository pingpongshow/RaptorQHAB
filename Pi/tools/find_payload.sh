#!/bin/bash
#
# Find the payload over the USB cable and print the command to connect to it.
#
# `ssh raptorhab.local` is normally all you need -- the payload advertises
# itself by mDNS and the connection goes over USB without configuring anything.
# This exists for when that does not work: a machine without mDNS, two payloads
# on one desk, or a name that has been claimed by something else.
#
# It finds the payload by asking every device on the USB link to identify
# itself (the IPv6 all-nodes multicast address), which needs no addresses, no
# DHCP and no administrator password. That last part matters: the USB link has
# no DHCP server, so the host self-assigns a 169.254 address while the payload
# sits on 10.55.0.1, and the two cannot speak IPv4 without someone hand-editing
# network settings. IPv6 link-local has no such problem.
#
set -u

USER_NAME="${1:-}"

case "$(uname -s)" in
    Darwin) IFACES=$(networksetup -listallhardwareports 2>/dev/null \
                | awk '/RaptorHab|Gadget|RNDIS|NCM/{getline; print $2}') ;;
    Linux)  IFACES=$(ls /sys/class/net 2>/dev/null | grep -E '^(usb|enp.*u|eth)') ;;
    *)      IFACES="" ;;
esac

# Nothing recognisable by name: sweep everything rather than give up.
[ -n "$IFACES" ] || IFACES=$(ifconfig -l 2>/dev/null || ls /sys/class/net)

found=""
for iface in $IFACES; do
    # Every IPv6 host answers this. The payload's gadget MAC starts 02:1a:11,
    # which is what distinguishes it from the machine's own reply.
    # -W means different things to the two ping6 implementations and is
    # rejected outright by the macOS one, so it is not used. -c bounds the run.
    replies=$(ping6 -c 2 -I "$iface" ff02::1 2>/dev/null \
              | sed -n 's/.*from \(fe80[^ ,]*\).*/\1/p' | sort -u)
    for addr in $replies; do
        case "$addr" in
            *1a:11ff:fe00:2*) found="$addr"; iface_found="$iface"; break 2 ;;
        esac
    done
done

if [ -z "$found" ]; then
    echo "No payload found on the USB link." >&2
    echo >&2
    echo "Check that:" >&2
    echo "  - the cable is in the Pi's USB port, not PWR IN" >&2
    echo "  - it is a data cable, not charge-only" >&2
    echo "  - the card was provisioned with --usb-ethernet" >&2
    echo >&2
    echo "Interfaces searched: ${IFACES:-none}" >&2
    exit 1
fi

echo "Payload found on $iface_found at $found"
echo
if [ -n "$USER_NAME" ]; then
    echo "  ssh $USER_NAME@$found"
else
    echo "  ssh <your-username>@$found"
    echo
    echo "(pass your username as an argument and this prints the whole command)"
fi
