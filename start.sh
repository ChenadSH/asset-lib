#!/usr/bin/env bash
# macOS / Linux 启动脚本
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "已从 config.example.yaml 生成 config.yaml"
  echo "请把其中的 source_root 改成你的素材目录，例如："
  echo "    source_root: \"/Volumes/Design\""
  echo "或者直接用环境变量：ASSET_LIB_SOURCE=/Volumes/Design ./start.sh"
  exit 1
fi

if [ -z "${ASSET_LIB_SOURCE:-}" ] && ! grep -qE '^source_root:[[:space:]]*"?[^"[:space:]]' config.yaml; then
  echo "✗ 还没有配置素材目录（source_root 为空）"
  echo "  编辑 config.yaml，或运行：ASSET_LIB_SOURCE=/Volumes/Design ./start.sh"
  exit 1
fi

exec "$PY" -m app.cli serve
