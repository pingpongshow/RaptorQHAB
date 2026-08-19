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
import time
from typing import Optional

from common.sealedbox import format_key, key_fingerprint, parse_public_key, seal

logger = logging.getLogger(__name__)

SEALED_SUFFIX = ".rhs"

# How often an appended log is forced out to the card. Every line would be
# honest but punishing: a 1 Hz telemetry log would mean 3600 fsyncs an hour on
# an SD card, each one a full erase block. Ten seconds bounds the loss to ten
# rows while leaving the card alone the rest of the time.
DEFAULT_SYNC_INTERVAL_SEC = 10.0


class SealedWriter:
    """Writes plaintext or sealed files depending on configuration."""

    def __init__(
        self,
        public_key_text: str = "",
        enabled: bool = True,
        sync_interval_sec: float = DEFAULT_SYNC_INTERVAL_SEC,
    ):
        """
        Args:
            public_key_text: Recipient public key, base64 or hex. Empty
                disables encryption.
            enabled: Master switch, so encryption can be turned off without
                discarding the configured key.
            sync_interval_sec: How often appended logs are forced to the card.
                Zero syncs every line.
        """
        self._key: Optional[bytes] = None
        self._enabled = enabled
        self._sync_interval_sec = max(0.0, sync_interval_sec)
        self._last_sync = 0.0

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

    @staticmethod
    def _write_atomic(path: str, data: bytes) -> None:
        """
        Write a whole file so it is either complete on the card or absent.

        A balloon loses power without warning, and the card is often pulled
        straight out of a payload that was still running. A plain open-and-write
        can leave a half-written file, which for a sealed recording is not
        "most of an image" but nothing at all -- the authentication tag is at
        the end, so a truncated box fails to open.

        Temp file, fsync, rename, fsync the directory. The rename is atomic, so
        a reader sees the old file or the new one and never a partial one.
        """
        tmp = f"{path}.part"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

        # The rename itself needs flushing, or the directory entry can be lost
        # even though the data blocks made it.
        dirfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)

    def write(self, path: str, data: bytes) -> str:
        """
        Write `data`, sealed if a key is configured.

        Returns the path actually written, which gains `.rhs` when sealed.
        """
        if not self.active:
            self._write_atomic(path, data)
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
            self._write_atomic(path, data)
            return path

        self._write_atomic(target, payload)
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
                self._maybe_sync(f)
            return path

        target = path + SEALED_SUFFIX
        try:
            record = seal(line.encode("utf-8"), self._key)
        except Exception as e:
            # The same promise write() makes, which this method was quietly
            # not keeping: a crypto failure must not cost the data. Falls back
            # to the plaintext log rather than raising into the caller, which
            # here is a GPS callback.
            logger.error(
                f"Could not seal a log line for {os.path.basename(path)} "
                f"({e}); appending it unencrypted so the row is not lost"
            )
            with open(path, "a") as f:
                f.write(line)
                self._maybe_sync(f)
            return path

        with open(target, "ab") as f:
            f.write(len(record).to_bytes(4, "big"))
            f.write(record)
            self._maybe_sync(f)
        return target

    def _maybe_sync(self, f) -> None:
        """
        Force the log to the card, at most every sync_interval_sec.

        Without this the whole point of writing each row as its own sealed
        record is lost: the records are correct but they are sitting in the
        page cache, and a payload that loses power keeps none of them.
        """
        now = time.monotonic()
        if self._sync_interval_sec and now - self._last_sync < self._sync_interval_sec:
            return
        try:
            f.flush()
            os.fsync(f.fileno())
            self._last_sync = now
        except OSError as e:
            logger.warning(f"Could not flush log to disk: {e}")
