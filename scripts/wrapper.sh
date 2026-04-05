#!/usr/bin/env bash
# systemd/cron wrapper for wechat-bridge
# Usage: wrapper.sh [instance-name]
#   wrapper.sh          → loads .env (default instance)
#   wrapper.sh xiaonuo  → loads .env.xiaonuo
set -euo pipefail

export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

INSTANCE="${1:-}"
CONF_DIR=~/.config/wechat-bridge

# Load instance-specific or default .env
set -a
if [ -n "$INSTANCE" ] && [ -f "$CONF_DIR/.env.$INSTANCE" ]; then
    source "$CONF_DIR/.env.$INSTANCE"
elif [ -f "$CONF_DIR/.env" ]; then
    source "$CONF_DIR/.env"
fi
set +a

cd ~/projects/wechat-bridge
exec python3 -m wechat_bridge
