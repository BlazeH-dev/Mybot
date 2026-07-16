#!/bin/zsh

set -e

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
  echo "未找到项目虚拟环境：$PROJECT_DIR/venv"
  echo "请先在项目目录创建 venv 后再运行。"
  read -r "?按回车键关闭窗口..."
  exit 1
fi

source "$PROJECT_DIR/venv/bin/activate"
echo "正在启动 Mybot gateway..."
echo "WebUI: http://127.0.0.1:8765/webui"
exec nanobot gateway
