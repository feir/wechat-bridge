"""Entry point: python -m wechat_bridge"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wechat-bridge",
        description="WeChat <-> Claude Code Bridge",
    )
    parser.add_argument(
        "--login", action="store_true",
        help="Run interactive QR login instead of starting the bridge",
    )
    parser.add_argument(
        "--credentials", type=Path, default=None,
        help="Path to credentials.json (overrides WECHAT_CREDENTIALS_FILE env var)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.login:
        import os
        from wechat_bridge import config
        from wechat_bridge.ilink_auth import load_credentials, login

        # Resolve credentials path: CLI flag > env var > default
        # (config.init() is NOT called here — WECHAT_ALLOWED_USERS may not be set during login)
        if args.credentials:
            config.CREDENTIALS_FILE = args.credentials
        else:
            env_path = os.environ.get("WECHAT_CREDENTIALS_FILE", "").strip()
            if env_path:
                config.CREDENTIALS_FILE = Path(env_path)

        target = config.CREDENTIALS_FILE
        creds = load_credentials(target)
        if creds:
            print(f"Existing credentials found at {target} (bot_id={creds.get('bot_id', '?')})")
            print("Re-running login to refresh...")
        asyncio.run(login(credentials_path=target))
        sys.exit(0)

    # Apply --credentials to env before config.init() runs inside run_bridge()
    if args.credentials:
        import os
        os.environ["WECHAT_CREDENTIALS_FILE"] = str(args.credentials)

    from wechat_bridge.bridge import run_bridge

    try:
        asyncio.run(run_bridge())
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
