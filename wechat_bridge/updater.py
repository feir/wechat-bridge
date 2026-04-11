"""Background update checker for wechat-bridge.

Detects new versions (git or PyPI), pulls updates without restarting,
and exposes pending_version for reply suffix notification.
"""

import importlib.metadata
import json
import logging
import subprocess
import threading
import time
from typing import Optional
from urllib.request import urlopen

log = logging.getLogger(__name__)

_PACKAGE = "wechat-bridge"
_PACKAGE_MODULE = "wechat_bridge"

# Module-level singleton
_updater: Optional["UpdateChecker"] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_install_info() -> tuple[str, str | None]:
    """Detect installation mode.

    Returns (mode, source_path):
        mode: "pypi" or "git"
        source_path: local source directory (git mode only)
    """
    try:
        dist = importlib.metadata.distribution(_PACKAGE)
        raw = dist.read_text("direct_url.json")
        if raw:
            info = json.loads(raw)
            if info.get("dir_info", {}).get("editable"):
                url = info.get("url", "")
                path = url.removeprefix("file://") if url.startswith("file://") else url
                return "git", path
    except Exception:
        pass
    return "pypi", None


def init_updater(mode: str, source_path: Optional[str] = None,
                 check_interval: int = 6 * 3600):
    """Initialize the global UpdateChecker and start its background thread."""
    global _updater
    _updater = UpdateChecker(mode, source_path, check_interval)
    _updater.start()
    log.info("Update checker started (mode=%s, interval=%ds)", mode, check_interval)


def get_pending_version() -> Optional[str]:
    """Return the pending version string if an update is ready, else None."""
    if _updater is None:
        return None
    return _updater.pending_version


def get_update_banner() -> Optional[str]:
    """Return plain-text update banner for reply suffix, or None."""
    pv = get_pending_version()
    if not pv:
        return None
    return f"⬆ v{pv} 已就绪，/restart 部署"


def check_and_update() -> dict:
    """Manually trigger a check+update cycle.

    Returns:
        {"status": "updated", "version": "..."} |
        {"status": "up_to_date", "version": "..."} |
        {"status": "error", "message": "..."}
    """
    if _updater is None:
        return {"status": "error", "message": "Update checker not initialized"}
    return _updater.check_and_update()


# ---------------------------------------------------------------------------
# CalVer comparison
# ---------------------------------------------------------------------------


def _parse_calver(ver: str) -> tuple[int, ...]:
    """Parse CalVer string like '2026.4.5' into comparable tuple."""
    return tuple(int(x) for x in ver.split("."))


# ---------------------------------------------------------------------------
# UpdateChecker
# ---------------------------------------------------------------------------


class UpdateChecker:
    """Background thread that checks for and pulls wechat-bridge updates."""

    def __init__(self, mode: str, source_path: Optional[str],
                 check_interval: int):
        self.mode = mode  # "git" or "pypi"
        self.source_path = source_path
        self.check_interval = check_interval
        self.pending_version: Optional[str] = None
        self._lock = threading.Lock()

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True,
                             name="update-checker")
        t.start()

    def _loop(self):
        time.sleep(5)  # Let main init finish
        self._safe_check()
        while True:
            time.sleep(self.check_interval)
            self._safe_check()

    def _safe_check(self):
        try:
            self.check_and_update()
        except Exception:
            log.exception("Update check failed")

    def check_and_update(self) -> dict:
        """Detect new version, pull if available, set pending_version."""
        if self.pending_version is not None:
            return {"status": "updated", "version": self.pending_version}

        from wechat_bridge import __version__

        if self.mode == "git":
            return self._check_git(__version__)
        else:
            return self._check_pypi(__version__)

    # -- git mode ----------------------------------------------------------

    def _check_git(self, current_version: str) -> dict:
        """Fetch origin, compare, pull if ahead."""
        if not self.source_path:
            return {"status": "error", "message": "No source path for git mode"}

        r = subprocess.run(
            ["git", "fetch", "--quiet"],
            cwd=self.source_path, capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            msg = r.stderr.decode(errors="replace").strip()
            log.warning("git fetch failed: %s", msg)
            return {"status": "error", "message": f"git fetch failed: {msg}"}

        # Check how many commits ahead upstream is
        # Try @{upstream} → origin/HEAD → origin/main as fallbacks
        r = None
        for ref in ("@{upstream}", "origin/HEAD", "origin/main"):
            r = subprocess.run(
                ["git", "rev-list", f"HEAD..{ref}", "--count"],
                cwd=self.source_path, capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                break
        if r is None or r.returncode != 0:
            return {"status": "error", "message": "git rev-list failed"}

        ahead = int(r.stdout.decode().strip())
        if ahead == 0:
            log.info("Update check: up to date (git, v%s)", current_version)
            return {"status": "up_to_date", "version": current_version}

        log.info("Update check: %d new commit(s) available, pulling...", ahead)

        r = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=self.source_path, capture_output=True, timeout=60,
        )
        if r.returncode != 0:
            msg = r.stderr.decode(errors="replace").strip()
            log.warning("git pull --ff-only failed: %s", msg)
            return {"status": "error", "message": f"git pull failed: {msg}"}

        new_version = self._read_git_version()
        if new_version and new_version != current_version:
            with self._lock:
                self.pending_version = new_version
            log.info("Update pulled: v%s → v%s (pending restart)",
                     current_version, new_version)
            return {"status": "updated", "version": new_version}

        # Commits pulled but version unchanged
        with self._lock:
            self.pending_version = f"{current_version}+{ahead}"
        log.info("Update pulled: %d commit(s), version unchanged v%s",
                 ahead, current_version)
        return {"status": "updated", "version": self.pending_version}

    def _read_git_version(self) -> Optional[str]:
        """Read __version__ from the on-disk __init__.py."""
        try:
            init_path = f"{self.source_path}/{_PACKAGE_MODULE}/__init__.py"
            with open(init_path) as f:
                for line in f:
                    if line.startswith("__version__"):
                        return line.split('"')[1]
        except Exception:
            log.warning("Failed to read version from disk", exc_info=True)
        return None

    # -- pypi mode ---------------------------------------------------------

    def _check_pypi(self, current_version: str) -> dict:
        """Query PyPI for latest version, upgrade via pipx if newer."""
        try:
            with urlopen(f"https://pypi.org/pypi/{_PACKAGE}/json", timeout=10) as resp:
                data = json.loads(resp.read().decode())
            latest = data["info"]["version"]
        except Exception as e:
            log.warning("PyPI version check failed: %s", e)
            return {"status": "error", "message": f"PyPI check failed: {e}"}

        if _parse_calver(latest) <= _parse_calver(current_version):
            log.info("Update check: up to date (pypi, v%s)", current_version)
            return {"status": "up_to_date", "version": current_version}

        log.info("Update check: v%s available (current: v%s), upgrading...",
                 latest, current_version)

        r = subprocess.run(
            ["pipx", "upgrade", _PACKAGE],
            capture_output=True, timeout=120,
        )
        if r.returncode != 0:
            msg = (r.stderr.decode(errors="replace").strip()
                   or r.stdout.decode(errors="replace").strip())
            log.warning("pipx upgrade failed: %s", msg)
            return {"status": "error", "message": f"pipx upgrade failed: {msg}"}

        with self._lock:
            self.pending_version = latest
        log.info("Update installed: v%s → v%s (pending restart)",
                 current_version, latest)
        return {"status": "updated", "version": latest}
