"""TypedDict definitions for iLink Bot API protocol."""

from __future__ import annotations

from enum import IntEnum
from typing import Literal, TypedDict

try:
    from typing import NotRequired
except ImportError:  # Python < 3.11
    from typing_extensions import NotRequired


# --- Enums ---

class MessageType(IntEnum):
    USER = 1
    BOT = 2


class MessageState(IntEnum):
    NEW = 0
    GENERATING = 1
    FINISH = 2


class MessageItemType(IntEnum):
    TEXT = 1
    IMAGE = 2
    VOICE = 3
    FILE = 4
    VIDEO = 5


# --- Nested structures ---

class BaseInfo(TypedDict):
    channel_version: str


class TextItem(TypedDict):
    text: str


class CDNMedia(TypedDict):
    encrypt_query_param: NotRequired[str]
    aes_key: NotRequired[str]
    encrypt_type: NotRequired[int]
    full_url: NotRequired[str]


class ImageItem(TypedDict):
    media: CDNMedia
    aeskey: NotRequired[str]  # hex-encoded AES key (preferred over media.aes_key)
    url: NotRequired[str]


class VoiceItem(TypedDict):
    media: CDNMedia
    text: NotRequired[str]
    playtime: NotRequired[int]


class FileItem(TypedDict):
    media: CDNMedia
    file_name: NotRequired[str]


class VideoItem(TypedDict):
    media: CDNMedia
    play_length: NotRequired[int]


class MessageItem(TypedDict):
    type: MessageItemType
    text_item: NotRequired[TextItem]
    image_item: NotRequired[ImageItem]
    voice_item: NotRequired[VoiceItem]
    file_item: NotRequired[FileItem]
    video_item: NotRequired[VideoItem]


# --- Top-level message ---

class WeixinMessage(TypedDict):
    message_id: int
    from_user_id: str
    to_user_id: str
    client_id: str
    create_time_ms: int
    message_type: MessageType
    message_state: MessageState
    context_token: str
    item_list: list[MessageItem]
    # Group chat fields (present when message is from a group)
    room_id: NotRequired[str]
    chat_room_id: NotRequired[str]
    # @mention list (some iLink versions)
    at_user_list: NotRequired[list[str]]


# --- API request/response ---

class GetUpdatesResponse(TypedDict):
    msgs: list[WeixinMessage]
    get_updates_buf: str
    sync_buf: NotRequired[str]
    ret: NotRequired[int]
    longpolling_timeout_ms: NotRequired[int]
    errcode: NotRequired[int]
    errmsg: NotRequired[str]


class SendMessageBody(TypedDict):
    from_user_id: str
    to_user_id: str
    client_id: str
    message_type: MessageType
    message_state: MessageState
    context_token: str
    item_list: list[MessageItem]


class GetConfigResponse(TypedDict):
    typing_ticket: NotRequired[str]
    ret: NotRequired[int]
    errcode: NotRequired[int]
    errmsg: NotRequired[str]


class QrCodeResponse(TypedDict):
    qrcode: str
    qrcode_img_content: str


class QrStatusResponse(TypedDict):
    status: Literal["wait", "scaned", "scaned_but_redirect", "confirmed", "expired"]
    bot_token: NotRequired[str]
    ilink_bot_id: NotRequired[str]
    ilink_user_id: NotRequired[str]
    baseurl: NotRequired[str]
    redirect_host: NotRequired[str]  # new base URL when status=scaned_but_redirect
