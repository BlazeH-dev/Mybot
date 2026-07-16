#!/bin/zsh

set -e

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
  echo "未找到项目虚拟环境：$PROJECT_DIR/venv" >&2
  echo "请先在项目目录创建 venv 后再运行。" >&2
  exit 1
fi

source "$PROJECT_DIR/venv/bin/activate"
exec nanobot gateway
