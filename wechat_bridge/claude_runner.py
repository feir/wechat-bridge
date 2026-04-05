"""Claude CLI subprocess wrapper with streaming JSONL parsing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import AsyncIterator

from . import config

log = logging.getLogger(__name__)


class InvokeResult:
    """Result of a Claude invocation, including usage metrics."""
    __slots__ = ("text", "session_id", "input_tokens", "cache_read_tokens",
                 "cache_creation_tokens", "output_tokens", "total_cost_usd")

    def __init__(self, text: str, session_id: str,
                 input_tokens: int = 0, cache_read_tokens: int = 0,
                 cache_creation_tokens: int = 0, output_tokens: int = 0,
                 total_cost_usd: float = 0.0) -> None:
        self.text = text
        self.session_id = session_id
        self.input_tokens = input_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_creation_tokens = cache_creation_tokens
        self.output_tokens = output_tokens
        self.total_cost_usd = total_cost_usd

    @property
    def total_context_tokens(self) -> int:
        """Total input context (input + cache_read + cache_creation)."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @property
    def cache_hit_pct(self) -> float:
        """Cache read percentage of total context."""
        total = self.total_context_tokens
        return (self.cache_read_tokens / total * 100) if total > 0 else 0.0


async def invoke(
    prompt: str,
    session_id: str | None = None,
    timeout: float | None = None,
    cwd: str | None = None,
    disallowed_tools: list[str] | None = None,
    max_budget_usd: float | None = None,
) -> InvokeResult:
    """Run claude -p and return text + session_id.

    If session_id is None, starts a new session.
    If session_id is provided, resumes that session.

    Guest-user parameters:
      cwd: Working directory for the subprocess (workspace isolation).
      disallowed_tools: Tools to block (e.g. Bash, Write, Edit).
      max_budget_usd: Per-invocation cost cap (overrides global config).

    Returns InvokeResult with the response text and the actual session_id
    (which may differ from input on first call).
    """
    timeout = timeout or config.CLAUDE_TIMEOUT

    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", config.CLAUDE_MODEL,
        "--dangerously-skip-permissions",
        "--max-turns", "50",
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    if config.SYSTEM_PROMPT:
        cmd.extend(["--append-system-prompt", config.SYSTEM_PROMPT])

    # Budget: per-invocation override > global config
    budget = max_budget_usd if max_budget_usd is not None else config.MAX_BUDGET_USD
    if budget > 0:
        cmd.extend(["--max-budget-usd", str(budget)])

    # Tool restrictions for guest users
    if disallowed_tools:
        cmd.extend(["--disallowed-tools", ",".join(disallowed_tools)])

    log.info("Claude invoke: session=%s prompt=%s...",
             (session_id or "new")[:8], prompt[:40])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # isolate process group
        cwd=cwd,  # None = inherit parent's cwd; set for guest workspace isolation
    )

    pgid = os.getpgid(proc.pid)
    text_parts: list[str] = []
    actual_session_id = session_id or ""
    # Usage tracking
    usage_input = 0
    usage_cache_read = 0
    usage_cache_creation = 0
    usage_output = 0
    cost_usd = 0.0

    try:
        # Feed prompt via stdin
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        # Stream stdout JSONL
        assert proc.stdout is not None
        line_count = 0
        async for line in _read_lines_with_timeout(proc.stdout, timeout):
            line_count += 1
            log.debug("JSONL line %d: %s", line_count, line[:200])

            try:
                evt = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            # Extract session_id from any event
            sid = evt.get("session_id")
            if sid and isinstance(sid, str):
                actual_session_id = sid

            etype = evt.get("type", "")

            # Usage from assistant events (per-turn, take latest)
            if etype == "assistant":
                u = evt.get("message", {}).get("usage", {})
                if u:
                    usage_input = u.get("input_tokens", 0)
                    usage_cache_read = u.get("cache_read_input_tokens", 0)
                    usage_cache_creation = u.get("cache_creation_input_tokens", 0)
                    usage_output = u.get("output_tokens", 0)

            # Final result: text + cost
            if etype == "result":
                cost_usd = evt.get("total_cost_usd", 0.0) or 0.0
                if evt.get("is_error"):
                    errors = evt.get("errors", [])
                    error_str = "; ".join(errors) if errors else "unknown"
                    log.error("Claude error: %s", error_str)
                    # Stale session: retry without --resume
                    if session_id and "No conversation found" in error_str:
                        log.info("Stale session %s, retrying without --resume", session_id[:8])
                        return await invoke(
                            prompt, session_id=None, timeout=timeout,
                            cwd=cwd, disallowed_tools=disallowed_tools,
                            max_budget_usd=max_budget_usd,
                        )
                else:
                    text = evt.get("result", "")
                    if text:
                        text_parts.append(text)

        # Capture stderr for diagnostics
        assert proc.stderr is not None
        stderr_data = await proc.stderr.read()
        returncode = await proc.wait()

        if returncode != 0:
            stderr_str = stderr_data.decode(errors="replace").strip()
            log.warning("Claude exit=%d stderr=%s", returncode, stderr_str[:300])

    except asyncio.CancelledError:
        log.info("Claude invoke cancelled, killing pgid %d", pgid)
        _kill_pg(pgid)
        raise
    except asyncio.TimeoutError:
        log.warning("Claude timeout after %ss, killing pgid %d", timeout, pgid)
        _kill_pg(pgid)
        raise
    except Exception:
        _kill_pg(pgid)
        raise

    result_text = "".join(text_parts).strip()
    if not result_text:
        result_text = "(Claude returned empty response)"
        log.warning("Empty Claude response for session %s", actual_session_id[:8])

    total_ctx = usage_input + usage_cache_read + usage_cache_creation
    log.info("Claude done: session=%s len=%d ctx=%d (cache_hit=%.0f%%) cost=$%.4f",
             actual_session_id[:8], len(result_text), total_ctx,
             (usage_cache_read / total_ctx * 100) if total_ctx else 0,
             cost_usd)

    return InvokeResult(
        text=result_text,
        session_id=actual_session_id,
        input_tokens=usage_input,
        cache_read_tokens=usage_cache_read,
        cache_creation_tokens=usage_cache_creation,
        output_tokens=usage_output,
        total_cost_usd=cost_usd,
    )


async def _read_lines_with_timeout(
    stream: asyncio.StreamReader,
    timeout: float,
) -> AsyncIterator[bytes]:
    """Yield lines from stream with overall timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        try:
            line = await asyncio.wait_for(stream.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            raise
        if not line:
            break
        yield line



def _kill_pg(pgid: int) -> None:
    """Kill entire process group."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass


async def kill_process(proc: asyncio.subprocess.Process, timeout: float = 10) -> None:
    """Gracefully kill a Claude subprocess."""
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            os.killpg(pgid, signal.SIGKILL)
            await proc.wait()
    except OSError:
        pass
