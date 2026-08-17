"""
RaptorHab Configuration Store

JSON-backed, schema-versioned persistent configuration with atomic writes.

Design constraints (this is flight software):
  - A corrupt or unreadable config file must NEVER prevent the payload from
    starting. It falls back to defaults, loudly, and preserves the bad file
    for post-flight analysis.
  - A write interrupted by power loss must never leave a half-written file.
    Writes go to a temp file in the same directory, are fsync'd, then
    atomically renamed over the target.
  - Unknown keys from a newer schema version are preserved on read/modify/write
    so that downgrading firmware does not silently discard settings.
"""

import json
import logging
import os
import shutil
import tempfile
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Bumped whenever the on-disk layout changes in a way that needs migration.
CURRENT_SCHEMA_VERSION = 1

_VERSION_KEY = "_schema_version"
_SAVED_AT_KEY = "_saved_at"


class ConfigStore:
    """Atomic, versioned JSON configuration file."""

    def __init__(
        self,
        path: str,
        schema_version: int = CURRENT_SCHEMA_VERSION,
        migrator: Optional[Callable[[Dict[str, Any], int], Dict[str, Any]]] = None,
    ):
        """
        Args:
            path: Absolute path to the JSON config file.
            schema_version: Schema version this build expects.
            migrator: Optional ``fn(data, from_version) -> data`` used to
                upgrade an older on-disk layout.
        """
        self.path = path
        self.schema_version = schema_version
        self._migrator = migrator
        self._last_load_failed = False
        self._last_error: Optional[str] = None

    # --- reading -----------------------------------------------------------

    @property
    def exists(self) -> bool:
        return os.path.isfile(self.path)

    @property
    def last_load_failed(self) -> bool:
        """True if the most recent load() fell back to defaults."""
        return self._last_load_failed

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def load(self) -> Dict[str, Any]:
        """
        Load configuration values.

        Returns an empty dict (meaning "use defaults") if the file is missing,
        unreadable, malformed, or not a JSON object. Never raises.
        """
        self._last_load_failed = False
        self._last_error = None

        if not self.exists:
            logger.info(f"No config file at {self.path}; using defaults")
            return {}

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except OSError as e:
            # An I/O or permission error says nothing about the file's
            # contents, so it must NOT be quarantined -- a transient problem
            # would otherwise destroy a perfectly good configuration.
            self._last_load_failed = True
            self._last_error = str(e)
            logger.error(
                f"CONFIG UNREADABLE: {self.path}: {e}. "
                f"Falling back to built-in defaults; file left in place."
            )
            return {}
        except (ValueError, UnicodeDecodeError) as e:
            self._last_load_failed = True
            self._last_error = str(e)
            logger.error(
                f"CONFIG CORRUPT: {self.path} is not valid JSON: {e}. "
                f"Falling back to built-in defaults."
            )
            self._quarantine()
            return {}

        if not isinstance(data, dict):
            self._last_load_failed = True
            self._last_error = f"top-level JSON is {type(data).__name__}, expected object"
            logger.error(
                f"CONFIG CORRUPT: {self.path} does not contain a JSON object. "
                f"Falling back to built-in defaults."
            )
            self._quarantine()
            return {}

        on_disk_version = data.get(_VERSION_KEY, 0)
        if not isinstance(on_disk_version, int):
            on_disk_version = 0

        if on_disk_version > self.schema_version:
            logger.warning(
                f"Config schema v{on_disk_version} is newer than this build "
                f"(v{self.schema_version}); unknown keys will be preserved but ignored"
            )
        elif on_disk_version < self.schema_version and self._migrator is not None:
            try:
                data = self._migrator(data, on_disk_version)
                logger.info(
                    f"Migrated config from schema v{on_disk_version} to v{self.schema_version}"
                )
            except Exception as e:
                self._last_load_failed = True
                self._last_error = f"migration failed: {e}"
                logger.error(
                    f"CONFIG MIGRATION FAILED (v{on_disk_version} -> "
                    f"v{self.schema_version}): {e}. Falling back to defaults."
                )
                self._quarantine()
                return {}

        # Strip bookkeeping keys before handing values back.
        return {k: v for k, v in data.items() if not k.startswith("_")}

    # --- writing -----------------------------------------------------------

    def save(self, values: Dict[str, Any]) -> bool:
        """
        Write configuration atomically. Returns True on success.

        Preserves any unknown keys already present in the file so that a
        firmware downgrade does not discard settings it doesn't understand.
        """
        merged: Dict[str, Any] = {}

        # Preserve unknown keys from an existing readable file.
        if self.exists:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    merged.update(
                        {
                            k: v
                            for k, v in existing.items()
                            if not k.startswith("_") and k not in values
                        }
                    )
            except (OSError, ValueError, UnicodeDecodeError):
                # Unreadable existing file: nothing to preserve, just overwrite.
                pass

        merged.update(values)
        merged[_VERSION_KEY] = self.schema_version
        merged[_SAVED_AT_KEY] = int(time.time())

        try:
            # No `default=` handler on purpose: silently stringifying a value
            # the caller did not intend to be a string would round-trip back
            # as garbage. Refusing the write is the safer failure.
            payload = json.dumps(merged, indent=2, sort_keys=True)
        except (TypeError, ValueError) as e:
            logger.error(f"Config not JSON-serializable, refusing to save: {e}")
            return False

        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            logger.error(f"Cannot create config directory {directory}: {e}")
            return False

        tmp_fd = None
        tmp_path = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=".raptorhab-cfg-", dir=directory, text=True
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                tmp_fd = None  # now owned by the file object
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())

            # 0600: config may hold channel pre-shared keys.
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
            tmp_path = None

            # fsync the directory so the rename itself survives power loss.
            self._fsync_dir(directory)

            logger.info(f"Config saved to {self.path}")
            return True

        except OSError as e:
            logger.error(f"Failed to save config to {self.path}: {e}")
            return False
        finally:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _fsync_dir(directory: str) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _quarantine(self) -> None:
        """Move a bad config file aside so the next save starts clean."""
        if not self.exists:
            return
        target = f"{self.path}.corrupt.{int(time.time())}"
        try:
            shutil.move(self.path, target)
            logger.error(f"Corrupt config preserved at {target}")
        except OSError as e:
            logger.error(f"Could not quarantine corrupt config: {e}")
