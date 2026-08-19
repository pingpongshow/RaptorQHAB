"""
Read a recovered payload's SD card.

After a flight the card holds far more than the radio ever sent: every image at
full quality rather than the handful that fit in the airtime budget, and the
complete telemetry log rather than the packets that happened to arrive. Getting
at it should not require SSH into a Pi that may no longer boot, or a card
reader on the same machine that flew the mission.

Most of it is sealed. Images and telemetry are encrypted to an X25519 public
key as they are written, so a finder who keeps the payload gets ciphertext. The
private half lives on the ground station and never flies.

That design has one sharp edge, and this module is built around warning about
it: if the ground station does not hold the private key matching the public key
the payload was configured with, the recordings are not merely inconvenient to
read, they are gone. Nothing can recover them. So the first thing this does
with a card is compare the two keys and say plainly whether the data is
readable, before anyone spends time copying it.
"""

import csv
import io
import logging
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .sealedbox import (
    SealedBoxError, open_sealed, parse_public_key, public_key_from_private,
)

logger = logging.getLogger(__name__)

SEALED_SUFFIX = ".rhs"
DEFAULT_KEY_PATH = Path.home() / ".raptorhab" / "recording_key"

# Where a payload keeps its state, relative to the root of the card.
STATE_ROOT = "var/lib/raptorhab"


@dataclass
class CardFile:
    path: Path
    name: str
    size: int
    sealed: bool
    kind: str            # "image" | "telemetry" | "log" | "other"
    modified: float

    def as_dict(self) -> dict:
        return {
            "path": str(self.path), "name": self.name, "size": self.size,
            "sealed": self.sealed, "kind": self.kind, "modified": self.modified,
        }


@dataclass
class CardSurvey:
    """What is on a card, and whether we can read it."""
    root: Path
    state_root: Optional[Path] = None
    images: List[CardFile] = field(default_factory=list)
    telemetry: List[CardFile] = field(default_factory=list)
    logs: List[CardFile] = field(default_factory=list)
    callsign: Optional[str] = None
    payload_public_key: Optional[str] = None
    have_private_key: bool = False
    key_matches: Optional[bool] = None
    notes: List[str] = field(default_factory=list)

    @property
    def sealed_count(self) -> int:
        return sum(1 for f in self.images + self.telemetry + self.logs if f.sealed)

    @property
    def readable(self) -> bool:
        """Whether the sealed material on this card can actually be opened."""
        return self.sealed_count == 0 or bool(self.key_matches)

    def as_dict(self) -> dict:
        return {
            "root": str(self.root),
            "state_root": str(self.state_root) if self.state_root else None,
            "callsign": self.callsign,
            "images": len(self.images),
            "telemetry": len(self.telemetry),
            "logs": len(self.logs),
            "sealed": self.sealed_count,
            "payload_public_key": self.payload_public_key,
            "have_private_key": self.have_private_key,
            "key_matches": self.key_matches,
            "readable": self.readable,
            "notes": self.notes,
            "total_bytes": sum(f.size for f in self.images + self.telemetry + self.logs),
        }


def candidate_cards() -> List[dict]:
    """
    Mounted volumes that look like a payload card.

    On macOS the payload's ext4 root mounts read-only if a driver is present
    and not at all otherwise, so a card that shows only `bootfs` is reported
    with that explanation rather than silently omitted -- "nothing found" and
    "your Mac cannot read ext4" are very different problems.
    """
    found = []
    roots = ["/Volumes"] if platform.system() == "Darwin" else \
            [f"/media/{os.environ.get('USER', '')}", "/run/media", "/mnt"]

    for base in roots:
        base_path = Path(base)
        if not base_path.is_dir():
            continue
        for entry in sorted(base_path.iterdir()):
            try:
                if not entry.is_dir():
                    continue
            except PermissionError:
                continue
            state = entry / STATE_ROOT
            if state.is_dir():
                found.append({"path": str(entry), "kind": "payload-root",
                              "detail": "payload data present"})
            elif (entry / "config.txt").is_file() and (entry / "cmdline.txt").is_file():
                found.append({"path": str(entry), "kind": "boot-only",
                              "detail": "Raspberry Pi boot partition; the payload's "
                                        "data lives on the ext4 root partition"})
    return found


def _classify(path: Path) -> Optional[str]:
    name = path.name
    stem = name[:-len(SEALED_SUFFIX)] if name.endswith(SEALED_SUFFIX) else name
    lowered = stem.lower()
    if lowered.endswith((".webp", ".jpg", ".jpeg", ".png")):
        return "image"
    if lowered.endswith(".csv"):
        return "telemetry"
    if lowered.endswith(".log") or lowered.endswith(".txt"):
        return "log"
    return None


def survey_card(root: str | Path,
                key_path: Optional[Path] = None) -> CardSurvey:
    """Look at a card and report what is there and whether it can be read."""
    root = Path(root)
    survey = CardSurvey(root=root)

    state = root / STATE_ROOT
    if not state.is_dir():
        # Perhaps they pointed at the state directory itself.
        if (root / "images").is_dir() or (root / "logs").is_dir():
            state = root
        else:
            survey.notes.append(
                f"No payload data under {root}. If this is a Raspberry Pi card, "
                f"the images and logs are on the ext4 root partition, not the "
                f"FAT32 boot partition.")
            return survey
    survey.state_root = state

    for directory, bucket in ((state / "images", survey.images),
                              (state / "logs", None)):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            kind = _classify(path)
            if kind is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entry = CardFile(path=path, name=path.name, size=stat.st_size,
                             sealed=path.name.endswith(SEALED_SUFFIX),
                             kind=kind, modified=stat.st_mtime)
            if kind == "image":
                survey.images.append(entry)
            elif kind == "telemetry":
                survey.telemetry.append(entry)
            else:
                survey.logs.append(entry)

    # The payload's own configuration tells us which key it sealed to.
    config = state / "config" / "airborne.json"
    if config.is_file():
        try:
            import json
            values = json.loads(config.read_text())
            survey.callsign = values.get("callsign")
            survey.payload_public_key = values.get("recording_public_key") or None
        except Exception as exc:
            survey.notes.append(f"Could not read the payload config: {exc}")
    elif survey.sealed_count:
        survey.notes.append(
            "The payload config is unreadable, so the key this card was sealed "
            "to cannot be confirmed.")

    _check_key(survey, key_path)
    return survey


def _check_key(survey: CardSurvey, key_path: Optional[Path]) -> None:
    """
    Compare the key on the card with the key we hold.

    This is the check worth doing before anything else. A mismatch is not a
    recoverable error: the recordings were encrypted to a public key whose
    private half does not exist here, and no amount of later effort changes
    that.
    """
    path = Path(key_path or DEFAULT_KEY_PATH)
    private = load_private_key(path)
    survey.have_private_key = private is not None

    if not survey.sealed_count:
        return

    if private is None:
        survey.notes.append(
            f"There is no recording key at {path}, so the {survey.sealed_count} "
            f"sealed file(s) on this card cannot be opened. If the key exists "
            f"elsewhere, point at it; if it was never kept, this data is not "
            f"recoverable.")
        return

    if not survey.payload_public_key:
        survey.notes.append(
            "A private key is available but the card does not say which public "
            "key it was sealed to; a decrypt attempt will show whether they match.")
        return

    expected = parse_public_key(survey.payload_public_key)
    if expected is None:
        survey.notes.append("The payload's recorded public key is malformed.")
        return

    survey.key_matches = (public_key_from_private(private) == expected)
    if not survey.key_matches:
        survey.notes.append(
            "The key held here does not match the one this payload sealed to. "
            "These recordings were encrypted for a different keypair and cannot "
            "be opened with this one.")


def load_private_key(path: Optional[Path] = None) -> Optional[bytes]:
    """
    Load the X25519 private key, however it happens to be stored.

    Raw bytes are tried first because that is what recording_key.py writes.
    Reading as text first was the original order, and it threw
    UnicodeDecodeError on every genuine key -- a ValueError, not an OSError, so
    it escaped the handler and took the whole card survey down with it.
    """
    path = Path(path or DEFAULT_KEY_PATH)
    if not path.is_file():
        return None

    try:
        raw = path.read_bytes()
    except OSError:
        return None

    if len(raw) == 32:
        return raw

    import base64
    import binascii

    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None

    for decoder in (binascii.unhexlify, base64.b64decode):
        try:
            decoded = decoder(text)
            if len(decoded) == 32:
                return decoded
        except Exception:
            continue
    return None



@dataclass
class ImportResult:
    copied: int = 0
    decrypted: int = 0
    failed: int = 0
    skipped: int = 0
    bytes_written: int = 0
    errors: List[str] = field(default_factory=list)
    output_dir: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "copied": self.copied, "decrypted": self.decrypted,
            "failed": self.failed, "skipped": self.skipped,
            "bytes_written": self.bytes_written, "errors": self.errors[:20],
            "output_dir": self.output_dir,
        }


def import_files(files: List[CardFile], output_dir: str | Path,
                 private_key: Optional[bytes] = None,
                 overwrite: bool = False,
                 progress: Optional[Callable[[int, int, str], None]] = None
                 ) -> ImportResult:
    """
    Copy files off a card, unsealing whatever is sealed.

    A file that cannot be opened is reported and skipped rather than written as
    ciphertext with a misleading name. Half a recovered flight labelled as if it
    were whole is worse than a short list and an explanation.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    result = ImportResult(output_dir=str(output))

    for index, entry in enumerate(files, start=1):
        if progress:
            progress(index, len(files), entry.name)

        target_name = entry.name[:-len(SEALED_SUFFIX)] if entry.sealed else entry.name
        target = output / target_name
        if target.exists() and not overwrite:
            result.skipped += 1
            continue

        try:
            if entry.sealed:
                if private_key is None:
                    result.failed += 1
                    result.errors.append(f"{entry.name}: sealed, and no key is loaded")
                    continue
                plaintext = open_sealed(entry.path.read_bytes(), private_key)
                target.write_bytes(plaintext)
                result.decrypted += 1
                result.bytes_written += len(plaintext)
            else:
                shutil.copy2(entry.path, target)
                result.copied += 1
                result.bytes_written += entry.size
        except SealedBoxError as exc:
            result.failed += 1
            result.errors.append(f"{entry.name}: {exc}")
        except OSError as exc:
            result.failed += 1
            result.errors.append(f"{entry.name}: {exc}")

    return result


def read_image(entry: CardFile, private_key: Optional[bytes] = None) -> Optional[bytes]:
    """One image's bytes, unsealed if necessary, for display without importing."""
    try:
        raw = entry.path.read_bytes()
    except OSError:
        return None
    if not entry.sealed:
        return raw
    if private_key is None:
        return None
    try:
        return open_sealed(raw, private_key)
    except SealedBoxError:
        return None


def read_telemetry(entry: CardFile, private_key: Optional[bytes] = None,
                   limit: int = 5000) -> List[dict]:
    """
    Parse a telemetry CSV off the card, sealed or not.

    Sealed logs are written as a stream of length-prefixed records rather than
    one blob, so that a flight cut short still yields everything written up to
    that moment. open_sealed handles both.
    """
    raw = read_image(entry, private_key)   # same unseal path
    if raw is None:
        return []
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return []
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows
