#!/usr/bin/env bash
# 停止 EdgeQuake + Webhook 接收器
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

if command -v docker >/dev/null 2>&1 && [ -f docker-compose.yml ]; then
  docker compose -f docker-compose.yml down
fi

# 同时清理可能残留的裸进程
if pgrep -f "webhook/app.py" >/dev/null 2>&1; then
  pkill -f "webhook/app.py" || true
  echo "已停止残留的 webhook 进程"
fi

echo "服务已停止"
