"""Entry point: python -m wechat_bridge"""

import asyncio
import logging
import sys


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    from wechat_bridge.bridge import run_bridge

    try:
        asyncio.run(run_bridge())
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
