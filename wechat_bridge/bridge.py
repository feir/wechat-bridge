"""Main bridge loop: poll → process → reply.

Concurrency model:
- Single getupdates poller (main loop)
- Per-user asyncio task with internal FIFO queue
- Global semaphore limits concurrent Claude subprocesses
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re as _re
import shutil
import signal
import sys
import threading
from pathlib import Path
from typing import Any

import aiohttp

from . import cdn, commands, config, ilink_api, claude_runner, lockfile, workspace
from .chunk import chunk_text
from .format import md_to_wechat
from .claude_runner import InvokeResult
from .ilink_api import ApiError
from .ilink_types import WeixinMessage
from .session import ContextTokenStore, MessageDedup, SessionMap

log = logging.getLogger(__name__)

# --- Globals (initialized in run_bridge) ---
_shutdown = False
_http: aiohttp.ClientSession | None = None
_bot_id: str = ""  # iLink bot user ID, for @mention detection in groups
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
_last_results: dict[str, InvokeResult] = {}  # user_id → last InvokeResult (for /status)


# --- Text extraction & reply helpers ---

def _detect_group(msg: WeixinMessage) -> str | None:
    """Detect if message is from a group chat.

    Returns group_id (room_id / chat_room_id) if group message, None if DM.
    iLink uses room_id or chat_room_id fields, and group IDs end with @chatroom.
    """
    room_id = str(msg.get("room_id", "") or msg.get("chat_room_id", "") or "").strip()  # type: ignore[call-overload]
    if room_id:
        return room_id
    # Heuristic: to_user_id ending in @chatroom
    to_user = str(msg.get("to_user_id", "")).strip()
    if to_user.endswith("@chatroom"):
        return to_user
    return None


def _is_group_allowed(group_id: str) -> bool:
    """Check if a group message should be processed based on GROUP_POLICY."""
    if config.GROUP_POLICY == "disabled":
        return False
    if config.GROUP_POLICY == "open":
        return True
    if config.GROUP_POLICY == "allowlist":
        return group_id in config.ALLOWED_GROUPS
    return False


def _has_mention(msg: WeixinMessage, text: str) -> bool:
    """Check if message contains an @mention of our bot specifically.

    Only returns True if the bot's ID appears in at_user_list or if
    to_user_id matches the bot (direct reply to bot in group).
    Falls back to checking if message's to_user_id is the bot (some
    iLink versions route @mentions this way).
    """
    # Check at_user_list for bot-specific mention
    at_list = msg.get("at_user_list", [])  # type: ignore[call-overload]
    if at_list and _bot_id:
        if _bot_id in at_list:
            return True
        # at_list exists but bot not in it → not our mention
        return False
    if at_list:
        # No bot_id to compare — conservatively accept
        return True

    # Some iLink versions set to_user_id to the mentioned user
    to_user = str(msg.get("to_user_id", "")).strip()
    if _bot_id and to_user == _bot_id:
        return True

    return False


_MENTION_RE = _re.compile(r"@\S+\s*")


def _strip_mention(text: str) -> str:
    """Remove @mention prefix from group message text.

    Users typically send '@bot /stop' in groups. Strip the mention
    so command parsing and Claude prompt are clean.
    """
    return _MENTION_RE.sub("", text, count=1).strip()


def _extract_text(msg: WeixinMessage) -> str:
    """Extract text from incoming WeChat message.

    Supports: text items (type=1), voice ASR transcription (type=3 with text).
    """
    for item in msg.get("item_list", []):
        itype = item.get("type")
        if itype == 1 and "text_item" in item:
            return item["text_item"]["text"]
        # Voice with ASR transcription → treat as text (P0)
        if itype == 3 and "voice_item" in item:
            asr_text = item["voice_item"].get("text", "")
            if asr_text:
                return f"[语音转文字] {asr_text}"
    return ""


def _extract_images(msg: WeixinMessage) -> list[dict]:
    """Extract image items from message for CDN download."""
    images = []
    for item in msg.get("item_list", []):
        if item.get("type") == 2 and "image_item" in item:
            images.append(item["image_item"])
    return images


def _extract_files(msg: WeixinMessage) -> list[tuple[dict, str]]:
    """Extract file items (type=4) from message. Returns [(media_dict, filename), ...]."""
    files = []
    for item in msg.get("item_list", []):
        if item.get("type") == 4 and "file_item" in item:
            fi = item["file_item"]
            # Sanitize to basename to prevent path traversal from untrusted input
            fname = Path(fi.get("file_name", "")).name
            files.append((fi.get("media", {}), fname))
    return files


def _extract_videos(msg: WeixinMessage) -> list[dict]:
    """Extract video items (type=5) from message. Returns [media_dict, ...]."""
    videos = []
    for item in msg.get("item_list", []):
        if item.get("type") == 5 and "video_item" in item:
            videos.append(item["video_item"].get("media", {}))
    return videos


def _extract_quoted_media(msg: WeixinMessage) -> tuple[str, list[dict], list[tuple[dict, str]], list[dict]]:
    """Extract text and media from quoted/reply messages.

    iLink includes quoted message content in item_list entries with a
    'reply_item' or 'quote_item' wrapper, which contains its own item_list.
    Also checks top-level 'reply' and 'quote' fields on the message.

    Returns: (quoted_text, images, files, videos) from the quoted message.
    """
    quoted_text = ""
    images: list[dict] = []
    files: list[tuple[dict, str]] = []
    videos: list[dict] = []

    # Check all possible quote locations in iLink protocol
    quote_sources: list[dict] = []

    # Top-level reply/quote fields
    for key in ("reply", "quote", "reply_item", "quote_item"):
        q = msg.get(key)  # type: ignore[call-overload]
        if isinstance(q, dict):
            quote_sources.append(q)

    # Item-level reply wrappers
    for item in msg.get("item_list", []):
        for key in ("reply_item", "quote_item"):
            q = item.get(key)
            if isinstance(q, dict):
                quote_sources.append(q)

    for source in quote_sources:
        for qitem in source.get("item_list", []):
            qtype = qitem.get("type")
            if qtype == 1 and "text_item" in qitem:
                qt = qitem["text_item"].get("text", "")
                if qt and not quoted_text:
                    quoted_text = qt
            elif qtype == 2 and "image_item" in qitem:
                images.append(qitem["image_item"])
            elif qtype == 4 and "file_item" in qitem:
                fi = qitem["file_item"]
                files.append((fi.get("media", {}), Path(fi.get("file_name", "")).name))
            elif qtype == 5 and "video_item" in qitem:
                videos.append(qitem["video_item"].get("media", {}))

        # Some formats have direct media fields
        if "image_item" in source:
            images.append(source["image_item"])
        if "media" in source and source.get("type") in (2, 4, 5):
            # Flat format: media directly on quote
            if source.get("type") == 2:
                images.append({"media": source["media"]})
            elif source.get("type") == 4:
                files.append((source["media"], Path(source.get("file_name", "")).name))
            elif source.get("type") == 5:
                videos.append(source["media"])

    return quoted_text, images, files, videos


async def _send_media(
    user_id: str, token: str, base_url: str, file_path: Path,
) -> bool:
    """Upload and send a media file (image/file) to user. Returns True on success.

    Currently unused — intended for sending Claude-generated files (plots, exports)
    back to the user. Will be wired into Claude tool output handling.
    """
    assert _http is not None
    ctx = _ctx_store.get(user_id) or ""

    upload_info = await cdn.upload_media(_http, base_url, token, file_path)
    if not upload_info:
        return False

    suffix = file_path.suffix.lower()
    if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        body = ilink_api.build_image_message(user_id, ctx, upload_info)
    else:
        body = ilink_api.build_file_message(user_id, ctx, upload_info, file_path.name)

    try:
        await ilink_api.send_message(_http, base_url, token, body)
        log.info("Media sent to user %s: %s", user_id[:16], file_path.name)
        return True
    except Exception as e:
        log.error("send_message (media) failed: %s", e)
        return False


async def _send_reply(
    user_id: str, token: str, base_url: str, text: str,
) -> None:
    """Send text reply to user (format-adapted, chunked if needed). Best-effort."""
    assert _http is not None
    ctx = _ctx_store.get(user_id) or ""
    if not ctx:
        log.warning("No context_token for user %s, reply may fail", user_id[:16])
    text = md_to_wechat(text)
    chunks = chunk_text(text)
    for chunk in chunks:
        try:
            body = ilink_api.build_text_message(user_id, ctx, chunk)
            await ilink_api.send_message(_http, base_url, token, body)
        except Exception as e:
            log.error("send_message failed: %s", e)
            break


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

    try:
        while not _shutdown:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=300)  # 5min idle
            except asyncio.TimeoutError:
                log.info("User worker idle timeout: %s", user_id[:16])
                break

            await _process_message(msg, token, base_url)
            queue.task_done()
    finally:
        # Cleanup — runs on normal exit, CancelledError (/stop, /new), or exception
        _user_tasks.pop(user_id, None)
        _user_queues.pop(user_id, None)
        _last_results.pop(user_id, None)
        log.info("User worker exited: %s", user_id[:16])


async def _process_message(msg: WeixinMessage, token: str, base_url: str) -> None:
    """Process a single incoming message: command check → media → typing → Claude → reply."""
    assert _http is not None

    sender_id = msg["from_user_id"]
    context_token = msg["context_token"]
    group_id = _detect_group(msg)
    # Reply target: group_id for group messages, sender_id for DMs
    reply_to = group_id or sender_id
    # Session key: same as reply_to (group shares one Claude session)
    user_id = reply_to

    # Extract text (includes voice ASR) and media
    text = _extract_text(msg)
    images = _extract_images(msg)
    files = _extract_files(msg)
    videos = _extract_videos(msg)

    # Strip @mention prefix for group messages so commands and Claude prompt are clean
    if group_id and text:
        text = _strip_mention(text)

    # Extract quoted/reply message media (user long-pressing a message to reply)
    quoted_text, q_images, q_files, q_videos = _extract_quoted_media(msg)
    images.extend(q_images)
    files.extend(q_files)
    videos.extend(q_videos)

    if not text and not images and not files and not videos:
        log.info("Non-text message from %s, skipping", user_id[:16])
        return

    # Prepend quoted text context if user is replying to a message
    if quoted_text and text:
        text = f"[引用消息: {quoted_text[:200]}]\n\n{text}"

    log.info("Processing: user=%s text=%s images=%d files=%d videos=%d",
             user_id[:16], (text or "(none)")[:40], len(images), len(files), len(videos))

    # Update context token (keyed by reply target so send_reply can find it)
    _ctx_store.update(reply_to, context_token)

    # --- Bridge command handling (queued commands: /compact, /status, /help) ---
    if text:
        parsed = commands.parse_command(text)
        if parsed:
            cmd, arg = parsed
            reply = await _handle_queued_command(cmd, arg, user_id, sender_id)
            await _send_reply(user_id, token, base_url, reply)
            return

    # --- Download, invoke, reply — unified try/finally for cleanup ---
    media_paths: list[Path] = []  # all downloaded media (images, files, videos)
    typing_task: asyncio.Task[None] | None = None
    ticket = ""

    try:
        # Download media (before typing — download can be slow)
        if images:
            for img in images:
                path = await cdn.download_image(_http, base_url, token, img)
                if path:
                    media_paths.append(path)
            log.info("Downloaded %d/%d images for user %s",
                     len(media_paths), len(images), user_id[:16])

        if files:
            for media_dict, fname in files:
                path = await cdn.download_media(
                    _http, base_url, token, media_dict, file_name=fname,
                )
                if path:
                    media_paths.append(path)
            log.info("Downloaded %d/%d files for user %s",
                     sum(1 for _ in files), len(files), user_id[:16])

        if videos:
            for media_dict in videos:
                path = await cdn.download_media(
                    _http, base_url, token, media_dict, suffix=".mp4",
                )
                if path:
                    media_paths.append(path)
            log.info("Downloaded %d/%d videos for user %s",
                     sum(1 for _ in videos), len(videos), user_id[:16])

        # Build prompt: inject media paths so Claude can Read them
        prompt = text or ""
        if media_paths:
            media_refs = "\n".join(str(p) for p in media_paths)
            label = "图片" if images and not files and not videos else "文件"
            if prompt:
                prompt = f"{prompt}\n\n[用户同时发送了{label}，请用 Read 工具查看:]\n{media_refs}"
            else:
                prompt = f"[用户发送了{label}，请用 Read 工具查看并描述:]\n{media_refs}"

        if not prompt:
            log.warning("No content to process for user %s", user_id[:16])
            return

        # Get typing ticket
        try:
            cfg = await ilink_api.get_config(_http, base_url, token, user_id, context_token)
            ticket = cfg.get("typing_ticket", "")
            if ticket:
                await ilink_api.send_typing(_http, base_url, token, user_id, ticket, 1)
                typing_task = asyncio.create_task(
                    _typing_refresh(user_id, ticket, token, base_url)
                )
        except Exception as e:
            log.warning("get_config/typing failed: %s", e)

        # Invoke Claude (under semaphore)
        # Use sender_id for privilege check — user_id is group_id for groups
        is_primary = config.is_primary(sender_id)
        invoke_kwargs: dict[str, Any] = {}
        if not is_primary:
            ws = workspace.ensure_workspace(user_id)
            invoke_kwargs["cwd"] = str(ws)
            invoke_kwargs["disallowed_tools"] = config.GUEST_DISALLOWED_TOOLS
            invoke_kwargs["max_budget_usd"] = config.GUEST_MAX_BUDGET_USD
            log.info("Guest user %s → workspace %s", user_id[:16], ws)

        session_id = _session_map.get(user_id)
        async with _semaphore:
            result = await claude_runner.invoke(prompt, session_id, **invoke_kwargs)
        reply = result.text
        if result.session_id:
            _session_map.set(user_id, result.session_id)
        _last_results[user_id] = result
        _schedule_compact(user_id, result.session_id, result.total_context_tokens)
        suffix = commands.reply_suffix(result, config.CLAUDE_MODEL)
        if suffix:
            reply = reply + "\n\n" + suffix

    except asyncio.CancelledError:
        raise  # Let CancelledError propagate (cleanup in finally)
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
        # Clean up temp media files (guaranteed even on CancelledError)
        for p in media_paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # Send reply (chunked if needed)
    await _send_reply(user_id, token, base_url, reply)
    log.info("Reply sent: user=%s len=%d", user_id[:16], len(reply))


async def _handle_queued_command(cmd: str, arg: str, user_id: str, sender_id: str) -> str:
    """Handle commands that are serialized with messages (/compact, /status, /help).

    sender_id is the actual human who sent the command (differs from user_id in groups).
    """
    if cmd == "/compact":
        session_id = _session_map.get(user_id)
        if not session_id:
            return "当前无活跃会话，无需压缩"
        async with _semaphore:
            return await commands.run_compact(session_id)
    elif cmd == "/status":
        session_id = _session_map.get(user_id)
        last = _last_results.get(user_id)
        return commands.format_status(last, session_id, config.CLAUDE_MODEL)
    elif cmd == "/update":
        if not config.is_primary(sender_id):
            return "仅主用户可执行 /update"
        return await commands.run_update()
    elif cmd == "/help":
        return commands.format_help()
    else:
        return f"未知命令: {cmd}"


# --- Main poll loop ---

_SESSION_PAUSE_S = 60  # Pause before retry (transient -14 protection)


async def _poll_loop(token: str, base_url: str) -> None:
    """Long-poll getupdates and dispatch to per-user workers."""
    assert _http is not None

    buf = ""
    backoff = 0.0
    session_retry_pending = False

    while not _shutdown:
        try:
            resp = await ilink_api.get_updates(_http, base_url, token, buf)
            backoff = 0.0
            session_retry_pending = False
        except ApiError as e:
            if e.is_session_expired:
                if not session_retry_pending:
                    # First -14: might be transient, pause and retry same token
                    log.warning("Session expired (errcode=-14), pausing %ds before retry...",
                                _SESSION_PAUSE_S)
                    session_retry_pending = True
                    await asyncio.sleep(_SESSION_PAUSE_S)
                    continue
                # Second -14: confirmed expiry, start recovery
                log.error("Session expiry confirmed after retry. Starting recovery...")
                result = await _session_recovery(base_url)
                if result is None:
                    return  # Shutdown or unrecoverable
                token, base_url = result
                buf = ""
                session_retry_pending = False
                # Safe: new workers only spawn in dispatch section below
                # (after get_updates succeeds with new token). Existing workers
                # hold old token by value and are cancelled here.
                await _cancel_all_workers()
                continue
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

            # Extract text once for all checks below
            user_id = msg.get("from_user_id", "")
            group_id = _detect_group(msg)
            text = _extract_text(msg)

            if group_id:
                # Group message: check group policy
                if not _is_group_allowed(group_id):
                    continue

                # @mention filter: only respond when bot specifically mentioned
                if config.GROUP_REQUIRE_MENTION:
                    if not _has_mention(msg, text):
                        continue

                # Strip @mention prefix so commands and prompts are clean
                if text:
                    text = _strip_mention(text)

                # Group messages use group_id as queue key (shared session per group)
                chat_id = group_id
            else:
                # DM: permission check on sender
                if user_id not in config.ALLOWED_USERS:
                    log.info("Blocked message from non-allowed user: %s", user_id[:16])
                    continue
                chat_id = user_id

            # Immediate commands (/stop, /new, /restart) bypass the queue
            if text:
                parsed = commands.parse_command(text)
                if parsed and parsed[0] in ("/stop", "/new", "/restart"):
                    # Update context token even for immediate commands
                    ctx_tok = msg.get("context_token", "")
                    if ctx_tok:
                        _ctx_store.update(chat_id, ctx_tok)
                    await _handle_immediate_command(
                        parsed[0], chat_id, user_id, token, base_url,
                    )
                    continue

            # Dispatch to per-chat queue (user_id for DM, group_id for group)
            if chat_id not in _user_queues:
                _user_queues[chat_id] = asyncio.Queue()
                _user_tasks[chat_id] = asyncio.create_task(
                    _user_worker(chat_id, token, base_url)
                )
            await _user_queues[chat_id].put(msg)


async def _handle_immediate_command(
    cmd: str, user_id: str, sender_id: str, token: str, base_url: str,
) -> None:
    """Handle commands that bypass the queue (/stop, /new, /restart).

    These must be processed immediately — if they were queued, they'd
    block behind the very Claude invocation the user wants to cancel.
    sender_id is the actual human who sent the command (differs from user_id in groups).
    """
    if cmd == "/stop":
        task = _user_tasks.get(user_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            # Defensive cleanup (worker finally should handle this, but be safe)
            _user_tasks.pop(user_id, None)
            _user_queues.pop(user_id, None)
            log.info("User task cancelled via /stop: %s", user_id[:16])
            await _send_reply(user_id, token, base_url, "已停止当前任务")
        else:
            await _send_reply(user_id, token, base_url, "当前没有运行中的任务")

    elif cmd == "/new":
        # Cancel running task if any
        task = _user_tasks.get(user_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            _user_tasks.pop(user_id, None)
            _user_queues.pop(user_id, None)
            log.info("User task cancelled via /new: %s", user_id[:16])
        # Reset session
        _session_map.reset(user_id)
        _session_map.flush()
        _last_results.pop(user_id, None)
        # Cancel idle compact timer
        timer = _compact_timers.pop(user_id, None)
        if timer:
            timer.cancel()
        log.info("Session reset via /new: %s", user_id[:16])
        await _send_reply(user_id, token, base_url, "会话已重置，开始新对话")

    elif cmd == "/restart":
        if not config.is_primary(sender_id):
            await _send_reply(user_id, token, base_url, "仅主用户可执行 /restart")
            return
        log.info("Restart requested by user %s in chat %s",
                 sender_id[:16], user_id[:16])
        from wechat_bridge import __version__
        await _send_reply(user_id, token, base_url, f"正在重启 v{__version__}...")
        # Persist chat_id so the new process can send restart-complete notification
        restart_file = config.STATE_DIR / "restart.json"
        try:
            restart_file.write_text(json.dumps({"chat_id": user_id}))
        except Exception:
            log.exception("Failed to write restart state")
        await _flush_state()
        # Non-zero exit triggers Restart=on-failure in systemd
        def _deferred_exit():
            logging.shutdown()
            os._exit(1)
        threading.Timer(0.3, _deferred_exit).start()


_RECOVERY_MAX_ERRORS = 10  # Max consecutive non-transient errors before giving up


async def _session_recovery(base_url: str) -> tuple[str, str] | None:
    """Recover session via QR re-login with Feishu notifications.

    Flow:
        1. Notify Feishu "系统监测" group
        2. Generate QR → push URL to Feishu → poll for scan
        3. On confirmed: save credentials, clear stale state, return new (token, base_url)
        4. On QR expired: generate new QR and repeat
    Returns None on shutdown or unrecoverable error.

    Security: QR URL is sent to FEISHU_NOTIFY_CHAT_ID. Ensure this chat
    is restricted to authorized operators — anyone who can see the QR can
    hijack the WeChat session (valid ~5 min).
    """
    assert _http is not None
    from .ilink_auth import save_credentials

    await _notify_feishu(
        "[wechat-bridge] Session expired (errcode=-14)\n"
        "Initiating QR re-login. Generating QR code..."
    )

    error_count = 0

    while not _shutdown:
        try:
            qr_resp = await ilink_api.fetch_qr_code(_http, base_url)
            qr_url = qr_resp.get("qrcode_img_content", "")
            qr_id = qr_resp["qrcode"]
            error_count = 0  # QR fetch succeeded, reset error counter

            # Push QR URL to Feishu for easy mobile scanning
            await _notify_feishu(
                "[wechat-bridge] Scan to re-login:\n"
                f"{qr_url}\n\n"
                "QR expires in ~5 min. Waiting..."
            )
            log.info("QR code pushed to Feishu, waiting for scan...")

            # Poll scan status
            while not _shutdown:
                status_resp = await ilink_api.poll_qr_status(_http, base_url, qr_id)
                status = status_resp["status"]

                if status == "confirmed":
                    new_creds = {
                        "bot_token": status_resp["bot_token"],
                        "base_url": status_resp.get("baseurl", base_url),
                        "bot_id": status_resp.get("ilink_bot_id", ""),
                        "user_id": status_resp.get("ilink_user_id", ""),
                    }
                    save_credentials(new_creds)
                    # Context tokens invalidated by new iLink session.
                    # _session_map (Claude sessions) deliberately NOT cleared —
                    # Claude sessions are independent of iLink and can resume.
                    _ctx_store.clear()
                    _ctx_store.flush()
                    await _notify_feishu(
                        "[wechat-bridge] Re-login successful! Bot resumed.\n"
                        f"bot_id={new_creds['bot_id']}"
                    )
                    log.info("Session recovery complete, new token obtained")
                    return new_creds["bot_token"], new_creds["base_url"]

                elif status == "scaned_but_redirect":
                    new_host = status_resp.get("redirect_host", "")
                    if new_host:
                        if new_host.startswith("http://"):
                            log.error("QR redirect rejected: HTTP not allowed (got %s)", new_host)
                            return None
                        base_url = new_host if new_host.startswith("https://") else f"https://{new_host}"
                        log.info("QR login redirect to %s", base_url)

                elif status == "scaned":
                    log.info("QR scanned, waiting for confirmation...")

                elif status == "expired":
                    log.info("QR expired, generating new one...")
                    await _notify_feishu(
                        "[wechat-bridge] QR code expired. Generating new one..."
                    )
                    break  # Break inner loop → generate new QR

                await asyncio.sleep(2)

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # Transient network error — retry without counting
            log.warning("Session recovery transient error: %s", e)
            await asyncio.sleep(30)
        except Exception as e:
            # Contract/API error — count towards limit
            error_count += 1
            log.error("Session recovery error (%d/%d): %s",
                      error_count, _RECOVERY_MAX_ERRORS, e)
            if error_count >= _RECOVERY_MAX_ERRORS:
                await _notify_feishu(
                    f"[wechat-bridge] Session recovery FAILED after {error_count} errors.\n"
                    f"Last error: {e}\n"
                    "Bridge stopping. Manual intervention required."
                )
                return None
            await asyncio.sleep(30)

    return None


async def _cancel_all_workers() -> None:
    """Cancel all user workers (they hold stale token references)."""
    for task in _user_tasks.values():
        task.cancel()
    if _user_tasks:
        await asyncio.gather(*_user_tasks.values(), return_exceptions=True)
    _user_tasks.clear()
    _user_queues.clear()
    for handle in _compact_timers.values():
        handle.cancel()
    _compact_timers.clear()
    log.info("All user workers cancelled (session reset)")


async def _notify_feishu(text: str) -> None:
    """Send notification to Feishu 系统监测 group. Best-effort, never raises."""
    chat_id = config.FEISHU_NOTIFY_CHAT_ID
    if not chat_id:
        log.warning("No FEISHU_NOTIFY_CHAT_ID set, skipping notification")
        return

    cli = shutil.which("feishu-cli")
    if not cli:
        log.warning("feishu-cli not found, skipping notification")
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            cli, "send-message", "--chat-id", chat_id, "--text", text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        if proc.returncode != 0:
            stderr = (await proc.stderr.read()).decode()[:200] if proc.stderr else ""
            log.warning("feishu-cli failed (rc=%d): %s", proc.returncode, stderr)
    except Exception as e:
        log.warning("Failed to send feishu notification: %s", e)


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


async def _notify_restart_complete(token: str, base_url: str) -> None:
    """Send restart-complete message if a /restart was in progress."""
    restart_file = config.STATE_DIR / "restart.json"
    if not restart_file.exists():
        return
    try:
        data = json.loads(restart_file.read_text())
        chat_id = data.get("chat_id")
        if chat_id:
            from wechat_bridge import __version__
            await _send_reply(chat_id, token, base_url, f"重启完成 v{__version__}")
            log.info("Restart-complete notification sent to %s", chat_id[:16])
    except Exception:
        log.warning("Failed to send restart-complete notification", exc_info=True)
    finally:
        try:
            restart_file.unlink(missing_ok=True)
        except Exception:
            pass


# --- Entry point ---

async def run_bridge() -> None:
    """Main entry point."""
    global _http, _dedup, _ctx_store, _session_map, _semaphore, _bot_id

    # Init config
    config.init()
    log.info("Allowed users: %s", {u[:16] + "..." for u in config.ALLOWED_USERS})

    # Load credentials (path from config, supports multi-instance)
    from .ilink_auth import load_credentials
    creds = load_credentials()
    if not creds:
        log.error("No credentials at %s. Run: wechat-bridge --login", config.CREDENTIALS_FILE)
        sys.exit(1)

    token = creds["bot_token"]
    base_url = creds["base_url"]
    _bot_id = creds.get("bot_id", "") or creds.get("user_id", "")

    # Acquire exclusive lock (prevent multi-instance token conflicts)
    _lock = lockfile.BridgeLock(config.STATE_DIR)
    if not _lock.acquire():
        sys.exit(1)

    # Init state
    _dedup = MessageDedup()
    _ctx_store = ContextTokenStore(config.STATE_DIR)
    _session_map = SessionMap(config.STATE_DIR)
    _semaphore = asyncio.Semaphore(config.MAX_CONCURRENT)

    # Start background update checker
    from .updater import get_install_info, init_updater
    _mode, _src_path = get_install_info()
    init_updater(_mode, _src_path)

    # Signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)

    # Run
    log.info("wechat-bridge starting (base_url=%s)", base_url)
    _http = aiohttp.ClientSession()

    # Notify restart-complete if a /restart was in progress
    await _notify_restart_complete(token, base_url)

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
        _lock.release()
        log.info("wechat-bridge stopped")
