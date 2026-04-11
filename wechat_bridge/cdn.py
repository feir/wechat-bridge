"""CDN media download and AES-128-ECB decryption for iLink Bot API.

iLink stores media as ciphertext on its CDN. Download flow:
1. Construct URL from CDNMedia.full_url or encrypt_query_param
2. Fetch raw bytes
3. AES-128-ECB decrypt with PKCS7 unpadding using resolved key
4. Save to temp file

Key encoding varies by media type (see openclaw-weixin reference):
- Image: image_item.aeskey (hex) preferred, fallback media.aes_key (base64 → 16B)
- Voice/File/Video: media.aes_key (base64 → hex string → unhex → 16B)
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as crypto_padding

log = logging.getLogger(__name__)


# --- SSRF protection ---

def is_safe_url(url: str) -> bool:
    """Reject URLs pointing to private/loopback/link-local addresses.

    Prevents SSRF via crafted CDN URLs that could probe internal services.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False
        # Resolve to IP and check
        import socket
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                log.warning("SSRF blocked: %s resolves to private IP %s", hostname, addr)
                return False
    except (ValueError, socket.gaierror) as e:
        log.warning("SSRF check failed for %s: %s", url, e)
        return False
    return True


async def download_image(
    session: aiohttp.ClientSession,
    base_url: str,
    token: str,
    image_item: dict,
) -> Path | None:
    """Download, decrypt, and save an image. Returns temp file path or None."""
    media = image_item.get("media", {})

    # Resolve download URL (full_url > url > construct from param)
    url = media.get("full_url") or image_item.get("url")
    if not url:
        param = media.get("encrypt_query_param")
        if not param:
            log.warning("Image has no download URL and no encrypt_query_param")
            return None
        url = f"{base_url}/download?encrypted_query_param={quote(param, safe='')}"

    # Resolve AES key
    aes_key = _resolve_image_key(image_item, media)

    # SSRF check
    if not is_safe_url(url):
        log.error("Image URL blocked by SSRF check: %s", url[:120])
        return None

    # Download
    try:
        data = await _download(session, url, token)
    except Exception as e:
        log.error("Image download failed: %s", e)
        return None

    if not data:
        log.warning("Image download returned empty data")
        return None

    # Decrypt if key available
    if aes_key:
        try:
            data = _decrypt_aes_ecb(data, aes_key)
        except Exception as e:
            log.warning("Image AES decrypt failed, trying raw: %s", e)
            # Fall through with raw data — might be unencrypted

    # Save to temp file
    suffix = _guess_image_ext(data)
    fd, path_str = tempfile.mkstemp(suffix=suffix, prefix="wechat_img_")
    try:
        with open(fd, "wb") as f:
            f.write(data)
    except Exception:
        Path(path_str).unlink(missing_ok=True)
        raise

    log.info("Image saved: %s (%d bytes)", path_str, len(data))
    return Path(path_str)


async def download_media(
    session: aiohttp.ClientSession,
    base_url: str,
    token: str,
    media: dict,
    *,
    suffix: str = "",
    file_name: str = "",
) -> Path | None:
    """Download, decrypt, and save a generic media file (voice/file/video).

    Args:
        media: CDNMedia dict with encrypt_query_param, aes_key, full_url.
        suffix: File extension (e.g. ".mp4", ".pdf"). Auto-detected if empty.
        file_name: Original filename from FileItem (used for suffix detection).
    """
    url = media.get("full_url")
    if not url:
        param = media.get("encrypt_query_param")
        if not param:
            log.warning("Media has no download URL and no encrypt_query_param")
            return None
        url = f"{base_url}/download?encrypted_query_param={quote(param, safe='')}"

    aes_key = _parse_media_aes_key(media)

    if not is_safe_url(url):
        log.error("Media URL blocked by SSRF check: %s", url[:120])
        return None

    try:
        data = await _download(session, url, token)
    except Exception as e:
        log.error("Media download failed: %s", e)
        return None

    if not data:
        log.warning("Media download returned empty data")
        return None

    if aes_key:
        try:
            data = _decrypt_aes_ecb(data, aes_key)
        except Exception as e:
            log.warning("Media AES decrypt failed, trying raw: %s", e)

    # Determine suffix
    if not suffix and file_name:
        ext = Path(file_name).suffix
        if ext:
            suffix = ext
    if not suffix:
        suffix = _guess_media_ext(data)

    fd, path_str = tempfile.mkstemp(suffix=suffix, prefix="wechat_media_")
    try:
        with open(fd, "wb") as f:
            f.write(data)
    except Exception:
        Path(path_str).unlink(missing_ok=True)
        raise

    log.info("Media saved: %s (%d bytes)", path_str, len(data))
    return Path(path_str)


# --- Key resolution ---

def _resolve_image_key(image_item: dict, media: dict) -> bytes | None:
    """Resolve AES key for image. Returns 16-byte key or None.

    Priority: image_item.aeskey (hex) > media.aes_key (base64).
    """
    # Priority 1: image_item.aeskey — hex string → 16 bytes
    hex_key = image_item.get("aeskey")
    if hex_key and isinstance(hex_key, str):
        try:
            key = bytes.fromhex(hex_key)
            if len(key) == 16:
                return key
        except ValueError:
            log.debug("image_item.aeskey is not valid hex: %s", hex_key[:20])

    # Priority 2: media.aes_key — base64 → bytes
    return _parse_media_aes_key(media)


def _parse_media_aes_key(media: dict) -> bytes | None:
    """Parse media.aes_key. Handles both direct-16B and hex-in-base64 formats."""
    b64_key = media.get("aes_key")
    if not b64_key or not isinstance(b64_key, str):
        return None

    try:
        raw = base64.b64decode(b64_key)
    except Exception:
        return None

    # Direct 16-byte key (common for images)
    if len(raw) == 16:
        return raw

    # Hex-in-base64: base64 → 32-char hex string → unhex → 16 bytes
    # (common for voice/file/video, but some images use this too)
    if len(raw) == 32:
        try:
            return bytes.fromhex(raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            pass

    log.debug("Unexpected aes_key length after b64 decode: %d", len(raw))
    return None


# --- Download ---

_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50MB safety limit


async def _download(
    session: aiohttp.ClientSession,
    url: str,
    token: str,
) -> bytes:
    """Fetch raw bytes from CDN URL.

    Security: redirects disabled to prevent SSRF bypass (attacker could
    redirect from a safe host to 127.0.0.1). Size limited to _MAX_DOWNLOAD_BYTES.
    """
    from .ilink_api import build_headers

    async with session.get(
        url,
        headers=build_headers(token),
        timeout=aiohttp.ClientTimeout(total=30),
        allow_redirects=False,
    ) as resp:
        if resp.status in (301, 302, 303, 307, 308):
            raise RuntimeError(f"CDN redirect blocked (SSRF prevention): {resp.status}")
        if resp.status != 200:
            raise RuntimeError(f"CDN HTTP {resp.status}")
        # Size guard: reject unexpectedly large responses
        content_length = resp.content_length
        if content_length and content_length > _MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"CDN response too large: {content_length} bytes")
        data = await resp.content.read(_MAX_DOWNLOAD_BYTES + 1)
        if len(data) > _MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"CDN response exceeded {_MAX_DOWNLOAD_BYTES} byte limit")
        return data


# --- AES decrypt ---

def _decrypt_aes_ecb(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB decrypt with PKCS7 unpadding."""
    cipher = Cipher(algorithms.AES128(key), modes.ECB())
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(data) + decryptor.finalize()

    unpadder = crypto_padding.PKCS7(128).unpadder()
    return unpadder.update(decrypted) + unpadder.finalize()


# --- Helpers ---

def _guess_image_ext(data: bytes) -> str:
    """Guess image format from magic bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF" and len(data) > 11 and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"  # safe default


def _guess_media_ext(data: bytes) -> str:
    """Guess generic media format from magic bytes."""
    # Video
    if len(data) > 11 and data[4:8] == b"ftyp":
        return ".mp4"
    if data[:4] == b"\x1aE\xdf\xa3":
        return ".webm"
    # Audio
    if data[:4] == b"#!AM":  # AMR
        return ".amr"
    if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return ".mp3"
    if data[:4] == b"OggS":
        return ".ogg"
    if data[:7] == b"\x02#!SILK":
        return ".silk"  # WeChat SILK voice format
    # Document
    if data[:4] == b"%PDF":
        return ".pdf"
    if data[:4] == b"PK\x03\x04":
        return ".zip"  # Could be docx/xlsx/zip
    # Image fallback
    return _guess_image_ext(data)


# --- Media upload (encrypt + upload to CDN) ---

async def upload_media(
    session: aiohttp.ClientSession,
    base_url: str,
    token: str,
    file_path: Path,
) -> dict | None:
    """Encrypt and upload a local file to iLink CDN.

    Returns dict with upload metadata needed for send_message, or None on failure.
    Flow: generate AES key → encrypt → get upload URL → PUT ciphertext → return ref.
    """
    from .ilink_api import build_headers

    try:
        data = file_path.read_bytes()
    except OSError as e:
        log.error("Failed to read file for upload: %s", e)
        return None

    # Generate random AES-128 key
    aes_key = os.urandom(16)

    # Encrypt
    try:
        ciphertext = _encrypt_aes_ecb(data, aes_key)
    except Exception as e:
        log.error("AES encrypt failed: %s", e)
        return None

    # Get upload URL from iLink
    try:
        upload_info = await _get_upload_url(session, base_url, token, len(ciphertext))
    except Exception as e:
        log.error("Failed to get upload URL: %s", e)
        return None

    upload_url = upload_info.get("upload_url", "")
    if not upload_url:
        log.error("No upload_url in response: %s", upload_info)
        return None

    # SSRF check on upload URL (attacker-controlled API could return internal URL)
    if not is_safe_url(upload_url):
        log.error("Upload URL blocked by SSRF check: %s", upload_url[:120])
        return None

    # Upload ciphertext
    try:
        async with session.put(
            upload_url,
            data=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            timeout=aiohttp.ClientTimeout(total=60),
            allow_redirects=False,
        ) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                log.error("CDN upload redirect blocked (SSRF prevention): %d", resp.status)
                return None
            if resp.status not in (200, 201, 204):
                log.error("CDN upload failed: HTTP %d", resp.status)
                return None
    except Exception as e:
        log.error("CDN upload error: %s", e)
        return None

    log.info("Media uploaded: %s (%d bytes cipher)", file_path.name, len(ciphertext))

    return {
        "aes_key": base64.b64encode(aes_key).decode(),
        "encrypt_query_param": upload_info.get("encrypt_query_param", ""),
        "file_size": len(data),
        "encrypt_type": 1,
    }


def _encrypt_aes_ecb(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB encrypt with PKCS7 padding."""
    padder = crypto_padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()

    cipher = Cipher(algorithms.AES128(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


async def _get_upload_url(
    session: aiohttp.ClientSession,
    base_url: str,
    token: str,
    file_size: int,
) -> dict:
    """Request an upload URL from iLink API."""
    from .ilink_api import build_headers, _base_info

    url = f"{base_url.rstrip('/')}/ilink/bot/getuploadurl"
    body = {
        "file_size": file_size,
        "base_info": _base_info(),
    }
    async with session.post(
        url,
        headers=build_headers(token),
        json=body,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        import json as _json
        payload = _json.loads(await resp.text())
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"getuploadurl HTTP {resp.status}: {payload}")
        return payload
