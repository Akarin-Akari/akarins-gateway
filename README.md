**[English](README.md)** | **[简体中文](README_CN.md)**

# Akarins Gateway

A high-performance, data-driven API gateway that unifies multiple AI backend providers behind an **OpenAI-compatible** interface. Route requests across 7+ backends with automatic fallback, circuit breaking, and IDE-aware optimizations — all configured through a single YAML file.

## Features

### Multi-Backend Routing

- **7+ backend providers** — ZeroGravity, Antigravity, Copilot, Kiro, Public Stations, and more
- **Priority-based selection** — each backend has a configurable priority (p0–p4)
- **Per-model fallback chains** — e.g. Claude Opus 4.6 can cascade through up to 16 backend steps
- **Cross-model fallback** — when all backends fail for a model, degrade to a different model family (Claude → Gemini)

### Data-Driven Configuration

- **YAML-powered routing** — all routing rules, backend capabilities, and fallback chains defined in `config/gateway.yaml`
- **Pattern matching** — `fnmatch`-style patterns for model-to-backend mapping (`claude-*haiku*`, `gemini-*`, `gpt-*`)
- **Backend capabilities declaration** — include/exclude patterns control which models each backend supports
- **Zero code changes** — add or rearrange backends by editing YAML only

### Reliability

- **Circuit breaker** — prevents cascading failures across backends
- **Exponential backoff retry** — with `Retry-After` header parsing
- **Anti-loop protection** — visited-backend tracking with max depth of 20
- **Health probing** — periodic backend availability checks

### IDE & Client Compatibility

- **Client detection** — identifies Claude Code, Cursor, Windsurf, Augment, and other IDE clients
- **Message sanitization** — cleans and normalizes requests per client type
- **SCID tracking** — session correlation ID for request tracing
- **History cache** — smart message selection for long conversations (LRU backend, 200KB body limit)
- **Tool semantic conversion** — hides Claude Code tool fingerprints from upstream providers

### Protocol Support

- **OpenAI-compatible API** — `POST /v1/chat/completions` with Bearer auth
- **Augment Code compatibility** — `POST /gateway/chat-stream` with SSE-to-NDJSON conversion
- **SSE streaming** — full Server-Sent Events support with tool name reverse mapping
- **TLS fingerprint impersonation** — via `curl_cffi` (chrome131) for anti-detection

## Architecture Deep Dive

### SCID: Session Correlation ID (Self-Developed)

Most AI IDE clients (Cursor, Windsurf, etc.) don't send a stable `conversation_id`. Akarins Gateway solves this with **SCID** — a self-developed session tracking system that generates stable identifiers even when clients provide none.

**Multi-strategy SCID generation** (7-level priority cascade):

| Priority | Source | Stability | Description |
|----------|--------|-----------|-------------|
| 1 | `X-AG-Conversation-Id` header | Highest | Client-provided, most reliable |
| 2 | `X-Conversation-Id` header | High | Alternative client header |
| 3 | `conversation_id` in body | High | Body-level identifier |
| 4 | First user message + Client IP | High | SHA256 fingerprint, checkpoint-friendly |
| 5 | First user message only | Medium | Stable across rollbacks |
| 6 | Client IP + Time window | Low | 60-min window fallback |
| 7 | Random UUID | Fallback | Last resort |

**Key design decisions**:
- **Checkpoint-friendly**: Uses the *first meaningful user message* for fingerprinting — this message rarely changes even during checkpoint rollbacks, unlike the previous "first 3 messages" approach
- **IDE metadata stripping**: Automatically strips `<user_info>`, `<environment_context>`, and OS/workspace prefixes injected by Cursor and other IDEs before generating fingerprints
- **Dynamic checkpoint intervals**: Saves stream state at adaptive intervals (every 2 chunks initially → 5 → 10) to prevent signature loss on stream interruption
- **Incremental state caching**: Real-time incremental writes instead of batch writeback on stream end

### IDE Compatibility Layer

A middleware system that detects and adapts to 11 different AI coding clients, each with different quirks and requirements.

**Supported clients**:

| Client | Detection | Sanitization | Cross-pool Fallback | State Mode |
|--------|-----------|:------------:|:-------------------:|------------|
| Claude Code | UA pattern + `anthropic-claude` | No | Yes | Signature recovery only |
| Cursor | UA `cursor/` or `go-http-client/` | Yes | No | Full SCID |
| Augment | UA `augment`/`bugment`/`vscode` + special headers | Yes | No | Full SCID |
| Windsurf | UA `windsurf/` | Yes | No | Full SCID |
| Cline | UA `cline/`/`claude-dev` | Yes | Yes | Stateless |
| Continue.dev | UA `continue/` | Yes | Yes | Stateless |
| Aider | UA `aider/` | Yes | Yes | Stateless |
| Zed | UA `zed/` | Yes | No | Full SCID |
| GitHub Copilot | UA `github-copilot` | Yes | No | Full SCID |
| OpenAI SDK | UA `openai-python/`/`openai-node/` | No | Yes | Stateless |

**Per-client behavior adaptation**:
- **Message sanitization**: IDE clients may corrupt `thinking` blocks — the middleware intercepts and cleans requests before they reach the routing layer
- **Stateless mode**: CLI tools (Cline, Aider, Continue.dev) manage their own state, so the gateway bypasses SCID session tracking entirely
- **Signature recovery only**: Claude Code needs thinking signature recovery but not full SCID state management — a lightweight hybrid mode
- **Cross-pool fallback**: Allowed for stateless CLI tools; blocked for stateful IDE clients to prevent session corruption

### Augment Code Protocol Bridge

A full protocol translation layer that makes the gateway compatible with [Augment Code](https://www.augmentcode.com/) (internally called "Bugment") alongside the standard OpenAI interface.

```
Augment Client                          Gateway                         Upstream LLM
    │                                      │                                │
    │  POST /gateway/chat-stream           │                                │
    │  (Augment protocol: nodes,           │                                │
    │   chat_history, tool_definitions)    │                                │
    │─────────────────────────────────────>│                                │
    │                                      │  1. Parse Bugment protocol     │
    │                                      │  2. Convert nodes → messages   │
    │                                      │  3. Apply Bugment State        │
    │                                      │     fallback (chat_history,    │
    │                                      │     model recovery)            │
    │                                      │  4. Convert tool_definitions   │
    │                                      │     → OpenAI tools format      │
    │                                      │  5. Normalize message          │
    │                                      │     structure for merge        │
    │                                      │                                │
    │                                      │  POST /v1/chat/completions     │
    │                                      │  (OpenAI format)               │
    │                                      │───────────────────────────────>│
    │                                      │                                │
    │                                      │  SSE stream response           │
    │                                      │<───────────────────────────────│
    │                                      │                                │
    │  NDJSON stream response              │  6. Convert SSE → NDJSON      │
    │  (Augment protocol: TEXT,            │  7. Map tool calls to          │
    │   TOOL_USE, STOP nodes)              │     Augment node types         │
    │<─────────────────────────────────────│                                │
```

**Key capabilities**:
- **Bugment State management**: Persists `chat_history` and `model` per conversation, automatically recovers when the client sends empty fields
- **Authoritative history unification**: Merges client-sent history with server-side authoritative history, preferring the more complete source
- **Mode-aware processing**: CHAT mode disables thinking (for fast responses); AGENT mode preserves thinking configuration
- **Tool loop support**: Converts Augment's `TOOL_RESULT` nodes back to OpenAI tool messages for multi-step tool workflows
- **Rate limiting**: Per-IP rate limiting (100 req/min) on the Augment endpoint

## Quick Start

### Prerequisites

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Installation

```bash
# Clone the repository
git clone https://github.com/Akarin-Akari/akarins-gateway.git
cd akarins-gateway

# Install with uv (recommended)
uv sync

# Or install with pip
pip install -e .
```

### Configuration

1. Copy the environment template:

```bash
cp .env.example .env
```

2. Edit `.env` with your settings:

```env
# Server
HOST=0.0.0.0
PORT=7861
API_PASSWORD=your-api-password

# Proxy (optional)
SOCKS5_PROXY=socks5://127.0.0.1:1080

# TLS Fingerprint
TLS_IMPERSONATE=chrome131
```

3. Edit `config/gateway.yaml` to configure backends, routing rules, and fallback chains (see [Configuration Guide](#configuration-guide) below).

### Run

```bash
# Via entry point
akarins-gateway

# Or via Python module
python -m akarins_gateway.server
```

The server starts on `http://0.0.0.0:7861` by default. If the port is occupied, it auto-falls back to adjacent ports.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completions (streaming & non-streaming) |
| `GET`  | `/v1/models` | List available models across all backends |
| `POST` | `/gateway/chat-stream` | Augment Code compatible endpoint (SSE → NDJSON) |

### Example Request

```bash
curl -X POST http://localhost:7861/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-password" \
  -d '{
    "model": "claude-sonnet-4-20250514",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

## Configuration Guide

### `config/gateway.yaml` Structure

```yaml
backends:
  zerogravity:
    base_url: "https://..."
    priority: 0              # Lower = higher priority
    enabled: true
    timeout: 120
    max_retries: 2
    capabilities:
      include: ["claude-*", "gemini-*"]   # Supported model patterns
      exclude: ["*-embedding-*"]          # Excluded model patterns

routing:
  model_routing:             # Per-model routing rules
    claude-opus-4-6:
      backends: [zerogravity, antigravity, copilot, ...]  # Fallback chain
    gemini-2.5-pro:
      backends: [zerogravity, ruoli, anyrouter]

  default_routing:           # Pattern-based fallback
    - pattern: "claude-*haiku*"
      backends: [zerogravity, copilot, anyrouter]
    - pattern: "gemini-*"
      backends: [zerogravity, ruoli, anyrouter]

  cross_model_fallback:      # Cross-model degradation
    claude-opus-4-6: gemini-2.5-pro

  catch_all:                 # Final fallback
    backends: [copilot]
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `7861` | Server port |
| `API_PASSWORD` | — | Bearer token for API authentication |
| `SOCKS5_PROXY` | — | SOCKS5 proxy URL |
| `TLS_IMPERSONATE` | `chrome131` | TLS fingerprint to impersonate |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client Request                           │
│              (Claude Code / Cursor / Augment / ...)              │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                    ┌──────▼──────┐
                    │  Auth Gate  │
                    └──────┬──────┘
                           │
                ┌──────────▼──────────┐
                │  IDE Compatibility  │  Client detection, SCID,
                │     Middleware      │  message sanitization
                └──────────┬──────────┘
                           │
                  ┌────────▼────────┐
                  │  History Cache  │  Smart message selection,
                  │   & Normalize   │  tool semantic conversion
                  └────────┬────────┘
                           │
              ┌────────────▼────────────┐
              │     YAML Router         │  model_routing →
              │  (gateway.yaml driven)  │  default_routing →
              └────────────┬────────────┘  catch_all
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
    │  Backend   │   │  Backend   │   │  Backend   │   ...
    │ ZeroGrav   │   │  Copilot   │   │   Kiro     │
    │   (p0)     │   │   (p2)     │   │   (p2)     │
    └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
          │                │                │
          │         Circuit Breaker         │
          │        Retry + Backoff          │
          │       Anti-loop Guard           │
          │                │                │
          └────────────────┼────────────────┘
                           │
                    ┌──────▼──────┐
                    │   Response  │  SSE streaming,
                    │  Converter  │  SSE→NDJSON (Augment)
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │   Client    │
                    └─────────────┘
```

## Project Structure

```
akarins_gateway/
├── server.py                 # Hypercorn launcher, port pre-flight check
├── app.py                    # FastAPI app factory, lifespan, middleware
│
├── core/                     # Foundation layer
│   ├── auth.py               # API key authentication
│   ├── config.py             # Configuration loading
│   ├── constants.py          # Global constants
│   ├── httpx_client.py       # Shared HTTP client pool
│   ├── log.py                # Structured logging
│   ├── rate_limiter.py       # Rate limiting
│   ├── retry_utils.py        # Retry utilities
│   └── tls_impersonate.py    # TLS fingerprint impersonation
│
├── gateway/                  # Gateway core
│   ├── routing.py            # YAML-driven routing engine
│   ├── circuit_breaker.py    # Circuit breaker pattern
│   ├── model_registry.py     # Dynamic model discovery
│   ├── health.py             # Backend health probing
│   ├── config_loader.py      # YAML config parser
│   ├── scid.py               # Session Correlation ID
│   ├── normalization.py      # Request normalization
│   ├── concurrency.py        # Concurrency control
│   │
│   ├── backends/             # Backend implementations
│   │   ├── interface.py      # Backend protocol definition
│   │   ├── registry.py       # Backend registry
│   │   ├── zerogravity.py    # ZeroGravity backend
│   │   ├── copilot.py        # Copilot backend
│   │   ├── kiro.py           # Kiro backend
│   │   ├── antigravity/      # Antigravity backends
│   │   └── public_station/   # Public station backends
│   │
│   ├── endpoints/            # API endpoints
│   │   ├── openai.py         # /v1/chat/completions
│   │   ├── models.py         # /v1/models
│   │   ├── anthropic.py      # Anthropic-format endpoint
│   │   └── admin.py          # Admin/management endpoints
│   │
│   ├── augment/              # Augment Code integration
│   │   ├── bridge.py         # SSE→NDJSON bridge
│   │   ├── endpoints.py      # /gateway/chat-stream
│   │   └── nodes_bridge.py   # Node-style compatibility
│   │
│   └── sse/                  # Server-Sent Events
│       └── converter.py      # SSE conversion utilities
│
├── converters/               # Message & format converters
│   ├── message_converter.py  # Cross-format message conversion
│   ├── model_config.py       # Model configuration
│   ├── tool_converter.py     # Tool call conversion
│   ├── tool_semantic_converter.py  # Tool fingerprint hiding
│   ├── signature_recovery.py # Signature recovery
│   └── gemini_fix.py         # Gemini-specific fixes
│
├── cache/                    # Caching layer
│   ├── cache_facade.py       # Unified cache interface
│   ├── memory_cache.py       # In-memory LRU cache
│   ├── signature_cache.py    # Signature cache
│   ├── async_write_queue.py  # Async write queue
│   └── migration/            # Cache migration utilities
│
├── ide_compat/               # IDE compatibility layer
│   ├── middleware.py          # IDE detection middleware
│   ├── client_detector.py    # Client type identification
│   ├── history_cache.py      # Conversation history cache
│   ├── sanitizer.py          # Message sanitization
│   ├── state_manager.py      # Client state management
│   ├── hash_cache.py         # Hash-based caching
│   ├── cache_backends/       # Cache backend implementations
│   └── selection_strategies/ # Message selection strategies
│
└── augment_compat/           # Augment protocol compatibility
    ├── routes.py             # Augment route definitions
    ├── request_normalize.py  # Request normalization
    ├── ndjson.py             # NDJSON formatting
    ├── tools_bridge.py       # Tool call bridging
    └── types.py              # Augment-specific types
```

## Development

### Setup

```bash
# Install with dev dependencies
uv sync --dev

# Or
pip install -e ".[dev]"
```

### Code Quality

```bash
# Lint
ruff check .

# Format
ruff format .

# Type check
pyright
```

### Testing

```bash
pytest
```

### Build

```bash
# Build distribution
python -m build

# Or with hatchling
hatch build
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Runtime | Python >= 3.11 |
| Web Framework | FastAPI |
| ASGI Server | Hypercorn |
| HTTP Client | httpx (with SOCKS5 support) |
| TLS Impersonation | curl_cffi |
| Validation | Pydantic |
| Configuration | PyYAML |
| Database | aiosqlite (signature cache) |
| Build System | hatchling |

## License

MIT License - see [LICENSE](LICENSE) for details.

## Author

**Akari** — [GitHub](https://github.com/Akarin-Akari)
