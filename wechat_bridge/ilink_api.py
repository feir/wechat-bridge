"""iLink Bot API client — 5 HTTP endpoints + header construction."""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Literal, cast
from urllib.parse import quote
from uuid import uuid4

import aiohttp

from .ilink_types import (
    BaseInfo,
    GetConfigResponse,
    GetUpdatesResponse,
    MessageItemType,
    MessageState,
    MessageType,
    QrCodeResponse,
    QrStatusResponse,
    SendMessageBody,
)

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.0"


# --- Errors ---

class ApiError(Exception):
    """iLink API returned an error."""

    def __init__(self, message: str, *, status: int, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def is_session_expired(self) -> bool:
        return self.code == -14


# --- Header helpers ---

def random_wechat_uin() -> str:
    """Random uint32 → decimal string → base64."""
    value = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(value).encode()).decode("ascii")


def build_headers(token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": random_wechat_uin(),
    }


def _base_info() -> BaseInfo:
    return {"channel_version": CHANNEL_VERSION}


# --- Low-level request ---

async def _post(
    session: aiohttp.ClientSession,
    base_url: str,
    endpoint: str,
    body: dict[str, Any],
    token: str,
    timeout_s: float = 40,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{endpoint}"
    async with session.post(
        url,
        headers=build_headers(token),
        json=body,
        timeout=aiohttp.ClientTimeout(total=timeout_s),
    ) as resp:
        text = await resp.text()
        payload = cast(dict[str, Any], json.loads(text) if text else {})

        if resp.status < 200 or resp.status >= 300:
            msg = payload.get("errmsg") or f"{endpoint} HTTP {resp.status}"
            raise ApiError(msg, status=resp.status, code=payload.get("errcode"))

        # Some endpoints return ret/errcode only on error; success has no ret field
        ret = payload.get("ret")
        errcode = payload.get("errcode")
        if (isinstance(ret, int) and ret != 0) or (isinstance(errcode, int) and errcode < 0):
            code = errcode if errcode is not None else ret
            msg = payload.get("errmsg") or f"{endpoint} ret={ret} errcode={errcode}"
            raise ApiError(msg, status=resp.status, code=code)

        return payload


async def _get(
    session: aiohttp.ClientSession,
    base_url: str,
    path: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    async with session.get(url, headers=headers or {}) as resp:
        text = await resp.text()
        payload = cast(dict[str, Any], json.loads(text) if text else {})

        if resp.status < 200 or resp.status >= 300:
            msg = payload.get("errmsg") or f"{path} HTTP {resp.status}"
            raise ApiError(msg, status=resp.status, code=payload.get("errcode"))

        return payload


# --- 5 API endpoints ---

async def get_updates(
    session: aiohttp.ClientSession,
    base_url: str,
    token: str,
    buf: str,
) -> GetUpdatesResponse:
    """Long-poll for new messages (35s hold by server)."""
    body = {
        "get_updates_buf": buf,
        "base_info": _base_info(),
    }
    payload = await _post(session, base_url, "/ilink/bot/getupdates", body, token, timeout_s=40)
    return cast(GetUpdatesResponse, payload)


async def send_message(
    session: aiohttp.ClientSession,
    base_url: str,
    token: str,
    msg: SendMessageBody,
) -> dict[str, Any]:
    """Send a message to a user."""
    body = {"msg": msg, "base_info": _base_info()}
    return await _post(session, base_url, "/ilink/bot/sendmessage", body, token, timeout_s=15)


async def get_config(
    session: aiohttp.ClientSession,
    base_url: str,
    token: str,
    user_id: str,
    context_token: str,
) -> GetConfigResponse:
    """Get typing ticket and other config for a conversation."""
    body = {
        "ilink_user_id": user_id,
        "context_token": context_token,
        "base_info": _base_info(),
    }
    payload = await _post(session, base_url, "/ilink/bot/getconfig", body, token, timeout_s=15)
    return cast(GetConfigResponse, payload)


async def send_typing(
    session: aiohttp.ClientSession,
    base_url: str,
    token: str,
    user_id: str,
    ticket: str,
    status: Literal[1, 2],
) -> dict[str, Any]:
    """Set typing status: 1=start, 2=stop."""
    body = {
        "ilink_user_id": user_id,
        "typing_ticket": ticket,
        "status": status,
        "base_info": _base_info(),
    }
    return await _post(session, base_url, "/ilink/bot/sendtyping", body, token, timeout_s=15)


# --- QR login (no auth needed) ---

async def fetch_qr_code(
    session: aiohttp.ClientSession,
    base_url: str = DEFAULT_BASE_URL,
) -> QrCodeResponse:
    """Request a new QR code for bot login."""
    payload = await _get(session, base_url, "/ilink/bot/get_bot_qrcode?bot_type=3")
    return cast(QrCodeResponse, payload)


async def poll_qr_status(
    session: aiohttp.ClientSession,
    base_url: str,
    qrcode: str,
) -> QrStatusResponse:
    """Poll QR code scan status."""
    path = f"/ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
    payload = await _get(
        session, base_url, path,
        headers={"iLink-App-ClientVersion": "1"},
    )
    return cast(QrStatusResponse, payload)


# --- Message construction helper ---

def build_text_message(
    user_id: str,
    context_token: str,
    text: str,
) -> SendMessageBody:
    """Build a text reply message."""
    return {
        "from_user_id": "",
        "to_user_id": user_id,
        "client_id": str(uuid4()),
        "message_type": MessageType.BOT,
        "message_state": MessageState.FINISH,
        "context_token": context_token,
        "item_list": [
            {
                "type": MessageItemType.TEXT,
                "text_item": {"text": text},
            }
        ],
    }


def build_image_message(
    user_id: str,
    context_token: str,
    upload_info: dict[str, Any],
) -> SendMessageBody:
    """Build an image reply message from CDN upload metadata."""
    return {
        "from_user_id": "",
        "to_user_id": user_id,
        "client_id": str(uuid4()),
        "message_type": MessageType.BOT,
        "message_state": MessageState.FINISH,
        "context_token": context_token,
        "item_list": [
            {
                "type": MessageItemType.IMAGE,
                "image_item": {
                    "media": {
                        "encrypt_query_param": upload_info.get("encrypt_query_param", ""),
                        "aes_key": upload_info.get("aes_key", ""),
                        "encrypt_type": upload_info.get("encrypt_type", 1),
                    },
                },
            }
        ],
    }


def build_file_message(
    user_id: str,
    context_token: str,
    upload_info: dict[str, Any],
    file_name: str,
) -> SendMessageBody:
    """Build a file reply message from CDN upload metadata."""
    return {
        "from_user_id": "",
        "to_user_id": user_id,
        "client_id": str(uuid4()),
        "message_type": MessageType.BOT,
        "message_state": MessageState.FINISH,
        "context_token": context_token,
        "item_list": [
            {
                "type": MessageItemType.FILE,
                "file_item": {
                    "media": {
                        "encrypt_query_param": upload_info.get("encrypt_query_param", ""),
                        "aes_key": upload_info.get("aes_key", ""),
                        "encrypt_type": upload_info.get("encrypt_type", 1),
                    },
                    "file_name": file_name,
                },
            }
        ],
    }
