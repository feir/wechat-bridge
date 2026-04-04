#!/usr/bin/env bash
# systemd/cron wrapper for wechat-bridge
set -euo pipefail

export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Load .env
set -a
[ -f ~/.config/wechat-bridge/.env ] && source ~/.config/wechat-bridge/.env
set +a

cd ~/projects/wechat-bridge
exec python3 -m wechat_bridge
