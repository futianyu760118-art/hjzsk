#!/usr/bin/env bash
# 启动 EdgeQuake + Webhook 接收器（Docker Compose）
#
# 依赖：docker + docker compose
# 生产环境推荐改用 systemd 守护（见 deploy/ 目录）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "错误：未安装 docker" >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo "错误：缺少 .env 文件" >&2
  echo "请先执行：cp .env.example .env 并填写配置" >&2
  exit 1
fi

# 自动生成 Webhook 令牌
if ! grep -qE '^GITEE_WEBHOOK_TOKEN=.+$' .env; then
  TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))' 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -d '\n')"
  if grep -q '^GITEE_WEBHOOK_TOKEN=' .env; then
    sed -i "s|^GITEE_WEBHOOK_TOKEN=.*|GITEE_WEBHOOK_TOKEN=${TOKEN}|" .env
  else
    echo "GITEE_WEBHOOK_TOKEN=${TOKEN}" >> .env
  fi
  echo "已生成 GITEE_WEBHOOK_TOKEN: ${TOKEN}"
fi

mkdir -p logs state workspace

echo "启动服务..."
docker compose -f docker-compose.yml up -d

echo ""
echo "服务状态："
docker compose -f docker-compose.yml ps
echo ""
echo "Webhook 接收器: http://0.0.0.0:${WEBHOOK_PORT:-9000}/webhook"
echo "本机自检：curl http://127.0.0.1:9000/health"
echo ""
echo "注意：Gitee 必须能公网访问到该地址，生产请用 Nginx/Caddy 做 HTTPS 反代"
