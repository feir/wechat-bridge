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
CREDENTIALS_FILE: Path = Path.home() / ".config" / "wechat-bridge" / "credentials.json"

# --- Group chat ---
# Policy: "disabled" (ignore all), "open" (respond to all), "allowlist" (only listed groups)
GROUP_POLICY: str = "disabled"
ALLOWED_GROUPS: set[str] = set()  # group IDs for allowlist mode
GROUP_REQUIRE_MENTION: bool = True  # only respond when @mentioned in group (recommended)

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
    global CREDENTIALS_FILE, GROUP_POLICY, ALLOWED_GROUPS, GROUP_REQUIRE_MENTION

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

    # Credentials file: configurable for multi-instance deployments
    creds_file = os.environ.get("WECHAT_CREDENTIALS_FILE", "").strip()
    if creds_file:
        CREDENTIALS_FILE = Path(creds_file)

    FEISHU_NOTIFY_CHAT_ID = os.environ.get("FEISHU_NOTIFY_CHAT_ID", "").strip()

    # System prompt: file takes precedence over env var
    prompt_file = os.environ.get("WECHAT_SYSTEM_PROMPT_FILE", "").strip()
    if prompt_file:
        p = Path(prompt_file)
        if p.exists():
            SYSTEM_PROMPT = p.read_text().strip()
        else:
            print(f"WARNING: WECHAT_SYSTEM_PROMPT_FILE={prompt_file} not found, using default",
                  file=sys.stderr)
            SYSTEM_PROMPT = _DEFAULT_SYSTEM_PROMPT
    else:
        SYSTEM_PROMPT = os.environ.get("WECHAT_SYSTEM_PROMPT", "").strip() or _DEFAULT_SYSTEM_PROMPT

    # Group chat config
    GROUP_POLICY = os.environ.get("WECHAT_GROUP_POLICY", "disabled").strip().lower()
    if GROUP_POLICY not in ("disabled", "open", "allowlist"):
        print(f"WARNING: Invalid WECHAT_GROUP_POLICY={GROUP_POLICY}, using 'disabled'",
              file=sys.stderr)
        GROUP_POLICY = "disabled"
    raw_groups = os.environ.get("WECHAT_ALLOWED_GROUPS", "").strip()
    ALLOWED_GROUPS = {g.strip() for g in raw_groups.split(",") if g.strip()} if raw_groups else set()
    GROUP_REQUIRE_MENTION = os.environ.get("WECHAT_GROUP_REQUIRE_MENTION", "true").strip().lower() != "false"


def is_primary(user_id: str) -> bool:
    return user_id == PRIMARY_USER
