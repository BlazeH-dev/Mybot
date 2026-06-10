This file provides guidance to AI coding agents working with this repository.
**每次修改项目后，请同步更新此文件以保持最新。**

## 项目概述

Mybot 是基于 [nanobot](https://github.com/HKUDS/nanobot) v0.2.1 二次开发的个人 AI 助手，
使用 DeepSeek 模型 + WebSocket WebUI 交互，已精简为纯本地开发项目。

## 开发命令

```bash
# 启动后端网关（生产模式，需先构建前端）
source venv/bin/activate
nanobot gateway

# 前端开发（热更新）
cd webui && bun run dev

# 前端构建（输出到 ../nanobot/web/dist/）
cd webui && bun run build

# 测试 / 代码检查
pytest tests/ -v
ruff check nanobot/
```

## 项目结构

```
├── nanobot/               # 后端核心（基于 nanobot 二次开发）
│   ├── agent/             # Agent 循环、工具、记忆
│   ├── channels/          # 消息通道（仅启用 WebSocket）
│   ├── providers/         # LLM 提供商
│   ├── config/            # 配置管理（~/.nanobot/config.json）
│   ├── cli/               # CLI 入口
│   ├── api/               # OpenAI 兼容 API 服务
│   └── ...
├── webui/                 # 前端 React + Vite + Tailwind
├── tests/                 # 测试
├── pyproject.toml         # Python 项目配置
├── hatch_build.py         # 构建钩子（含前端构建逻辑）
├── README.md
├── LICENSE                # MIT
├── THIRD_PARTY_NOTICES.md
└── AGENTS.md              # 本文件
```

## 当前配置

- **模型**: DeepSeek (`deepseek-chat`, provider: deepseek, apiType: auto)
- **通道**: 仅 WebSocket（`ws://127.0.0.1:8765/`，token 认证已关闭）
- **前端地址**: `http://127.0.0.1:8765/webui`
- **健康检查**: `http://127.0.0.1:18790/health`
- **API Key**: 必须在 `~/.nanobot/config.json` 的 `providers.deepseek.apiKey` 中填写，网关不读取环境变量

## 已做的精简（与原 nanobot 的区别）

- ✅ 仅保留 WebSocket 通道，删除所有其他通道（Telegram/Discord/Slack/QQ/微信/飞书等）
- ✅ 删除 Docker 相关（Dockerfile, docker-compose.yml, entrypoint.sh）
- ✅ 删除非开发文件（docs/, images/, case/, desktop/, scripts/, bridge/ 等）
- ✅ WebSocket 关闭 token 认证（`websocketRequiresToken: false`）
- ✅ 模型固定为 DeepSeek

## 核心架构（来自 nanobot）

消息通过异步 `MessageBus`（`nanobot/bus/queue.py`）解耦通道与 Agent 核心：

1. **Channels** → 仅 WebSocket 通道，接收前端消息并发布 `InboundMessage`
2. **AgentLoop** → 消费消息、构建上下文、协调处理
3. **AgentRunner** → LLM 对话循环：发送消息、执行工具、流式响应
4. 响应作为 `OutboundMessage` 返回 WebSocket 通道

### 关键子系统

- **LLM Providers**: 所有提供商实现，DeepSeek 使用 `openai_compat` 后端
- **Tools**: 文件系统读写、Shell 执行、网页搜索、cron、子代理等
- **Memory**: 会话历史持久化，Dream 两阶段记忆整合
- **Config**: Pydantic 配置，JSON 格式，camelCase 别名

## 代码风格

- Python 3.11+, asyncio
- 行长度: 100
- Lint: `ruff` (规则 E, F, I, N, W，忽略 E501)
- pytest + `asyncio_mode = "auto"`

## 修改记录

| 日期 | 修改内容 |
|------|----------|
| 2026-06-10 | 初始化：精简通道为仅 WebSocket，配置 DeepSeek，删除 Docker/文档/示例 |
