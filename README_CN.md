**[English](README.md)** | **[简体中文](README_CN.md)**

# Akarins Gateway

高性能、数据驱动的 API 网关，将多个 AI 后端供应商统一在 **OpenAI 兼容**接口之后。通过单一 YAML 文件配置，实现跨 7+ 后端的请求路由、自动降级、熔断保护和 IDE 感知优化。

## 特性

### 多后端路由

- **7+ 后端供应商** — ZeroGravity、Antigravity、Copilot、Kiro、公共站点等
- **优先级调度** — 每个后端可配置优先级（p0–p4）
- **按模型降级链** — 例如 Claude Opus 4.6 可级联最多 16 个后端步骤
- **跨模型降级** — 当某模型的所有后端均失败时，自动切换到其他模型族（Claude → Gemini）

### 数据驱动配置

- **YAML 驱动路由** — 所有路由规则、后端能力声明、降级链均在 `config/gateway.yaml` 中定义
- **模式匹配** — `fnmatch` 风格通配符（`claude-*haiku*`、`gemini-*`、`gpt-*`）
- **后端能力声明** — include/exclude 模式控制每个后端支持的模型范围
- **零代码变更** — 添加或调整后端仅需编辑 YAML

### 可靠性

- **熔断器** — 防止后端故障级联扩散
- **指数退避重试** — 支持解析 `Retry-After` 响应头
- **防环保护** — 已访问后端追踪，最大深度 20 层
- **健康探测** — 定期检测后端可用性

### IDE 与客户端兼容

- **客户端识别** — 自动检测 Claude Code、Cursor、Windsurf、Augment 等 IDE 客户端
- **消息清洗** — 按客户端类型清理和规范化请求
- **SCID 追踪** — 会话关联 ID，用于请求链路追踪
- **历史缓存** — 长对话智能消息选择（LRU 后端，200KB 请求体上限）
- **工具语义转换** — 向上游隐藏 Claude Code 的工具指纹特征

### 协议支持

- **OpenAI 兼容 API** — `POST /v1/chat/completions`，Bearer Token 认证
- **Augment Code 兼容** — `POST /gateway/chat-stream`，SSE 转 NDJSON
- **SSE 流式传输** — 完整的 Server-Sent Events 支持，含工具名反向映射
- **TLS 指纹伪装** — 通过 `curl_cffi`（chrome131）实现反检测

## 架构深入解析

### SCID：会话关联标识符（自研）

大多数 AI IDE 客户端（Cursor、Windsurf 等）不会发送稳定的 `conversation_id`。Akarins Gateway 通过自研的 **SCID** 系统解决这一问题——即使客户端未提供任何标识，也能生成稳定的会话标识符。

**多策略 SCID 生成**（7 级优先级瀑布）：

| 优先级 | 来源 | 稳定性 | 说明 |
|--------|------|--------|------|
| 1 | `X-AG-Conversation-Id` 请求头 | 最高 | 客户端提供，最可靠 |
| 2 | `X-Conversation-Id` 请求头 | 高 | 备选客户端请求头 |
| 3 | 请求体中的 `conversation_id` | 高 | Body 级标识符 |
| 4 | 首条用户消息 + 客户端 IP | 高 | SHA256 指纹，对 checkpoint 友好 |
| 5 | 仅首条用户消息 | 中 | 跨回滚稳定 |
| 6 | 客户端 IP + 时间窗口 | 低 | 60 分钟窗口兜底 |
| 7 | 随机 UUID | 兜底 | 最后手段 |

**关键设计决策**：
- **Checkpoint 友好**：使用*第一条有效业务用户消息*生成指纹——该消息即使在 checkpoint 回退时也很少变化，相比之前的"前 3 条消息"方案更稳定
- **IDE 元信息清洗**：自动剥离 Cursor 等 IDE 注入的 `<user_info>`、`<environment_context>` 和 OS/工作区前缀后再生成指纹
- **动态检查点间隔**：自适应保存流状态（初始每 2 个 chunk → 5 → 10），防止流中断导致签名丢失
- **增量状态缓存**：实时增量写入，而非流结束时批量回写

### IDE 兼容层

一套中间件系统，可检测并适配 11 种不同的 AI 编程客户端，每种客户端都有不同的特性和需求。

**支持的客户端**：

| 客户端 | 检测方式 | 消息净化 | 跨池降级 | 状态模式 |
|--------|---------|:-------:|:-------:|----------|
| Claude Code | UA 模式 + `anthropic-claude` | 否 | 是 | 仅签名恢复 |
| Cursor | UA `cursor/` 或 `go-http-client/` | 是 | 否 | 完整 SCID |
| Augment | UA `augment`/`bugment`/`vscode` + 特殊请求头 | 是 | 否 | 完整 SCID |
| Windsurf | UA `windsurf/` | 是 | 否 | 完整 SCID |
| Cline | UA `cline/`/`claude-dev` | 是 | 是 | 无状态 |
| Continue.dev | UA `continue/` | 是 | 是 | 无状态 |
| Aider | UA `aider/` | 是 | 是 | 无状态 |
| Zed | UA `zed/` | 是 | 否 | 完整 SCID |
| GitHub Copilot | UA `github-copilot` | 是 | 否 | 完整 SCID |
| OpenAI SDK | UA `openai-python/`/`openai-node/` | 否 | 是 | 无状态 |

**按客户端行为自适应**：
- **消息净化**：IDE 客户端可能破坏 `thinking` 块——中间件在请求到达路由层之前拦截并清洗
- **无状态模式**：CLI 工具（Cline、Aider、Continue.dev）自行管理状态，网关完全绕过 SCID 会话追踪
- **仅签名恢复**：Claude Code 需要 thinking 签名恢复但不需要完整 SCID 状态管理——一种轻量级混合模式
- **跨池降级**：无状态 CLI 工具允许跨池降级；有状态 IDE 客户端禁止，以防止会话损坏

### Augment Code 协议桥接

一个完整的协议翻译层，使网关能同时兼容 [Augment Code](https://www.augmentcode.com/)（内部代号 "Bugment"）和标准 OpenAI 接口。

```
Augment 客户端                         网关                            上游 LLM
    │                                    │                                │
    │  POST /gateway/chat-stream         │                                │
    │  (Augment 协议: nodes,             │                                │
    │   chat_history, tool_definitions)  │                                │
    │───────────────────────────────────>│                                │
    │                                    │  1. 解析 Bugment 协议          │
    │                                    │  2. 转换 nodes → messages      │
    │                                    │  3. 应用 Bugment State         │
    │                                    │     降级恢复 (chat_history,    │
    │                                    │     model 补全)                │
    │                                    │  4. 转换 tool_definitions      │
    │                                    │     → OpenAI tools 格式        │
    │                                    │  5. 规范化消息结构             │
    │                                    │     (确保 merge 兼容)          │
    │                                    │                                │
    │                                    │  POST /v1/chat/completions     │
    │                                    │  (OpenAI 格式)                 │
    │                                    │───────────────────────────────>│
    │                                    │                                │
    │                                    │  SSE 流式响应                  │
    │                                    │<───────────────────────────────│
    │                                    │                                │
    │  NDJSON 流式响应                   │  6. 转换 SSE → NDJSON          │
    │  (Augment 协议: TEXT,              │  7. 映射工具调用到             │
    │   TOOL_USE, STOP 节点)             │     Augment 节点类型           │
    │<───────────────────────────────────│                                │
```

**核心能力**：
- **Bugment State 管理**：按会话持久化 `chat_history` 和 `model`，客户端发送空字段时自动恢复
- **权威历史统一**：合并客户端发送的历史与服务端权威历史，优先使用更完整的来源
- **模式感知处理**：CHAT 模式禁用 thinking（快速响应）；AGENT 模式保留 thinking 配置
- **Tool Loop 支持**：将 Augment 的 `TOOL_RESULT` 节点转回 OpenAI 工具消息，支持多步骤工具调用工作流
- **限流保护**：Augment 端点按 IP 限流（100 请求/分钟）

## 快速开始

### 前置要求

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip

### 安装

```bash
# 克隆仓库
git clone https://github.com/Akarin-Akari/akarins-gateway.git
cd akarins-gateway

# 使用 uv 安装（推荐）
uv sync

# 或使用 pip
pip install -e .
```

### 配置

1. 复制环境变量模板：

```bash
cp .env.example .env
```

2. 编辑 `.env` 填入你的配置：

```env
# 服务器
HOST=0.0.0.0
PORT=7861
API_PASSWORD=your-api-password

# 代理（可选）
SOCKS5_PROXY=socks5://127.0.0.1:1080

# TLS 指纹
TLS_IMPERSONATE=chrome131
```

3. 编辑 `config/gateway.yaml` 配置后端、路由规则和降级链（详见下方[配置指南](#配置指南)）。

### 启动

```bash
# 通过入口点启动
akarins-gateway

# 或通过 Python 模块启动
python -m akarins_gateway.server
```

服务器默认启动在 `http://0.0.0.0:7861`。如果端口被占用，会自动尝试相邻端口。

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/chat/completions` | OpenAI 兼容的对话补全（支持流式与非流式） |
| `GET`  | `/v1/models` | 列出所有后端的可用模型 |
| `POST` | `/gateway/chat-stream` | Augment Code 兼容端点（SSE → NDJSON） |

### 请求示例

```bash
curl -X POST http://localhost:7861/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-password" \
  -d '{
    "model": "claude-sonnet-4-20250514",
    "messages": [{"role": "user", "content": "你好！"}],
    "stream": true
  }'
```

## 配置指南

### `config/gateway.yaml` 结构

```yaml
backends:
  zerogravity:
    base_url: "https://..."
    priority: 0              # 数值越小优先级越高
    enabled: true
    timeout: 120
    max_retries: 2
    capabilities:
      include: ["claude-*", "gemini-*"]   # 支持的模型模式
      exclude: ["*-embedding-*"]          # 排除的模型模式

routing:
  model_routing:             # 按模型的路由规则
    claude-opus-4-6:
      backends: [zerogravity, antigravity, copilot, ...]  # 降级链
    gemini-2.5-pro:
      backends: [zerogravity, ruoli, anyrouter]

  default_routing:           # 基于模式匹配的默认路由
    - pattern: "claude-*haiku*"
      backends: [zerogravity, copilot, anyrouter]
    - pattern: "gemini-*"
      backends: [zerogravity, ruoli, anyrouter]

  cross_model_fallback:      # 跨模型降级
    claude-opus-4-6: gemini-2.5-pro

  catch_all:                 # 兜底路由
    backends: [copilot]
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOST` | `0.0.0.0` | 服务器绑定地址 |
| `PORT` | `7861` | 服务器端口 |
| `API_PASSWORD` | — | API 认证 Bearer Token |
| `SOCKS5_PROXY` | — | SOCKS5 代理地址 |
| `TLS_IMPERSONATE` | `chrome131` | 要伪装的 TLS 指纹 |

## 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         客户端请求                                │
│              (Claude Code / Cursor / Augment / ...)              │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                    ┌──────▼──────┐
                    │   认证门控   │
                    └──────┬──────┘
                           │
                ┌──────────▼──────────┐
                │   IDE 兼容中间件     │  客户端检测、SCID、
                │                     │  消息清洗
                └──────────┬──────────┘
                           │
                  ┌────────▼────────┐
                  │   历史缓存 &    │  智能消息选择、
                  │   请求规范化     │  工具语义转换
                  └────────┬────────┘
                           │
              ┌────────────▼────────────┐
              │      YAML 路由引擎      │  model_routing →
              │  (gateway.yaml 驱动)    │  default_routing →
              └────────────┬────────────┘  catch_all
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
    │   后端     │   │   后端     │   │   后端     │   ...
    │ ZeroGrav  │   │  Copilot  │   │   Kiro    │
    │   (p0)    │   │   (p2)    │   │   (p2)    │
    └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
          │                │                │
          │           熔断器保护             │
          │          重试 + 退避             │
          │           防环守卫              │
          │                │                │
          └────────────────┼────────────────┘
                           │
                    ┌──────▼──────┐
                    │  响应转换器   │  SSE 流式传输、
                    │             │  SSE→NDJSON (Augment)
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │    客户端    │
                    └─────────────┘
```

## 项目结构

```
akarins_gateway/
├── server.py                 # Hypercorn 启动器，端口预检
├── app.py                    # FastAPI 应用工厂，生命周期管理，中间件
│
├── core/                     # 基础设施层
│   ├── auth.py               # API Key 认证
│   ├── config.py             # 配置加载
│   ├── constants.py          # 全局常量
│   ├── httpx_client.py       # 共享 HTTP 客户端池
│   ├── log.py                # 结构化日志
│   ├── rate_limiter.py       # 限流器
│   ├── retry_utils.py        # 重试工具
│   └── tls_impersonate.py    # TLS 指纹伪装
│
├── gateway/                  # 网关核心
│   ├── routing.py            # YAML 驱动的路由引擎
│   ├── circuit_breaker.py    # 熔断器模式
│   ├── model_registry.py     # 动态模型发现
│   ├── health.py             # 后端健康探测
│   ├── config_loader.py      # YAML 配置解析器
│   ├── scid.py               # 会话关联 ID
│   ├── normalization.py      # 请求规范化
│   ├── concurrency.py        # 并发控制
│   │
│   ├── backends/             # 后端实现
│   │   ├── interface.py      # 后端协议定义
│   │   ├── registry.py       # 后端注册中心
│   │   ├── zerogravity.py    # ZeroGravity 后端
│   │   ├── copilot.py        # Copilot 后端
│   │   ├── kiro.py           # Kiro 后端
│   │   ├── antigravity/      # Antigravity 系列后端
│   │   └── public_station/   # 公共站点后端
│   │
│   ├── endpoints/            # API 端点
│   │   ├── openai.py         # /v1/chat/completions
│   │   ├── models.py         # /v1/models
│   │   ├── anthropic.py      # Anthropic 格式端点
│   │   └── admin.py          # 管理端点
│   │
│   ├── augment/              # Augment Code 集成
│   │   ├── bridge.py         # SSE→NDJSON 桥接
│   │   ├── endpoints.py      # /gateway/chat-stream
│   │   └── nodes_bridge.py   # Node 风格兼容
│   │
│   └── sse/                  # Server-Sent Events
│       └── converter.py      # SSE 转换工具
│
├── converters/               # 消息与格式转换器
│   ├── message_converter.py  # 跨格式消息转换
│   ├── model_config.py       # 模型配置
│   ├── tool_converter.py     # 工具调用转换
│   ├── tool_semantic_converter.py  # 工具指纹隐藏
│   ├── signature_recovery.py # 签名恢复
│   └── gemini_fix.py         # Gemini 专项修复
│
├── cache/                    # 缓存层
│   ├── cache_facade.py       # 统一缓存接口
│   ├── memory_cache.py       # 内存 LRU 缓存
│   ├── signature_cache.py    # 签名缓存
│   ├── async_write_queue.py  # 异步写入队列
│   └── migration/            # 缓存迁移工具
│
├── ide_compat/               # IDE 兼容层
│   ├── middleware.py          # IDE 检测中间件
│   ├── client_detector.py    # 客户端类型识别
│   ├── history_cache.py      # 对话历史缓存
│   ├── sanitizer.py          # 消息清洗器
│   ├── state_manager.py      # 客户端状态管理
│   ├── hash_cache.py         # 哈希缓存
│   ├── cache_backends/       # 缓存后端实现
│   └── selection_strategies/ # 消息选择策略
│
└── augment_compat/           # Augment 协议兼容
    ├── routes.py             # Augment 路由定义
    ├── request_normalize.py  # 请求规范化
    ├── ndjson.py             # NDJSON 格式化
    ├── tools_bridge.py       # 工具调用桥接
    └── types.py              # Augment 专用类型
```

## 开发

### 环境搭建

```bash
# 安装开发依赖
uv sync --dev

# 或
pip install -e ".[dev]"
```

### 代码质量

```bash
# 代码检查
ruff check .

# 代码格式化
ruff format .

# 类型检查
pyright
```

### 测试

```bash
pytest
```

### 构建

```bash
# 构建分发包
python -m build

# 或使用 hatchling
hatch build
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 运行时 | Python >= 3.11 |
| Web 框架 | FastAPI |
| ASGI 服务器 | Hypercorn |
| HTTP 客户端 | httpx（支持 SOCKS5） |
| TLS 伪装 | curl_cffi |
| 数据校验 | Pydantic |
| 配置管理 | PyYAML |
| 数据库 | aiosqlite（签名缓存） |
| 构建系统 | hatchling |

## 许可证

MIT License - 详见 [LICENSE](LICENSE)。

## 作者

**Akari** — [GitHub](https://github.com/Akarin-Akari)
