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
import logging
import tempfile
from pathlib import Path
from urllib.parse import quote

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as crypto_padding

log = logging.getLogger(__name__)


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

async def _download(
    session: aiohttp.ClientSession,
    url: str,
    token: str,
) -> bytes:
    """Fetch raw bytes from CDN URL."""
    from .ilink_api import build_headers

    async with session.get(
        url,
        headers=build_headers(token),
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"CDN HTTP {resp.status}")
        return await resp.read()


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
