#!/usr/bin/env python3
"""
Manage the keypair that protects recovered flight recordings.

The balloon carries only the public key. It seals every image and every
telemetry row as it writes them and cannot open any of them again, so a
stranger who recovers the payload gets ciphertext. The private key stays here,
on the ground, and is the one thing that must never fly.

    # Once, on the ground station
    python3 tools/recording_key.py generate

    # Put the printed public key into the payload's configuration, then
    python3 tools/recording_key.py decrypt /path/from/sd/card --out ./recovered

Run `generate` on your Mac, not on the Pi.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.sealedbox import (
    SealedBoxError,
    format_key,
    generate_keypair,
    key_fingerprint,
    open_sealed,
    parse_public_key,
    public_key_from_private,
)
from common.sealedwriter import SEALED_SUFFIX

DEFAULT_KEY_PATH = Path.home() / ".raptorhab" / "recording_key"


def command_generate(args) -> int:
    path = Path(args.key or DEFAULT_KEY_PATH)

    if path.exists() and not args.force:
        print(f"error: {path} already exists.\n"
              f"       Overwriting it makes every recording sealed to the old "
              f"key permanently unreadable.\n"
              f"       Pass --force only if you are certain.", file=sys.stderr)
        return 1

    private, public = generate_keypair()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(private)
    path.chmod(0o600)
    path.with_suffix(".pub").write_text(format_key(public) + "\n")

    print(f"Private key  {path}  (0600 — keep this off the balloon)")
    print(f"Public key   {path.with_suffix('.pub')}")
    print(f"Fingerprint  {key_fingerprint(public)}")
    print()
    print("Set this on the payload:")
    print()
    print(f"    recording_encryption_enabled = true")
    print(f"    recording_public_key = {format_key(public)}")
    print()
    print("There is no recovery if the private key is lost. Back it up before "
          "you fly.")
    return 0


def command_show(args) -> int:
    path = Path(args.key or DEFAULT_KEY_PATH)
    if not path.is_file():
        print(f"error: no key at {path}; run `generate` first", file=sys.stderr)
        return 1

    public = public_key_from_private(path.read_bytes())
    print(f"Public key   {format_key(public)}")
    print(f"Fingerprint  {key_fingerprint(public)}")
    return 0


def _decrypt_records(data: bytes, private: bytes) -> bytes:
    """
    Walk a sealed log, which is a sequence of length-prefixed records.

    Written that way because a balloon can lose power at any moment: one
    growing box truncated mid-flight would be undecryptable in its entirety,
    whereas losing the tail of a record stream costs only the tail.
    """
    out = bytearray()
    offset = 0
    truncated = 0

    while offset + 4 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        offset += 4
        if offset + length > len(data):
            truncated += 1
            break
        out += open_sealed(data[offset:offset + length], private)
        offset += length

    if truncated:
        print("    note: the final record is truncated (power loss mid-write); "
              "everything before it recovered", file=sys.stderr)
    return bytes(out)


def command_decrypt(args) -> int:
    path = Path(args.key or DEFAULT_KEY_PATH)
    if not path.is_file():
        print(f"error: no private key at {path}", file=sys.stderr)
        return 1
    private = path.read_bytes()

    source = Path(args.source)
    files = ([source] if source.is_file()
             else sorted(source.rglob(f"*{SEALED_SUFFIX}")))
    if not files:
        print(f"no {SEALED_SUFFIX} files under {source}", file=sys.stderr)
        return 1

    destination = Path(args.out)
    destination.mkdir(parents=True, exist_ok=True)

    ok = failed = 0
    for item in files:
        target = destination / item.name.removesuffix(SEALED_SUFFIX)
        raw = item.read_bytes()
        try:
            # A log is a record stream; an image is a single box.
            plaintext = (_decrypt_records(raw, private)
                         if item.name.endswith(f".csv{SEALED_SUFFIX}")
                         or item.name.endswith(f".log{SEALED_SUFFIX}")
                         else open_sealed(raw, private))
            target.write_bytes(plaintext)
            print(f"  {item.name}  ->  {target.name}  ({len(plaintext)} bytes)")
            ok += 1
        except SealedBoxError as e:
            print(f"  {item.name}  FAILED: {e}", file=sys.stderr)
            failed += 1

    print(f"\n{ok} recovered, {failed} failed, into {destination}")
    if failed:
        print("Failures usually mean the file was sealed to a different key.",
              file=sys.stderr)
    return 0 if ok and not failed else (1 if failed else 0)


def command_verify(args) -> int:
    """Confirm a configured public key is well-formed before flight."""
    try:
        key = parse_public_key(args.public_key)
    except ValueError as e:
        print(f"invalid: {e}", file=sys.stderr)
        return 1

    if key is None:
        print("empty — recording encryption would be disabled")
        return 1

    print(f"valid X25519 public key, fingerprint {key_fingerprint(key)}")

    path = Path(args.key or DEFAULT_KEY_PATH)
    if path.is_file():
        mine = public_key_from_private(path.read_bytes())
        match = mine == key
        print(f"matches the private key at {path}: {'yes' if match else 'NO'}")
        if not match:
            print("  recordings sealed to it will NOT be readable here",
                  file=sys.stderr)
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", help=f"Private key path (default {DEFAULT_KEY_PATH})")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="Create a new keypair")
    g.add_argument("--force", action="store_true",
                   help="Overwrite an existing key, making old recordings unreadable")
    g.set_defaults(func=command_generate)

    sub.add_parser("show", help="Print the public key").set_defaults(func=command_show)

    d = sub.add_parser("decrypt", help="Decrypt recovered files")
    d.add_argument("source", help="A sealed file, or a directory to search")
    d.add_argument("--out", default="./recovered", help="Where to write plaintext")
    d.set_defaults(func=command_decrypt)

    v = sub.add_parser("verify", help="Check a public key before flight")
    v.add_argument("public_key")
    v.set_defaults(func=command_verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
