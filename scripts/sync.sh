#!/usr/bin/env bash
# 恒剑知识库 —— 双向同步包装脚本
# 用法: ./scripts/sync.sh [--status|--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if ! command -v node >/dev/null 2>&1; then
  echo "错误: 未找到 node，请先安装 Node.js" >&2
  exit 127
fi

cd "$REPO_ROOT"
exec node "$SCRIPT_DIR/sync.js" "$@"
