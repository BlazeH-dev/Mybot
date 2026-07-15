# Mybot

基于 [nanobot](https://github.com/HKUDS/nanobot) v0.2.1 二次开发的个人 AI 助手。

## 技术栈

- **后端**: Python 3.11+ / nanobot 0.2.1
- **前端**: React 18 + TypeScript + Vite + Tailwind CSS
- **模型**: 内置 DeepSeek V4 与中转站 GPT-5.6 Sol/Terra/Luna 预设，可运行时切换
- **交互**: WebSocket + WebUI

## 快速开始

```bash
# 1. 创建虚拟环境并安装
python3.11 -m venv venv
source venv/bin/activate
pip install -e .

# 2. 构建前端
cd webui && bun install && bun run build && cd ..

# 3. 配置 API Key
# 编辑 ~/.nanobot/config.json，按需填写：
# providers.deepseek.apiKey     # DeepSeek V4 Pro / Flash
# providers.openai.apiKey       # GPT-5.6 中转站
# providers.openai.apiBase      # 例如 https://your-relay.example/v1
# providers.xiaomiMimo.apiKey   # 可选，仅用于 Xiaomi MiMo 语音转写

# 4. 启动
nanobot gateway
```

启动后浏览器访问 **http://127.0.0.1:8765/webui**

## 模型切换

默认模型为 `deepseek-v4-pro`。内置可切换预设：

| 预设名 | 模型 | Provider | 默认 Base URL |
|--------|------|----------|---------------|
| `deepseek-v4-pro` | `deepseek-v4-pro` | `deepseek` | `https://api.deepseek.com` |
| `deepseek-v4-flash` | `deepseek-v4-flash` | `deepseek` | `https://api.deepseek.com` |
| `gpt-5-6-sol` | `gpt-5.6-sol` | `openai` | 由 `providers.openai.apiBase` 配置 |
| `gpt-5-6-terra` | `gpt-5.6-terra` | `openai` | 由 `providers.openai.apiBase` 配置 |
| `gpt-5-6-luna` | `gpt-5.6-luna` | `openai` | 由 `providers.openai.apiBase` 配置 |

WebUI 设置页提供模型配置列表，可新增、编辑、切换和删除自定义配置；输入框右下角菜单用于快速切换。也可以发送 `/model` 查看当前模型和可用预设，例如 `/model gpt-5-6-terra`。

## 项目结构

```
├── nanobot/               # 后端核心
│   ├── agent/             # Agent 循环、工具、记忆
│   ├── channels/          # 消息通道（默认仅启用 WebSocket，其他通道代码保留）
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
