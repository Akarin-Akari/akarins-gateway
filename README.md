**[English](README.md)** | **[简体中文](README_CN.md)**

# Akarins Gateway

A high-performance, data-driven API gateway that unifies multiple AI backend providers behind an **OpenAI-compatible** interface. Route requests across 7+ backends with automatic fallback, circuit breaking, and IDE-aware optimizations — all configured through a single YAML file.

## Management Panel

A built-in web-based management panel for configuring backends, model routing, and monitoring health status in real time.

| Backends | Model Routing | Health Monitor |
|:--------:|:-------------:|:--------------:|
| ![Backends Panel](docs/screenshots/panel-backends.png) | ![Model Routing Panel](docs/screenshots/panel-model-routing.png) | ![Health Monitor Panel](docs/screenshots/panel-health-monitor.png) |

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

### Cursor Replay Interception & Authoritative History (Self-Developed)

Cursor (and similar IDE clients) sends the **entire conversation history** with every new request — essentially performing a "packet capture replay". However, during this replay process, Cursor **corrupts** the messages in ways that break Anthropic's API requirements:

| Corruption Type | What Happens | Consequence |
|----------------|-------------|-------------|
| **Thinking block mutation** | `\r\n`→`\n`, trailing space trim, content truncation | `400 Invalid signature in thinking block` — signature is bound to exact bytes |
| **Signature loss** | `thoughtSignature` field silently stripped | No way to validate thinking blocks, extended thinking disabled |
| **Tool chain breakage** | `tool_use` sent without matching `tool_result` | `400 tool_use_result_mismatch` — conversation halted |
| **No session identity** | Cursor never sends `conversation_id` | Cannot correlate requests to the same conversation |

The gateway solves all of these by becoming the **authoritative state machine** — it never trusts IDE-replayed history, maintaining its own clean copy instead.

**Core design principle**: *"The gateway is the single source of truth. Client-replayed history is treated as untrusted input."*

#### Data Flow

```
Cursor Request (with corrupted replayed history)
    │
    ├──► IDECompatMiddleware ──► Detect client type (Cursor)
    │
    ├──► SCID Generator ──► Generate stable session ID
    │    (7-level cascade)     from first meaningful user message
    │
    ├──► AnthropicSanitizer ──► 6-layer signature recovery
    │    (sanitizer.py)          ├─ Client-provided signature
    │                            ├─ Context signature
    │                            ├─ Encoded tool_id decode
    │                            ├─ Session cache (sig, text) pair
    │                            ├─ Tool cache: tool_id → sig
    │                            └─ Last signature fallback
    │                            On failure: thinking → text downgrade
    │
    ├──► StateManager.merge ──► Replace corrupted history with
    │    (state_manager.py)      authoritative version
    │                            ├─ Position+role match: use authoritative
    │                            ├─ New messages: append from client
    │                            ├─ Orphan tool_use: recover tool_result
    │                            │   from cache or authoritative history
    │                            └─ Tool chain split: preserve both sides
    │
    ├──► History Cache ──► Smart message selection
    │    (history_cache.py)  (LRU + pinned tool definitions)
    │
    ▼
Forward clean request to upstream LLM
    │
    ▼
Stream response back
    ├─ Extract & cache thinking signatures (dynamic checkpoint: 2→5→10 chunks)
    ├─ Update authoritative history with clean response
    └─ Cache tool_use_id → tool_result mapping
```

#### Five Core Mechanisms

**1. Authoritative History Storage** (`state_manager.py`)

After each successful LLM response, the gateway stores the clean, original messages as the "authoritative history" for that session. On subsequent requests, when Cursor replays its corrupted version, the gateway **replaces** it:

```python
# For positions covered by authoritative history → use clean version
if auth_msg.get("role") == client_msg.get("role"):
    merged.append(authoritative[i])  # Use gateway's clean copy
# For new messages beyond authoritative history → accept from client
for i in range(auth_len, len(client_messages)):
    merged.append(client_messages[i])
```

Key properties:
- **Dual-layer persistence**: Memory cache (fast) + SQLite (durable across restarts)
- **Auto-compress disabled**: Protects tool chain integrity — compression only triggered by upstream body-too-large errors
- **Stale SCID collision detection**: When a short new conversation accidentally matches an old SCID with heavy tool history, the state is automatically reset

**2. 6-Layer Signature Recovery** (`sanitizer.py`)

When Cursor strips thinking signatures, the gateway attempts recovery through 6 strategies before giving up:

```
Layer 1: Client-provided signature (rarely available)
Layer 2: Signature from request context
Layer 3: Decode from encoded tool_id
Layer 4: Session cache — (signature, thinking_text) pair lookup
Layer 5: Tool cache — tool_use_id → signature mapping
Layer 6: Last known signature (fallback)

All layers fail → Graceful downgrade: thinking block → text block
                  + Sync thinkingConfig to match content
```

The last 2 assistant messages receive more aggressive recovery attempts, as they're most likely to be referenced in tool call rounds.

**3. Tool Chain Integrity** (`state_manager.py`)

Cursor frequently breaks `tool_use`/`tool_result` pairs during replay. The gateway detects and repairs this:

```
Detect orphan tool_use (has tool_call_id but no matching tool_result)
    │
    ├─ Step 1: Check tool_results_cache (per-session, keyed by tool_use_id)
    │   └─ Found → Inject cached tool_result after the tool_use
    │
    └─ Step 2: Fall back to authoritative history
        └─ Search for matching tool_result in stored history
            └─ Found → Merge into message list
```

The gateway also caches every `tool_use_id → tool_result` mapping during response processing, building a safety net for future replay corruption.

**4. Incremental Stream Checkpointing** (`scid.py`)

During streaming, signatures can be lost if the stream is interrupted (network issue, client disconnect). The gateway saves state at dynamic intervals:

| Stream Phase | Chunk Range | Save Interval | Rationale |
|-------------|------------|---------------|-----------|
| Initial | 0–50 | Every 2 chunks | Signatures often appear early |
| Normal | 50–200 | Every 5 chunks | Balanced performance |
| Late | 200+ | Every 10 chunks | Reduce I/O overhead |

On the next request, if the gateway detects an incomplete session, it recovers the signature from the last checkpoint — preventing thinking from being permanently disabled.

**5. Thinking Block Downgrade** (`sanitizer.py`)

When all recovery attempts fail, the gateway performs graceful degradation rather than letting the request fail:

```
thinking block (with invalid/missing signature)
    → Converted to text block (preserving content, removing signature requirement)
    → thinkingConfig synchronized (disabled if no valid thinking blocks remain)
    → Request proceeds without 400 error
```

This ensures conversations continue even when Cursor has heavily corrupted the replay history.

### Context Management & Compression Strategy (Self-Developed)

IDE clients (especially Cursor) replay the **entire conversation history** with every request, which grows unboundedly. The gateway must keep requests within upstream token/body limits while **never breaking tool chains or losing reasoning context**. This is solved through a conservative, multi-tier compression architecture.

**Core Design Principle**: *"Never delete messages. Only compress content. Tool chain integrity is the highest priority."*

#### Why Auto-Compress Is Disabled

```python
AUTO_COMPRESS_ENABLED = False      # Disabled — protects tool chains
MAX_HISTORY_MESSAGES = 200         # Soft limit, NOT a hard cap
COMPRESSED_KEEP_MESSAGES = 150     # Only used by emergency path
TOOL_RESULT_COMPRESSION_ENABLED = False  # Routine truncation off by default
```

Deleting **any** message risks breaking `tool_use`/`tool_result` pairs, causing `400 tool_use_result_mismatch` errors. The gateway instead relies on **content-only compression** — shrinking what's inside messages without removing the messages themselves.

#### Three-Tier Compression Architecture

```
Request arrives (potentially 200+ messages)
    │
    ├─► Tier 1: Routine Tool Result Compression (DEFAULT OFF)
    │   (_compress_tool_result)
    │   ├─ Normalizes format (Gemini → Anthropic)
    │   ├─ When enabled: truncates tool results to 5000 chars
    │   ├─ Adds "[SCID compressed N chars]" marker
    │   └─ Applied during merge and history updates
    │
    ├─► Tier 2: Authoritative History Compression
    │   (_compress_authoritative_history)
    │   ├─ Triggered when history > 200 messages
    │   ├─ ⚠️ NEVER deletes messages — only compresses content
    │   ├─ Iterates all messages through _compress_tool_result
    │   └─ All messages preserved, only content truncated
    │
    └─► Tier 3: Emergency Compression (LAST RESORT)
        (trigger_emergency_compress)
        ├─ Only triggered by upstream errors:
        │   ├─ 413 Request Entity Too Large
        │   └─ 400 with "too large" / "token limit" / "context length"
        ├─ Aggressively truncates tool results to 1000 chars
        ├─ Marks content: { _emergency_truncated: true }
        └─ Still preserves ALL messages — never breaks tool chains
```

**Key insight**: The gateway prefers to let the upstream reject a too-large request and then apply emergency compression, rather than proactively deleting messages and risking tool chain corruption.

#### History Cache with Pinned Anchors

For long conversations, the gateway maintains a **full history cache** separate from the authoritative state, with intelligent message selection to keep requests within the ~200KB body limit.

```
Full Conversation History (cached, never evicted proactively)
    │
    ├─► Pinned Anchors (tool definitions)
    │   ├─ Stored separately from regular messages
    │   ├─ NEVER evicted from LRU cache
    │   ├─ Always included in every backend request
    │   └─ 24h TTL for stale anchor cleanup
    │
    └─► Smart Message Selection (for backend requests)
        ├─ Step 1: ALL system messages (always included)
        ├─ Step 2: Recent N messages (default 10)
        ├─ Step 3: Important middle messages
        │   ├─ User messages prioritized (carry intent)
        │   ├─ Scored by content length (longer = more important)
        │   └─ Sorted and selected to fill remaining budget
        └─ Step 4: Tool Chain Integrity Check
            ├─ Every tool_use must have matching tool_result
            ├─ Orphan tool_use/tool_result removed
            └─ Supports both OpenAI and Anthropic formats
```

**Request body budget**: `≤ 200KB` — controlled via `HISTORY_CACHE_MAX_MESSAGES_DEFAULT=20` (normal) and `HISTORY_CACHE_MAX_MESSAGES_TOOL=40` (tool-heavy conversations).

### Multi-Tier Cache Architecture (Self-Developed)

The gateway uses a three-layer caching architecture for thinking signatures, conversation state, and stream checkpoints — designed for high-concurrency reads with eventual-consistency writes.

```
┌─────────────────────────────────────────────────────────────────┐
│                    Cache Read Path (Hot Path)                   │
│                                                                 │
│  Request ──► L1 Memory Cache ──hit──► Return immediately        │
│                    │                                            │
│                   miss                                          │
│                    │                                            │
│              L2 SQLite DB ──hit──► Promote to L1, return        │
│                    │                                            │
│                   miss                                          │
│                    │                                            │
│              Content Hash Cache ──► Prefix/normalized match     │
│                    │                                            │
│                   miss ──► Cache miss, no signature available   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                   Cache Write Path (Async)                      │
│                                                                 │
│  New signature ──► L1 Memory Cache (sync, immediate)            │
│                    │                                            │
│                    └──► Async Write Queue ──► L2 SQLite DB      │
│                         ├─ Batch commit (100 ops / 1000ms)      │
│                         ├─ Retry with exponential backoff       │
│                         ├─ Queue overflow protection (10K max)  │
│                         └─ Graceful shutdown with drain         │
└─────────────────────────────────────────────────────────────────┘
```

#### L1: Memory Cache

| Feature | Detail |
|---------|--------|
| Data structure | `OrderedDict` — O(1) get/put with LRU ordering |
| Concurrency | Custom `RWLock` — multiple readers, single writer |
| Eviction | LRU (default), also supports FIFO and LFU |
| TTL | Per-entry expiration, lazy cleanup |
| Isolation | Namespace + conversation ID scoping |
| Cross-namespace fallback | `get_by_thinking_hash_any_namespace()` — finds signatures even when namespace changes mid-conversation |
| Warm-up | Pre-loads from L2 on startup |

#### L2: SQLite Database (WAL Mode)

Five tables provide persistent storage across gateway restarts:

| Table | Purpose | Key Fields |
|-------|---------|------------|
| `signature_cache` | thinking_hash → signature mapping | `thinking_hash`, `signature`, `namespace`, `conversation_id` |
| `tool_signature_cache` | tool_use_id → signature | `tool_id`, `signature` |
| `session_signature_cache` | session_id → (signature, thinking_text) | `session_id`, `signature`, `thinking_text` |
| `conversation_state` | SCID → authoritative history + last signature | `scid`, `authoritative_history`, `last_signature` |
| `session_checkpoints` | Stream interruption recovery | `scid`, `thinking_content`, `partial_response`, `signature` |

#### Content Hash Cache (Signature Recovery Accelerator)

When Cursor corrupts thinking text (whitespace changes, trailing spaces, content truncation), exact hash lookup fails. The Content Hash Cache provides **fuzzy matching** to recover signatures:

```
Thinking text from Cursor (corrupted)
    │
    ├─► Exact SHA256 hash lookup ──hit──► Return signature
    │
    ├─► Normalized hash lookup ──hit──► Return signature
    │   (strips whitespace, normalizes \r\n → \n)
    │
    └─► Prefix matching ──hit──► Return signature
        (first 100+ chars match, handles truncation)
```

| Feature | Detail |
|---------|--------|
| Dual-hash strategy | Exact SHA256 + normalized SHA256 (whitespace-insensitive) |
| Prefix matching | Min 100 chars, handles IDE truncation of thinking content |
| Capacity | LRU with 10,000 entries, 1-hour TTL |
| Hit tracking | Three counters: `exact_hits`, `normalized_hits`, `prefix_hits` |

#### Cache Facade (Unified Interface)

The `CacheFacade` singleton provides a unified API over all cache layers, with built-in migration support for rolling upgrades:

```
Migration Phases:
  LEGACY_ONLY → DUAL_WRITE → NEW_PREFERRED → NEW_ONLY

  - LEGACY_ONLY: Read/write only old cache
  - DUAL_WRITE: Write to both, read from old (safe transition)
  - NEW_PREFERRED: Write to both, read from new first
  - NEW_ONLY: Old cache fully retired
```

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
