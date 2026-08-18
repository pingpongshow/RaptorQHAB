#!/usr/bin/env python3
"""
Report -- and optionally remove -- everything a finder could read off the SD
card if the payload is recovered by someone else.

The card is not encrypted, and on an unattended device it fundamentally
cannot be in any way that helps: the balloon has to boot on the pad with
nobody there to type a passphrase, so any key it can use is a key that
travels with it. File permissions are irrelevant to whoever holds the card.

What genuinely works is not having the secrets aboard in the first place.
This audits what is currently recoverable and can strip the parts you do not
need in flight.

    sudo python3 tools/preflight_secrets.py            # report only
    sudo python3 tools/preflight_secrets.py --sanitize # strip what it can

Nothing is removed without --sanitize, and each removal is named as it happens.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

RED = "\033[31m"; YELLOW = "\033[33m"; GREEN = "\033[32m"; BOLD = "\033[1m"; OFF = "\033[0m"


@dataclass
class Finding:
    name: str
    severity: str                    # "high", "medium", "low"
    detail: str
    why: str
    remove: Optional[Callable[[], str]] = None
    paths: List[str] = field(default_factory=list)


def _exists(pattern: str) -> List[str]:
    return sorted(p for p in glob.glob(pattern) if os.path.exists(p))


def find_wifi_credentials() -> Optional[Finding]:
    """Network credentials are the most damaging thing on a recovered card."""
    profiles: List[str] = []
    for pattern in ("/etc/NetworkManager/system-connections/*",
                    "/etc/wpa_supplicant/wpa_supplicant*.conf"):
        profiles.extend(_exists(pattern))

    with_secrets = []
    for path in profiles:
        try:
            text = Path(path).read_text(errors="ignore")
        except OSError:
            continue
        if "psk=" in text or "password=" in text:
            with_secrets.append(path)

    if not with_secrets:
        return None

    def remove() -> str:
        for path in with_secrets:
            os.remove(path)
        return f"removed {len(with_secrets)} network profile(s)"

    return Finding(
        name="Wi-Fi credentials",
        severity="high",
        detail=f"{len(with_secrets)} profile(s) contain a pre-shared key in plaintext",
        why="Whoever recovers the payload gets the password to your network. "
            "This is almost always the worst thing on the card.",
        remove=remove,
        paths=with_secrets,
    )


def find_ssh_material() -> Optional[Finding]:
    keys = _exists("/etc/ssh/ssh_host_*_key")
    authorized = _exists("/home/*/.ssh/authorized_keys") + _exists("/root/.ssh/authorized_keys")
    private = _exists("/home/*/.ssh/id_*") + _exists("/root/.ssh/id_*")
    private = [p for p in private if not p.endswith(".pub")]

    if not (keys or authorized or private):
        return None

    def remove() -> str:
        removed = 0
        for path in keys + authorized + private:
            os.remove(path)
            removed += 1
        return f"removed {removed} SSH file(s); host keys regenerate on next boot"

    return Finding(
        name="SSH key material",
        severity="medium" if not private else "high",
        detail=f"{len(keys)} host key(s), {len(authorized)} authorized_keys, "
               f"{len(private)} user private key(s)",
        why="Host keys let someone impersonate this Pi. A user private key, if "
            "present, may open other machines.",
        remove=remove,
        paths=keys + authorized + private,
    )


def find_channel_keys(config_path: str) -> Optional[Finding]:
    """
    A private Meshtastic channel key sits in the config in plaintext.

    Not removable here: the payload needs it to transmit on that channel. The
    answer is to treat it as burned after any flight where the payload leaves
    your possession.
    """
    if not os.path.isfile(config_path):
        return None

    import json
    try:
        values = json.loads(Path(config_path).read_text())
    except (OSError, ValueError):
        return None

    private = (values.get("meshtastic_private_psk") or "").strip()
    if not private:
        return None

    return Finding(
        name="Meshtastic private channel key",
        severity="high",
        detail=f"{config_path} holds a private channel key in plaintext",
        why="The payload must be able to transmit on that channel unattended, "
            "so the key has to be readable to it -- and therefore to anyone "
            "holding the card. Treat the key as compromised after any flight "
            "you do not recover yourself, and rotate it on your handhelds.",
        paths=[config_path],
    )


def find_flight_data(state_root: str) -> Optional[Finding]:
    logs = _exists(f"{state_root}/logs/*")
    images = _exists(f"{state_root}/images/*")
    if not (logs or images):
        return None

    total = sum(os.path.getsize(p) for p in logs + images if os.path.isfile(p))

    def remove() -> str:
        for path in logs + images:
            if os.path.isfile(path):
                os.remove(path)
        return f"removed {len(logs) + len(images)} file(s)"

    return Finding(
        name="Flight data from previous flights",
        severity="low",
        detail=f"{len(images)} image(s), {len(logs)} log file(s), "
               f"{total / 2**20:.1f} MB",
        why="Old telemetry logs contain the positions of previous flights, "
            "including where you launch from.",
        remove=remove,
        paths=logs + images,
    )


def find_session_sudoers() -> Optional[Finding]:
    entries = [p for p in _exists("/etc/sudoers.d/*") if "raptorhab-session" in p]
    if not entries:
        return None

    def remove() -> str:
        for path in entries:
            os.remove(path)
        return f"removed {len(entries)} sudoers entry"

    return Finding(
        name="Passwordless sudo rule",
        severity="medium",
        detail=", ".join(entries),
        why="Grants password-free root to a login account. Convenient while "
            "working on the payload, not something to fly with.",
        remove=remove,
        paths=entries,
    )


def find_encryption_state() -> Finding:
    """State the plain fact about the card, whatever else is found."""
    encrypted = False
    try:
        output = subprocess.run(["lsblk", "-o", "FSTYPE"], capture_output=True,
                                text=True, timeout=10).stdout
        encrypted = "crypto_LUKS" in output
    except (subprocess.SubprocessError, OSError):
        pass

    if encrypted:
        return Finding(
            name="Card encryption", severity="low",
            detail="a LUKS volume is present",
            why="Note that a balloon must boot unattended, so confirm the key "
                "is not simply stored alongside it.",
        )

    return Finding(
        name="Card encryption",
        severity="high",
        detail="the SD card is NOT encrypted; anyone can mount it and read everything",
        why="This cannot be fixed by encrypting the card. The payload boots on "
            "the pad with nobody there to unlock it, so any key it can use "
            "travels with it. The workable approach is to carry fewer secrets: "
            "strip credentials before flight, and encrypt anything written "
            "during flight to a PUBLIC key so the payload itself cannot read "
            "it back.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sanitize", action="store_true",
                        help="Remove what can be removed. Nothing is touched without this.")
    parser.add_argument("--state-root", default=os.environ.get(
        "RAPTORHAB_STATE_ROOT", "/var/lib/raptorhab"))
    parser.add_argument("--keep-ssh", action="store_true",
                        help="Keep SSH access when sanitising (you lose remote access otherwise)")
    parser.add_argument("--keep-wifi", action="store_true",
                        help="Keep Wi-Fi credentials when sanitising")
    args = parser.parse_args()

    config_path = os.path.join(args.state_root, "config", "airborne.json")

    findings = [f for f in (
        find_encryption_state(),
        find_wifi_credentials(),
        find_ssh_material(),
        find_channel_keys(config_path),
        find_session_sudoers(),
        find_flight_data(args.state_root),
    ) if f is not None]

    colour = {"high": RED, "medium": YELLOW, "low": GREEN}

    print(f"\n{BOLD}What a finder could read off this card{OFF}\n")
    for finding in findings:
        mark = colour[finding.severity]
        print(f"  {mark}[{finding.severity:^6}]{OFF} {BOLD}{finding.name}{OFF}")
        print(f"           {finding.detail}")
        print(f"           {finding.why}")
        if finding.paths and len(finding.paths) <= 4:
            for path in finding.paths:
                print(f"             {path}")
        elif finding.paths:
            print(f"             {finding.paths[0]} (+{len(finding.paths) - 1} more)")
        print()

    if not args.sanitize:
        removable = [f for f in findings if f.remove]
        if removable:
            print(f"{BOLD}Re-run with --sanitize to remove:{OFF} "
                  + ", ".join(f.name for f in removable))
            print("Nothing has been changed.\n")
        return 0

    if os.geteuid() != 0:
        print(f"{RED}--sanitize needs root{OFF}\n")
        return 2

    print(f"{BOLD}Sanitising{OFF}\n")
    skipped = []
    for finding in findings:
        if finding.remove is None:
            continue
        if args.keep_ssh and "SSH" in finding.name:
            skipped.append(finding.name); continue
        if args.keep_wifi and "Wi-Fi" in finding.name:
            skipped.append(finding.name); continue
        try:
            print(f"  {GREEN}removed{OFF}  {finding.name}: {finding.remove()}")
        except OSError as e:
            print(f"  {RED}failed {OFF}  {finding.name}: {e}")

    for name in skipped:
        print(f"  {YELLOW}kept   {OFF}  {name} (by request)")

    print(f"\n{BOLD}Reminder:{OFF} a private Meshtastic channel key cannot be "
          f"removed -- the payload needs it to transmit. Rotate it after any "
          f"flight you do not recover yourself.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
