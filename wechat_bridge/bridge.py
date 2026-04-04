"""Main bridge loop: poll → process → reply.

Concurrency model:
- Single getupdates poller (main loop)
- Per-user asyncio task with internal FIFO queue
- Global semaphore limits concurrent Claude subprocesses
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import signal
import sys
from typing import Any

import aiohttp

from . import config, ilink_api, claude_runner, workspace
from .chunk import chunk_text
from .ilink_api import ApiError
from .ilink_types import WeixinMessage
from .session import ContextTokenStore, MessageDedup, SessionMap

log = logging.getLogger(__name__)

# --- Globals (initialized in run_bridge) ---
_shutdown = False
_http: aiohttp.ClientSession | None = None
_dedup: MessageDedup
_ctx_store: ContextTokenStore
_session_map: SessionMap
_semaphore: asyncio.Semaphore
_user_queues: dict[str, asyncio.Queue[WeixinMessage]] = {}
_user_tasks: dict[str, asyncio.Task[None]] = {}

# Idle auto-compact: compact session while prompt cache is still warm
_IDLE_COMPACT_DELAY = 50 * 60   # 50 min (10 min buffer before 1h cache TTL)
_IDLE_COMPACT_MIN_CTX = 50_000  # Only compact sessions > 50K context tokens
_compact_timers: dict[str, asyncio.TimerHandle] = {}  # user_id → timer handle


# --- Idle auto-compact ---

def _schedule_compact(user_id: str, session_id: str, total_ctx: int) -> None:
    """Schedule a proactive /compact for a session going idle.

    If the session has >50K context tokens, schedule a compact after 50min
    so it runs while prompt cache is still warm (cache_read pricing = 10x cheaper).
    """
    # Cancel existing timer
    old = _compact_timers.pop(user_id, None)
    if old:
        old.cancel()

    if total_ctx < _IDLE_COMPACT_MIN_CTX:
        return

    loop = asyncio.get_running_loop()
    handle = loop.call_later(
        _IDLE_COMPACT_DELAY,
        lambda: asyncio.ensure_future(_run_compact(user_id, session_id, total_ctx)),
    )
    _compact_timers[user_id] = handle
    log.debug("Compact scheduled: user=%s ctx=%d in %.0fmin",
              user_id[:16], total_ctx, _IDLE_COMPACT_DELAY / 60)


async def _run_compact(user_id: str, session_id: str, ctx_before: int) -> None:
    """Execute /compact on an idle session."""
    _compact_timers.pop(user_id, None)

    if _shutdown:
        return

    log.info("Idle auto-compact firing: user=%s session=%s ctx=%d",
             user_id[:16], session_id[:8], ctx_before)

    try:
        result = await claude_runner.invoke(
            "/compact", session_id=session_id, timeout=60,
        )
        log.info("Compact done: user=%s ctx_before=%d cost=$%.4f",
                 user_id[:16], ctx_before, result.total_cost_usd)
    except Exception as e:
        log.warning("Compact failed: user=%s %s", user_id[:16], e)


# --- Typing helper ---

async def _typing_refresh(
    user_id: str, ticket: str, token: str, base_url: str,
) -> None:
    """Send typing(1) every 5s until cancelled."""
    assert _http is not None
    while True:
        try:
            await ilink_api.send_typing(_http, base_url, token, user_id, ticket, 1)
        except Exception as e:
            log.debug("typing refresh failed: %s", e)
        await asyncio.sleep(5)


async def _stop_typing(
    user_id: str, ticket: str, token: str, base_url: str,
) -> None:
    """Best-effort stop typing."""
    assert _http is not None
    try:
        await ilink_api.send_typing(_http, base_url, token, user_id, ticket, 2)
    except Exception:
        pass


# --- Per-user worker ---

async def _user_worker(user_id: str, token: str, base_url: str) -> None:
    """Process messages for a single user sequentially."""
    queue = _user_queues[user_id]

    while not _shutdown:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=300)  # 5min idle
        except asyncio.TimeoutError:
            log.info("User worker idle timeout: %s", user_id[:16])
            break

        await _process_message(msg, token, base_url)
        queue.task_done()

    # Cleanup
    _user_tasks.pop(user_id, None)
    _user_queues.pop(user_id, None)
    log.info("User worker exited: %s", user_id[:16])


async def _process_message(msg: WeixinMessage, token: str, base_url: str) -> None:
    """Process a single incoming message: permission → typing → Claude → reply."""
    assert _http is not None

    user_id = msg["from_user_id"]
    context_token = msg["context_token"]

    # Extract text
    text = ""
    for item in msg.get("item_list", []):
        if item.get("type") == 1 and "text_item" in item:
            text = item["text_item"]["text"]
            break

    if not text:
        log.info("Non-text message from %s, skipping", user_id[:16])
        return

    log.info("Processing: user=%s text=%s", user_id[:16], text[:40])

    # Update context token
    _ctx_store.update(user_id, context_token)

    # Get typing ticket
    typing_task: asyncio.Task[None] | None = None
    try:
        cfg = await ilink_api.get_config(_http, base_url, token, user_id, context_token)
        ticket = cfg.get("typing_ticket", "")
        if ticket:
            # Start typing + refresh loop
            await ilink_api.send_typing(_http, base_url, token, user_id, ticket, 1)
            typing_task = asyncio.create_task(
                _typing_refresh(user_id, ticket, token, base_url)
            )
    except Exception as e:
        log.warning("get_config/typing failed: %s", e)
        ticket = ""

    # Invoke Claude (under semaphore)
    # Primary user: full permissions, no cwd isolation
    # Guest user: restricted tools, workspace isolation, lower budget cap
    is_primary = config.is_primary(user_id)
    invoke_kwargs: dict[str, Any] = {}
    if not is_primary:
        ws = workspace.ensure_workspace(user_id)
        invoke_kwargs["cwd"] = str(ws)
        invoke_kwargs["disallowed_tools"] = config.GUEST_DISALLOWED_TOOLS
        invoke_kwargs["max_budget_usd"] = config.GUEST_MAX_BUDGET_USD
        log.info("Guest user %s → workspace %s", user_id[:16], ws)

    result: claude_runner.InvokeResult | None = None
    try:
        session_id = _session_map.get(user_id)  # None for new users
        async with _semaphore:
            result = await claude_runner.invoke(text, session_id, **invoke_kwargs)
        reply = result.text
        # Store session_id from Claude response for future --resume
        if result.session_id:
            _session_map.set(user_id, result.session_id)
        # Schedule idle compact if context is large enough
        _schedule_compact(user_id, result.session_id, result.total_context_tokens)
    except asyncio.TimeoutError:
        reply = "(Response timed out, please try again)"
    except Exception as e:
        log.error("Claude error: %s", e)
        reply = f"(Error: {e})"
    finally:
        # Stop typing
        if typing_task:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
        if ticket:
            await _stop_typing(user_id, ticket, token, base_url)

    # Send reply (chunked if needed)
    ctx = _ctx_store.get(user_id) or context_token
    chunks = chunk_text(reply)
    for chunk in chunks:
        try:
            body = ilink_api.build_text_message(user_id, ctx, chunk)
            await ilink_api.send_message(_http, base_url, token, body)
        except Exception as e:
            log.error("send_message failed: %s", e)
            break

    log.info("Reply sent: user=%s chunks=%d len=%d", user_id[:16], len(chunks), len(reply))


# --- Main poll loop ---

async def _poll_loop(token: str, base_url: str) -> None:
    """Long-poll getupdates and dispatch to per-user workers."""
    assert _http is not None

    buf = ""
    backoff = 0.0

    while not _shutdown:
        try:
            resp = await ilink_api.get_updates(_http, base_url, token, buf)
            backoff = 0.0  # reset on success
        except ApiError as e:
            if e.is_session_expired:
                log.error("Session expired (errcode=-14)! Need to re-login.")
                await _notify_session_expired()
                return
            log.warning("getupdates error: %s (code=%s)", e, e.code)
            backoff = min(backoff + 2, 30)
            await asyncio.sleep(backoff)
            continue
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("getupdates network error: %s", e)
            backoff = min(backoff + 2, 30)
            await asyncio.sleep(backoff)
            continue

        # Update cursor
        new_buf = resp.get("get_updates_buf", "")
        if new_buf:
            buf = new_buf

        # Dispatch messages
        for msg in resp.get("msgs", []):
            msg_id = msg.get("message_id", 0)

            # Dedup
            if _dedup.is_duplicate(msg_id):
                continue

            # Only process user messages
            if msg.get("message_type") != 1:
                continue

            # Permission check
            user_id = msg.get("from_user_id", "")
            if user_id not in config.ALLOWED_USERS:
                log.info("Blocked message from non-allowed user: %s", user_id[:16])
                continue

            # Dispatch to per-user queue
            if user_id not in _user_queues:
                _user_queues[user_id] = asyncio.Queue()
                _user_tasks[user_id] = asyncio.create_task(
                    _user_worker(user_id, token, base_url)
                )
            await _user_queues[user_id].put(msg)


async def _notify_session_expired() -> None:
    """Notify via feishu-cli if available, otherwise just log."""
    chat_id = config.FEISHU_NOTIFY_CHAT_ID
    if not chat_id:
        log.warning("No FEISHU_NOTIFY_CHAT_ID set, cannot send session expiry notification")
        return

    if not shutil.which("feishu-cli"):
        log.warning("feishu-cli not found, cannot send notification")
        return

    proc = await asyncio.create_subprocess_exec(
        "feishu-cli", "send-message",
        "--chat-id", chat_id,
        "--text", "[wechat-bridge] Session expired! Please re-scan QR code.",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    log.info("Session expiry notification sent to feishu chat %s", chat_id)


# --- Shutdown ---

def _handle_signal(sig: signal.Signals) -> None:
    global _shutdown
    log.info("Received %s, shutting down...", sig.name)
    _shutdown = True


async def _flush_state() -> None:
    """Persist all state to disk."""
    _ctx_store.flush()
    _session_map.flush()
    log.info("State flushed to disk")


# --- Entry point ---

async def run_bridge() -> None:
    """Main entry point."""
    global _http, _dedup, _ctx_store, _session_map, _semaphore

    # Init config
    config.init()
    log.info("Allowed users: %s", {u[:16] + "..." for u in config.ALLOWED_USERS})

    # Load credentials
    from .ilink_auth import load_credentials
    creds = load_credentials()
    if not creds:
        log.error("No credentials found. Run: python -m wechat_bridge.ilink_auth")
        sys.exit(1)

    token = creds["bot_token"]
    base_url = creds["base_url"]

    # Init state
    _dedup = MessageDedup()
    _ctx_store = ContextTokenStore(config.STATE_DIR)
    _session_map = SessionMap(config.STATE_DIR)
    _semaphore = asyncio.Semaphore(config.MAX_CONCURRENT)

    # Signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)

    # Run
    log.info("wechat-bridge starting (base_url=%s)", base_url)
    _http = aiohttp.ClientSession()
    try:
        await _poll_loop(token, base_url)
    finally:
        # Cancel compact timers
        for handle in _compact_timers.values():
            handle.cancel()
        _compact_timers.clear()

        # Cancel all user workers
        for task in _user_tasks.values():
            task.cancel()
        if _user_tasks:
            await asyncio.gather(*_user_tasks.values(), return_exceptions=True)

        await _flush_state()
        await _http.close()
        log.info("wechat-bridge stopped")
