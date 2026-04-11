"""File-based lock to prevent multiple bridge instances using the same token.

Uses fcntl.flock (advisory lock) — if the process dies, the OS releases the lock.
"""

from __future__ import annotations

import fcntl
import logging
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)


class BridgeLock:
    """Exclusive lock on a file in state_dir. Context-manager friendly."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "bridge.lock"
        self._fd: IO[str] | None = None

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True on success, False if held by another process."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = open(self._path, "w")  # noqa: SIM115
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(str(__import__("os").getpid()))
            self._fd.flush()
            log.info("Bridge lock acquired: %s", self._path)
            return True
        except OSError:
            log.error(
                "Another bridge instance is running (lock held: %s). "
                "If this is wrong, delete the lock file and retry.",
                self._path,
            )
            if self._fd:
                self._fd.close()
                self._fd = None
            return False

    def release(self) -> None:
        """Release the lock."""
        if self._fd:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except OSError:
                pass
            self._fd = None
            log.info("Bridge lock released")
