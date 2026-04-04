"""Session management: MessageDedup, ContextTokenStore, SessionMap.

All three share the same persistence directory (config.STATE_DIR).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger(__name__)


# --- 2.1 Message Dedup ---

class MessageDedup:
    """LRU + TTL dedup for message_id. Not persisted (buf cursor handles restarts)."""

    def __init__(self, capacity: int = 5000, ttl_s: float = 43200) -> None:
        self._seen: OrderedDict[int, float] = OrderedDict()
        self._capacity = capacity
        self._ttl = ttl_s

    def is_duplicate(self, message_id: int) -> bool:
        """Return True if already seen (and not expired). Marks as seen on first call."""
        now = time.monotonic()

        # Evict expired tail entries lazily
        while self._seen and next(iter(self._seen.values())) < now - self._ttl:
            self._seen.popitem(last=False)

        if message_id in self._seen:
            self._seen.move_to_end(message_id)
            return True

        # New entry
        self._seen[message_id] = now
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return False


# --- 2.2 Context Token Store ---

class ContextTokenStore:
    """Per-user context_token cache with disk persistence.

    iLink requires echoing the latest context_token in every reply.
    Updated on every incoming message.
    """

    def __init__(self, state_dir: Path) -> None:
        self._tokens: dict[str, str] = {}
        self._path = state_dir / "context_tokens.json"
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._tokens = json.loads(self._path.read_text())
                log.info("Loaded %d context tokens", len(self._tokens))
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Failed to load context tokens: %s", e)

    def flush(self) -> None:
        """Persist to disk."""
        try:
            self._path.write_text(json.dumps(self._tokens))
        except OSError as e:
            log.error("Failed to save context tokens: %s", e)

    def update(self, user_id: str, token: str) -> None:
        """Update context token for a user (called on every incoming message)."""
        self._tokens[user_id] = token

    def get(self, user_id: str) -> str | None:
        return self._tokens.get(user_id)


# --- 2.4 Session Map ---

class SessionMap:
    """Per-user Claude session_id persistence.

    Maps user_id → session_id so multi-turn conversations resume correctly.
    """

    def __init__(self, state_dir: Path) -> None:
        self._sessions: dict[str, str] = {}
        self._path = state_dir / "sessions.json"
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._sessions = json.loads(self._path.read_text())
                log.info("Loaded %d sessions", len(self._sessions))
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Failed to load sessions: %s", e)

    def flush(self) -> None:
        """Persist to disk."""
        try:
            self._path.write_text(json.dumps(self._sessions, indent=2))
        except OSError as e:
            log.error("Failed to save sessions: %s", e)

    def get(self, user_id: str) -> str | None:
        """Get existing session_id, or None for new users."""
        return self._sessions.get(user_id)

    def set(self, user_id: str, session_id: str) -> None:
        """Store session_id (from Claude response)."""
        self._sessions[user_id] = session_id
        log.info("Session stored: user=%s session=%s", user_id[:16], session_id[:8])

    def reset(self, user_id: str) -> None:
        """Force new session (e.g. user sends /new)."""
        self._sessions.pop(user_id, None)
