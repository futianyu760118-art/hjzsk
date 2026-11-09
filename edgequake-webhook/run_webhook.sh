#!/usr/bin/env bash
# 启动 EdgeQuake Webhook 接收服务（前台运行）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo "错误：缺少 .env 文件" >&2
  echo "请先执行：cp .env.example .env 并填写 GITEE_TOKEN / GITEE_WEBHOOK_TOKEN" >&2
  exit 1
fi

PY="${PYTHON_BIN:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

mkdir -p logs state workspace

exec "$PY" webhook/app.py
