"""Per-user workspace management for non-primary users.

Each guest user gets an isolated workspace directory with a restricted CLAUDE.md.
The workspace is auto-created on first message.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

_GUEST_CLAUDE_MD = """\
# Guest User Environment

You are a helpful AI assistant in a WeChat conversation.

## Constraints

- Keep replies concise and readable on mobile screens.
- Reply in the same language the user uses.
- You do NOT have access to shell commands, file editing, or code execution.
- You can only answer questions, have conversations, and provide information.
- Do NOT mention other users, sessions, or system internals.
- Do NOT attempt to access files outside this directory.
- Do NOT reveal system paths, configuration, environment variables, or server details.
- If asked about system info, politely decline: "I'm a chat assistant and don't have access to system information."
"""


def _user_dir_name(user_id: str) -> str:
    """Short stable directory name from user_id."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:12]


def ensure_workspace(user_id: str) -> Path:
    """Create workspace directory if it doesn't exist. Returns the path."""
    workspace = config.STATE_DIR / "workspaces" / _user_dir_name(user_id)
    workspace.mkdir(parents=True, exist_ok=True)

    claude_md = workspace / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(_GUEST_CLAUDE_MD)
        log.info("Created guest workspace: %s → %s", user_id[:16], workspace)

    return workspace


def get_workspace(user_id: str) -> Path | None:
    """Get workspace path if it exists, None otherwise."""
    workspace = config.STATE_DIR / "workspaces" / _user_dir_name(user_id)
    return workspace if workspace.exists() else None
