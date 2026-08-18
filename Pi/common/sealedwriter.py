"""
Write files sealed to a public key, when one is configured.

A thin front end over `sealedbox` that the camera and the telemetry logger
share, so encryption behaves the same in both and neither has to think about
it.

Two design choices worth stating:

  - When no key is configured, files are written in the clear exactly as
    before. Encryption is opt-in, and turning it on must not be the reason a
    flight fails.

  - A sealing failure never loses data. If encryption raises, the plaintext is
    written instead and the failure is logged loudly. Losing a flight's
    imagery to a crypto bug would be a far worse outcome than storing it
    unencrypted.

Sealed files take a `.rhs` suffix so they are obvious on the card and cannot
be mistaken for readable ones.
"""

import logging
import os
from typing import Optional

from common.sealedbox import format_key, key_fingerprint, parse_public_key, seal

logger = logging.getLogger(__name__)

SEALED_SUFFIX = ".rhs"


class SealedWriter:
    """Writes plaintext or sealed files depending on configuration."""

    def __init__(self, public_key_text: str = "", enabled: bool = True):
        """
        Args:
            public_key_text: Recipient public key, base64 or hex. Empty
                disables encryption.
            enabled: Master switch, so encryption can be turned off without
                discarding the configured key.
        """
        self._key: Optional[bytes] = None
        self._enabled = enabled

        if not enabled:
            return

        try:
            self._key = parse_public_key(public_key_text)
        except ValueError as e:
            # A malformed key must not silently mean "no encryption": the
            # operator asked for it and would otherwise never find out.
            logger.error(
                f"Recording encryption key is invalid ({e}). Files will be "
                f"written UNENCRYPTED. Fix recording_public_key or clear it."
            )
            self._key = None

        if self._key:
            logger.info(
                f"Recording encryption enabled; sealing to key "
                f"{key_fingerprint(self._key)}. The payload cannot read these "
                f"files back."
            )

    @property
    def active(self) -> bool:
        return self._key is not None

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self._key) if self._key else "none"

    def path_for(self, path: str) -> str:
        """The name this writer would actually use."""
        return path + SEALED_SUFFIX if self.active else path

    def write(self, path: str, data: bytes) -> str:
        """
        Write `data`, sealed if a key is configured.

        Returns the path actually written, which gains `.rhs` when sealed.
        """
        if not self.active:
            with open(path, "wb") as f:
                f.write(data)
            return path

        target = path + SEALED_SUFFIX
        try:
            payload = seal(data, self._key)
        except Exception as e:
            # Never lose the data over an encryption failure.
            logger.error(
                f"Could not seal {os.path.basename(path)} ({e}); writing it "
                f"unencrypted so the data is not lost"
            )
            with open(path, "wb") as f:
                f.write(data)
            return path

        with open(target, "wb") as f:
            f.write(payload)
        return target

    def append_line(self, path: str, line: str) -> str:
        """
        Append one line to a text log.

        Sealed logs are written as a sequence of independently sealed records
        rather than one growing box, because a balloon can lose power at any
        moment and a single box truncated mid-flight would be undecryptable in
        its entirety. Each record carries its own length prefix so the reader
        can walk them.
        """
        if not self.active:
            with open(path, "a") as f:
                f.write(line)
            return path

        target = path + SEALED_SUFFIX
        record = seal(line.encode("utf-8"), self._key)
        with open(target, "ab") as f:
            f.write(len(record).to_bytes(4, "big"))
            f.write(record)
        return target
