# Mybot

基于 [nanobot](https://github.com/HKUDS/nanobot) v0.2.1 二次开发的个人 AI 助手。

## 技术栈

- **后端**: Python 3.11+ / nanobot 0.2.1
- **前端**: React 18 + TypeScript + Vite + Tailwind CSS
- **模型**: DeepSeek (`deepseek-chat`)
- **交互**: WebSocket + WebUI

## 快速开始

```bash
# 1. 创建虚拟环境并安装
python3.11 -m venv venv
source venv/bin/activate
pip install -e .

# 2. 构建前端
cd webui && bun install && bun run build && cd ..

# 3. 配置 DeepSeek API Key
# 编辑 ~/.nanobot/config.json，在 providers.deepseek.apiKey 中填入你的 key

# 4. 启动
nanobot gateway
```

启动后浏览器访问 **http://127.0.0.1:8765/webui**

## 项目结构

```
├── nanobot/               # 后端核心
│   ├── agent/             # Agent 循环、工具、记忆
│   ├── channels/          # 消息通道（仅 WebSocket）
│   ├── providers/         # LLM 提供商
│   ├── config/            # 配置管理
│   └── cli/               # CLI 入口
├── webui/                 # 前端 React + Vite + Tailwind
├── tests/                 # 测试
├── pyproject.toml
├── hatch_build.py
└── LICENSE                # MIT
```

## 与原 nanobot 的区别


## 许可证

基于 nanobot，沿用 [MIT License](LICENSE)。
