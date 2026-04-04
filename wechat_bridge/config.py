"""Configuration from environment variables."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"FATAL: {name} is required but not set", file=sys.stderr)
        sys.exit(1)
    return val


# --- Required ---
ALLOWED_USERS: set[str] = set()  # populated by init()
PRIMARY_USER: str = ""  # primary user gets full permissions, no workspace isolation

# --- Optional with defaults ---
CLAUDE_MODEL: str = ""
CLAUDE_TIMEOUT: int = 300
MAX_CONCURRENT: int = 3
MAX_BUDGET_USD: float = 0.0
GUEST_MAX_BUDGET_USD: float = 1.0  # per-invocation cost cap for non-primary users
STATE_DIR: Path = Path.home() / ".local" / "share" / "wechat-bridge"
FEISHU_NOTIFY_CHAT_ID: str = ""
SYSTEM_PROMPT: str = ""

# Tools blocked for non-primary users (filesystem write + shell access)
GUEST_DISALLOWED_TOOLS: list[str] = [
    "Bash", "Write", "Edit", "NotebookEdit",
]

_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant in a WeChat conversation. "
    "Keep replies concise — WeChat displays poorly for very long messages. "
    "Use short paragraphs. Avoid unnecessary headers or formatting. "
    "Reply in the same language the user uses."
)


def init() -> None:
    """Load config from environment. Call once at startup."""
    global ALLOWED_USERS, PRIMARY_USER, CLAUDE_MODEL, CLAUDE_TIMEOUT, MAX_CONCURRENT
    global STATE_DIR, FEISHU_NOTIFY_CHAT_ID, SYSTEM_PROMPT, MAX_BUDGET_USD, GUEST_MAX_BUDGET_USD

    raw = _require("WECHAT_ALLOWED_USERS")
    _users_ordered = [u.strip() for u in raw.split(",") if u.strip()]
    ALLOWED_USERS = set(_users_ordered)
    if not ALLOWED_USERS:
        print("FATAL: WECHAT_ALLOWED_USERS is empty after parsing", file=sys.stderr)
        sys.exit(1)

    # Primary user: explicit env var, or first in the env var ordering (stable)
    PRIMARY_USER = os.environ.get("WECHAT_PRIMARY_USER", "").strip()
    if not PRIMARY_USER:
        PRIMARY_USER = _users_ordered[0]

    CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet").strip()
    CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
    MAX_CONCURRENT = int(os.environ.get("WECHAT_MAX_CONCURRENT", "3"))
    MAX_BUDGET_USD = float(os.environ.get("CLAUDE_MAX_BUDGET_USD", "0"))
    GUEST_MAX_BUDGET_USD = float(os.environ.get("CLAUDE_GUEST_MAX_BUDGET_USD", "1.0"))

    state_dir = os.environ.get("WECHAT_STATE_DIR", "").strip()
    STATE_DIR = Path(state_dir) if state_dir else Path.home() / ".local" / "share" / "wechat-bridge"
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    FEISHU_NOTIFY_CHAT_ID = os.environ.get("FEISHU_NOTIFY_CHAT_ID", "").strip()
    SYSTEM_PROMPT = os.environ.get("WECHAT_SYSTEM_PROMPT", "").strip() or _DEFAULT_SYSTEM_PROMPT


def is_primary(user_id: str) -> bool:
    return user_id == PRIMARY_USER
