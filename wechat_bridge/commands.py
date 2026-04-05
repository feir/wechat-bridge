"""Bridge-level command detection and handling.

Commands are intercepted before reaching Claude CLI:
- /stop, /new: handled at dispatch level (immediate, bypasses queue)
- /compact, /status, /help: handled in worker (serialized with messages)
"""

from __future__ import annotations

import asyncio
import logging

from . import claude_runner, config
from .claude_runner import InvokeResult

log = logging.getLogger(__name__)

# --- Command detection ---

_COMMANDS = frozenset({"/new", "/stop", "/compact", "/status", "/help"})


def parse_command(text: str) -> tuple[str, str] | None:
    """Parse bridge command. Returns (cmd, arg) or None if not a command."""
    parts = text.strip().split(None, 1)
    if not parts:
        return None
    cmd = parts[0].lower()
    if cmd not in _COMMANDS:
        return None
    arg = parts[1] if len(parts) > 1 else ""
    return cmd, arg


# --- Command responses ---

def format_help() -> str:
    return (
        "可用命令:\n"
        "/new — 重置会话（开始新对话）\n"
        "/stop — 停止当前任务\n"
        "/compact — 压缩上下文\n"
        "/status — 查看会话状态\n"
        "/help — 显示此帮助"
    )


def format_status(
    last_result: InvokeResult | None,
    session_id: str | None,
    model: str,
) -> str:
    """Build plain-text status from last invocation result."""
    if not last_result or not session_id:
        return f"当前无活跃会话\n模型: {model}"

    max_ctx = _context_window_for_model(model)
    total_ctx = last_result.total_context_tokens
    pct = total_ctx / max_ctx * 100 if max_ctx else 0

    # Text-based progress bar (no card support in WeChat)
    filled = int(pct / 5)
    bar = "█" * filled + "░" * (20 - filled)

    lines = [
        f"Context [{bar}] {pct:.0f}%",
        f"{total_ctx:,} / {max_ctx:,} tokens",
    ]

    if last_result.cache_hit_pct > 0:
        lines.append(
            f"Cache hit: {last_result.cache_read_tokens:,} "
            f"({last_result.cache_hit_pct:.0f}%)"
        )

    lines.append(f"模型: {model}")
    lines.append(f"会话: {session_id[:8]}...")

    if last_result.total_cost_usd > 0:
        lines.append(f"本次费用: ${last_result.total_cost_usd:.4f}")

    # Context warning
    if pct >= 85:
        lines.append("\n⚠ Context 接近上限，建议 /new 或 /compact")
    elif pct >= 70:
        lines.append("\n💡 Context 较高，可考虑 /compact 压缩")

    return "\n".join(lines)


async def run_compact(session_id: str, timeout: float = 120) -> str:
    """Execute /compact on a session. Returns reply text."""
    try:
        result = await claude_runner.invoke(
            "/compact", session_id=session_id, timeout=timeout,
        )
        max_ctx = _context_window_for_model(config.CLAUDE_MODEL)
        total_ctx = result.total_context_tokens
        pct = total_ctx / max_ctx * 100 if max_ctx else 0
        return (
            f"上下文已压缩\n"
            f"Context: {total_ctx:,} tokens ({pct:.0f}%)\n"
            f"费用: ${result.total_cost_usd:.4f}"
        )
    except asyncio.TimeoutError:
        return "压缩超时，请稍后重试"
    except Exception as e:
        log.error("Compact failed: %s", e)
        return f"压缩失败: {e}"


# --- Context hint (appended to normal replies) ---

def context_hint(result: InvokeResult, model: str) -> str | None:
    """Build context usage hint for reply suffix. Returns None if under threshold."""
    max_ctx = _context_window_for_model(model)
    total_ctx = result.total_context_tokens
    if max_ctx == 0 or total_ctx == 0:
        return None

    pct = total_ctx / max_ctx * 100

    if pct >= 85:
        return f"---\n⚠ Context {pct:.0f}% — 建议 /new 新会话或 /compact 压缩"
    if pct >= 70:
        return f"---\n💡 Context {pct:.0f}% — 可考虑 /compact 压缩上下文"
    return None


# --- Model context windows ---

_CONTEXT_WINDOWS: dict[str, int] = {
    "opus": 200_000,
    "sonnet": 200_000,
    "haiku": 200_000,
}
_DEFAULT_CONTEXT_WINDOW = 200_000


def _context_window_for_model(model: str) -> int:
    model_lower = model.lower()
    for key, window in _CONTEXT_WINDOWS.items():
        if key in model_lower:
            return window
    return _DEFAULT_CONTEXT_WINDOW
