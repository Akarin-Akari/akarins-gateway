# 实施计划：重命名 antigravity 后端 + 修复 404 freeze 策略

**日期**: 2026-03-02
**作者**: 浮浮酱 (Claude Opus 4.6)

---

## 问题背景

Augment 客户端请求 `gpt-4o-2024-11-20` 时返回 503，原因：

1. 启动探测 (`probe_backends_on_startup`) 向所有 backend 的根 URL 发送 HEAD 请求
2. copilot、kiro-gateway、ruoli 等 backend 根 URL 返回 404（没有根路由），被错误 freeze 300 秒
3. 实际上这些 backend 的 `/v1/chat/completions` 端点正常工作
4. 只剩 anyrouter 可用，但它不支持 `gpt-4o-2024-11-20`，最终 503

## 任务拆分

### Task 1: 重命名 backend key `"antigravity"` → `"gcli2api-antigravity"`

**目的**：区分自研 gcli2api 的 antigravity 后端与其他含 "antigravity" 的后端（如 antigravity-tools）

**不改的内容**：
- `antigravity-tools` 保持不变
- Python 模块目录 `backends/antigravity/` 不改名（仅改 key string）
- 环境变量名 `ANTIGRAVITY_BASE_URL`、`ANTIGRAVITY_ENABLED` 等保持向后兼容
- `ANTIGRAVITY_SUPPORTED_PATTERNS` 等常量名不改

**需要修改的文件和位置**：

#### Python 文件（运行时逻辑，必改）

| 文件 | 行号 | 当前值 | 改动 |
|------|------|--------|------|
| `config.py` | 118 | `"antigravity": {` | → `"gcli2api-antigravity": {` |
| `circuit_breaker.py` | 82 | `"antigravity": {` | → `"gcli2api-antigravity": {` |
| `routing.py` | 274 | `return "antigravity", model` | → `return "gcli2api-antigravity", model` |
| `routing.py` | 279 | `return "antigravity", model` | → `return "gcli2api-antigravity", model` |
| `routing.py` | 309 | `return "antigravity", model` | → `return "gcli2api-antigravity", model` |
| `proxy.py` | 1730 | `if backend_key == "antigravity"` | → `if backend_key == "gcli2api-antigravity"` |
| `proxy.py` | 3490 | `backend_key = "antigravity"` | → `backend_key = "gcli2api-antigravity"` |
| `proxy.py` | 3595 | `if backend_key == "antigravity"` | → `if backend_key == "gcli2api-antigravity"` |
| `proxy.py` | 3955 | `if backend_key == "antigravity"` | → `if backend_key == "gcli2api-antigravity"` |
| `backends/antigravity/backend.py` | 48 | `BACKENDS.get("antigravity", {})` | → `BACKENDS.get("gcli2api-antigravity", {})` |

#### Python 文件（注释/docstring，可选但建议改）

| 文件 | 行号 | 说明 |
|------|------|------|
| `config.py` | 444 | docstring 中的 "antigravity" |
| `routing.py` | 167 | docstring |
| `config_loader.py` | 429, 576, 1055, 1155 | comments/docstrings |
| `concurrency.py` | 437 | docstring example |

#### YAML 文件（gateway.yaml，约 80+ 处）

- `backends.antigravity` → `backends.gcli2api-antigravity` (line 45)
- `backend_capabilities.antigravity` → `backend_capabilities.gcli2api-antigravity` (line 730)
- 所有 `- backend: antigravity` → `- backend: gcli2api-antigravity` (model_routing + default_routing)
- 注释中的 "antigravity" 酌情更新

**注意**：`antigravity-tools` 的所有引用保持不变！

---

### Task 2: 修改启动探测 404 freeze 策略

**文件**: `proxy.py`，`probe_backends_on_startup()` 函数，lines 3676-3685

**当前逻辑**：
```python
if status == 404:
    await health_mgr.freeze_backend(
        backend_key, duration=STARTUP_PROBE_FREEZE_DURATION,
        reason=f"Startup probe: HTTP 404 at {host}:{port} — endpoint not found"
    )
    results[backend_key] = False
```

**修改方案**：添加白名单，只有特定 backend 在 404 时才 freeze

```python
# 只有这些 backend 的根 URL 应该返回 2xx，404 说明真的有问题
STRICT_404_FREEZE_BACKENDS = {"gcli2api-antigravity", "antigravity-tools"}

if status == 404:
    if backend_key in STRICT_404_FREEZE_BACKENDS:
        # 这些 backend 根 URL 应该返回 2xx，404 表示端点异常
        await health_mgr.freeze_backend(
            backend_key, duration=STARTUP_PROBE_FREEZE_DURATION,
            reason=f"Startup probe: HTTP 404 at {host}:{port} — endpoint not found"
        )
        log.warning(
            f"[STARTUP] ❄️ {backend_key} returned HTTP 404 at {host}:{port} "
            f"— endpoint not found, pre-frozen for 5min"
        )
        results[backend_key] = False
    else:
        # 其他 backend（copilot, kiro-gateway, ruoli 等）根 URL 返回 404 是正常的
        # 它们的实际 API 端点 /v1/chat/completions 可能正常工作
        log.info(
            f"[STARTUP] ✅ {backend_key} returned HTTP 404 at {host}:{port} "
            f"— root endpoint has no handler, but service is likely reachable"
        )
        results[backend_key] = True
```

---

## 执行顺序

1. **先执行 Task 1**（重命名），因为 Task 2 的白名单中使用了新名称 `"gcli2api-antigravity"`
2. **再执行 Task 2**（修改 404 freeze 策略）
3. 最后做一次全局检查，确保没有遗漏的 `"antigravity"` 引用（不含 `antigravity-tools`）

## 风险评估

- **低风险**：纯字符串重命名 + 逻辑分支修改，不涉及架构变更
- **回归风险**：gateway.yaml 中约 80+ 处替换，需仔细排除 `antigravity-tools`
- **回滚方案**：git revert 即可
