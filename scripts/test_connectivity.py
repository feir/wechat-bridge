#!/usr/bin/env python3
"""Phase 1.4 connectivity test.

Usage:
    # Step 1: Login (first time only)
    python -m wechat_bridge.ilink_auth

    # Step 2: Test connectivity
    python scripts/test_connectivity.py
"""

import asyncio
import json
import sys
sys.path.insert(0, ".")

import aiohttp
from wechat_bridge.ilink_api import get_updates, send_message, build_text_message, ApiError
from wechat_bridge.ilink_auth import load_credentials


async def main() -> None:
    creds = load_credentials()
    if not creds:
        print("No credentials found. Run: python -m wechat_bridge.ilink_auth")
        sys.exit(1)

    token = creds["bot_token"]
    base_url = creds["base_url"]
    print(f"Using base_url: {base_url}")
    print(f"Token prefix: {token[:8]}...")

    async with aiohttp.ClientSession() as session:
        # Test 1: raw getupdates to see actual response format
        print("\n--- Test 1: raw getupdates ---")
        from wechat_bridge.ilink_api import build_headers, _base_info
        raw_body = {"get_updates_buf": "", "base_info": _base_info()}
        url = f"{base_url.rstrip('/')}/ilink/bot/getupdates"
        try:
            async with session.post(
                url, headers=build_headers(token), json=raw_body,
                timeout=aiohttp.ClientTimeout(total=40),
            ) as resp:
                print(f"  HTTP status: {resp.status}")
                text = await resp.text()
                # Print raw response (truncate if huge)
                print(f"  Raw response ({len(text)} bytes):")
                print(f"  {text[:500]}")
                if len(text) > 500:
                    print(f"  ... (truncated)")
                # Parse and show structure
                data = json.loads(text) if text else {}
                print(f"  Keys: {list(data.keys())}")
                print(f"  ret={data.get('ret')}, errcode={data.get('errcode')}, errmsg={data.get('errmsg')}")
                msgs = data.get("msgs", [])
                print(f"  msgs count: {len(msgs)}")
                buf = data.get("get_updates_buf", "")
                print(f"  buf: {buf[:40]}..." if buf else "  buf: (empty)")
                for i, msg in enumerate(msgs[:3]):
                    print(f"  msg[{i}]: from={msg.get('from_user_id','?')}, "
                          f"text={msg.get('item_list',[{}])[0].get('text_item',{}).get('text','')[:40]}")
        except Exception as e:
            print(f"  Error: {type(e).__name__}: {e}")

    print("\nConnectivity test complete.")


if __name__ == "__main__":
    asyncio.run(main())
