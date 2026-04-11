"""QR code login flow + credential persistence.

Usage (standalone):
    python -m wechat_bridge.ilink_auth

Flow:
    1. GET /ilink/bot/get_bot_qrcode?bot_type=3 → qrcode + qrcode_img_content (base64 PNG)
    2. Display QR in terminal (or save to file)
    3. Poll GET /ilink/bot/get_qrcode_status?qrcode=<qr> every 2s
       Status machine: wait → scaned → confirmed → expired
    4. On confirmed: extract bot_token + baseurl → save to credentials.json
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from pathlib import Path

import aiohttp

from . import config, ilink_api

log = logging.getLogger(__name__)


def _creds_path(path: Path | None = None) -> Path:
    """Resolve credentials file path: explicit arg > config."""
    return path if path is not None else config.CREDENTIALS_FILE


def save_credentials(data: dict[str, str], path: Path | None = None) -> Path:
    """Persist credentials to disk."""
    target = _creds_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2))
    target.chmod(0o600)
    log.info("Credentials saved to %s", target)
    return target


def load_credentials(path: Path | None = None) -> dict[str, str] | None:
    """Load credentials from disk, or None if absent/corrupt."""
    target = _creds_path(path)
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text())
        if data.get("bot_token") and data.get("base_url"):
            return data
        return None
    except (json.JSONDecodeError, KeyError):
        return None


def _display_qr(qr_img_content: str) -> None:
    """Display QR code. Handles both URL and base64 PNG formats."""
    if qr_img_content.startswith("http"):
        # iLink returns a URL — print it for the user to open
        print(f"\n  Scan this QR link with WeChat:")
        print(f"  {qr_img_content}\n")
        try:
            import qrcode as qr_lib  # type: ignore[import-untyped]
            qr = qr_lib.QRCode(error_correction=qr_lib.constants.ERROR_CORRECT_L)
            qr.add_data(qr_img_content)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            pass
    else:
        # Legacy: base64 PNG
        try:
            png_data = base64.b64decode(qr_img_content)
            creds_dir = config.CREDENTIALS_FILE.parent
            creds_dir.mkdir(parents=True, exist_ok=True)
            out = creds_dir / "login_qr.png"
            out.write_bytes(png_data)
            print(f"\n  QR code saved to: {out}")
            print("  Open the file and scan with WeChat.\n")
        except Exception:
            print(f"  QR data: {qr_img_content[:80]}...")


async def login(
    base_url: str = ilink_api.DEFAULT_BASE_URL,
    credentials_path: Path | None = None,
) -> dict[str, str]:
    """Interactive QR login. Returns credentials dict."""
    async with aiohttp.ClientSession() as session:
        # Step 1: Get QR code
        qr_resp = await ilink_api.fetch_qr_code(session, base_url)
        qrcode_id = qr_resp["qrcode"]
        qr_img = qr_resp.get("qrcode_img_content", "")

        print("\n=== WeChat iLink Bot Login ===")
        if qr_img:
            _display_qr(qr_img)
        else:
            print(f"  QR code ID: {qrcode_id}")
            print("  (No image data returned, use QR ID manually)")

        print("Waiting for QR scan...")

        # Step 2: Poll status
        while True:
            status_resp = await ilink_api.poll_qr_status(session, base_url, qrcode_id)
            status = status_resp["status"]

            if status == "wait":
                pass  # still waiting
            elif status == "scaned":
                print("  QR scanned! Waiting for confirmation...")
            elif status == "scaned_but_redirect":
                # iLink backend migration: switch to new host and re-poll
                new_host = status_resp.get("redirect_host", "")
                if new_host:
                    if new_host.startswith("http://"):
                        log.error("QR redirect rejected: HTTP not allowed (got %s)", new_host)
                        print("  Error: redirect host uses HTTP, HTTPS required.")
                        sys.exit(1)
                    base_url = new_host if new_host.startswith("https://") else f"https://{new_host}"
                    print(f"  Redirected to {base_url}, continuing...")
                    log.info("QR login redirect to %s", base_url)
            elif status == "confirmed":
                creds = {
                    "bot_token": status_resp["bot_token"],
                    "base_url": status_resp.get("baseurl", base_url),
                    "bot_id": status_resp.get("ilink_bot_id", ""),
                    "user_id": status_resp.get("ilink_user_id", ""),
                }
                save_credentials(creds, credentials_path)
                print(f"  Login successful! Bot ID: {creds['bot_id']}")
                return creds
            elif status == "expired":
                print("  QR code expired. Please restart login.")
                sys.exit(1)
            else:
                log.warning("Unknown QR status: %s", status)

            await asyncio.sleep(2)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    target = config.CREDENTIALS_FILE
    creds = load_credentials(target)
    if creds:
        print(f"Existing credentials found at {target} (bot_id={creds.get('bot_id', '?')})")
        print("Re-running login to refresh...")
    await login(credentials_path=target)


if __name__ == "__main__":
    asyncio.run(_main())
