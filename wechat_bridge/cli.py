"""wechat-cli: Command-line interface for WeChat iLink Bot API.

Usage:
    wechat-cli send-message --user-id <id> --text <text>
    wechat-cli send-message --broadcast --text <text>
    wechat-cli list-users
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from . import config
from .ilink_api import CHANNEL_VERSION
from .ilink_types import MessageItemType, MessageState, MessageType


def _state_dir() -> Path:
    env = os.environ.get("WECHAT_STATE_DIR", "").strip()
    return Path(env) if env else Path.home() / ".local" / "share" / "wechat-bridge"


def _creds_path() -> Path:
    env = os.environ.get("WECHAT_CREDENTIALS_FILE", "").strip()
    return Path(env) if env else config.CREDENTIALS_FILE


def _load_credentials() -> dict[str, str]:
    creds_file = _creds_path()
    if not creds_file.exists():
        print(f"Error: No credentials at {creds_file}", file=sys.stderr)
        print("Run: wechat-bridge --login", file=sys.stderr)
        sys.exit(1)
    data = json.loads(creds_file.read_text())
    if not data.get("bot_token") or not data.get("base_url"):
        print("Error: Invalid credentials (missing bot_token or base_url)", file=sys.stderr)
        sys.exit(1)
    return data


def _load_context_tokens() -> dict[str, str]:
    path = _state_dir() / "context_tokens.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _random_wechat_uin() -> str:
    value = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(value).encode()).decode("ascii")


def _build_headers(token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": _random_wechat_uin(),
    }


def _send_text(base_url: str, token: str, user_id: str, context_token: str, text: str) -> bool:
    """Send a text message via iLink API. Prints JSON result."""
    url = f"{base_url.rstrip('/')}/ilink/bot/sendmessage"
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": user_id,
            "client_id": str(uuid4()),
            "message_type": MessageType.BOT,
            "message_state": MessageState.FINISH,
            "context_token": context_token,
            "item_list": [{"type": MessageItemType.TEXT, "text_item": {"text": text}}],
        },
        "base_info": {"channel_version": CHANNEL_VERSION},
    }
    data = json.dumps(body).encode()
    req = Request(url, data=data, headers=_build_headers(token), method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            # Match ilink_api.py error detection: check both ret and errcode
            ret = result.get("ret")
            errcode = result.get("errcode")
            if (isinstance(ret, int) and ret != 0) or (isinstance(errcode, int) and errcode < 0):
                errmsg = result.get("errmsg", f"ret={ret} errcode={errcode}")
                print(json.dumps({"ok": False, "user_id": user_id, "error": errmsg}))
                return False
            print(json.dumps({"ok": True, "user_id": user_id}))
            return True
    except HTTPError as e:
        # Parse structured error if available; avoid leaking raw server response
        try:
            err_body = json.loads(e.read().decode())
            errmsg = err_body.get("errmsg", f"HTTP {e.code}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            errmsg = f"HTTP {e.code}"
        print(json.dumps({"ok": False, "user_id": user_id, "error": errmsg}))
        return False
    except (URLError, OSError) as e:
        print(json.dumps({"ok": False, "user_id": user_id, "error": str(e)}))
        return False


def cmd_send_message(args: argparse.Namespace) -> None:
    creds = _load_credentials()
    ctx_tokens = _load_context_tokens()

    if args.broadcast:
        if not ctx_tokens:
            print("Error: No known users (context_tokens.json is empty)", file=sys.stderr)
            sys.exit(1)
        targets = list(ctx_tokens.keys())
    elif args.user_id:
        targets = [args.user_id]
    else:
        print("Error: --user-id or --broadcast required", file=sys.stderr)
        sys.exit(1)

    success = 0
    for uid in targets:
        ctx = ctx_tokens.get(uid)
        if not ctx:
            print(json.dumps({"ok": False, "user_id": uid, "error": "no context_token (user never messaged the bot)"}))
            continue
        if _send_text(creds["base_url"], creds["bot_token"], uid, ctx, args.text):
            success += 1

    sys.exit(0 if success > 0 else 1)


def cmd_list_users(args: argparse.Namespace) -> None:
    ctx_tokens = _load_context_tokens()
    users = [{"user_id": uid} for uid in ctx_tokens]
    print(json.dumps({"users": users, "count": len(users)}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="wechat-cli", description="WeChat iLink Bot CLI")
    sub = parser.add_subparsers(dest="command")

    p_send = sub.add_parser("send-message", help="Send a text message")
    g = p_send.add_mutually_exclusive_group(required=True)
    g.add_argument("--user-id", help="Target user ID (iLink user_id)")
    g.add_argument("--broadcast", action="store_true", help="Send to all known users")
    p_send.add_argument("--text", required=True, help="Message text")
    p_send.set_defaults(func=cmd_send_message)

    p_list = sub.add_parser("list-users", help="List known users with context tokens")
    p_list.set_defaults(func=cmd_list_users)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
