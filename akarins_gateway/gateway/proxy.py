"""
Gateway 代理请求模块

包含请求代理、流式响应处理、降级路由逻辑。

从 unified_gateway_router.py 抽取的代理函数。

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-01-18
"""

from typing import Dict, Any, Optional, Tuple, Callable, Union, AsyncIterator
import hashlib
import json
import asyncio
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import HTTPException
from starlette.responses import StreamingResponse as StarletteStreamingResponse

from .config import BACKENDS, RETRY_CONFIG, map_model_for_copilot, get_model_routing_rule
from .routing import (
    get_sorted_backends,
    get_backend_for_model,
    get_backend_and_model_for_routing,
    sanitize_model_params,
    calculate_retry_delay,
    should_retry,
    _is_model_supported_by_backend,  # [FIX 2026-03-10] capability filtering for fallback chain
)
# [DEPRECATED] is_antigravity_supported / is_kiro_gateway_supported no longer called by proxy.py (Phase B-2)
from .config_loader import is_backend_capable, get_cross_model_fallback, get_final_fallback, get_default_routing_rule, get_catch_all_routing
from .health import get_backend_health_manager

# [REFACTOR 2026-02-14] Public station module — unified header/body/fallback handling
from akarins_gateway.gateway.backends.public_station import get_public_station_manager as _get_psm
# [FIX 2026-02-12] 签名 400 错误恢复（从热路径 lazy import 提升到模块级）
from .thinking_recovery import (
    is_signature_400_error,
    is_in_tool_loop,
    prepare_signature_retry_body,
    SIGNATURE_RETRY_DELAY_MS,
)
from .circuit_breaker import is_copilot_circuit_open, open_copilot_circuit_breaker
from .conversion import (
    _convert_openai_to_anthropic_body,
    _convert_anthropic_to_openai_response,
    _convert_anthropic_stream_to_openai,
)
from .http_error_logger import log_http_error

# [FIX 2026-02-12] Antigravity-Tools thinking 拦截器
# antigravity-tools 返回 Anthropic 格式 SSE，但 thinking 内容作为内联 <thinking> 标签嵌入在 text_delta 中，
# 需要拦截并转换为结构化 thinking 块才能被 Claude Code 识别。
# 如果上游已返回原生 thinking 块则自动透传，不做任何处理。
from .thinking_interceptor import intercept_thinking_in_anthropic_sse

# [FIX 2026-02-03] 导入活跃请求追踪器（用于 SmartWarmup 静默期检测）
# STUB: credential_manager not extracted to akarins-gateway
# 提供 no-op stub，SmartWarmup 在独立网关中不需要
try:
    from akarins_gateway.credential_manager import get_active_request_tracker
except ImportError:
    from contextlib import asynccontextmanager as _acm

    class _NoopTracker:
        @_acm
        async def track_request(self):
            yield

    _noop_tracker = _NoopTracker()

    def get_active_request_tracker():
        return _noop_tracker

# [SEC 2026-02-21] Fallback 请求放大保护：单个用户请求最多尝试的后端次数
MAX_FALLBACK_ATTEMPTS = 10

# ==================== [FIX 2026-02-03] 403 错误快速降级支持 ====================

# 403 错误中表示配额/限制问题的关键词
_403_QUOTA_KEYWORDS = [
    'quota',
    'limit',
    'exceeded',
    'exhausted',
    'capacity',
    'billing',
    'subscription',
    'plan',
    'allowance',
    'usage',
    'rate',
    'throttl',
    'too many',
    'maximum',
    'restrict',
]

# 403 错误中表示认证问题的关键词（需要验证凭证而非降级）
_403_AUTH_KEYWORDS = [
    'unauthorized',
    'invalid token',
    'expired token',
    'authentication',
    'forbidden',
    'access denied',
    'permission',
    'not allowed',
]

# ==================== [NEW 2026-03-02] 凭证不可用检测 ====================
# gcli2api 在所有凭证被关闭时返回 HTTP 500 + "当前无可用凭证"
# 网关检测到此错误后冻结后端，并启动后台探测任务等待凭证恢复
_NO_CREDENTIAL_KEYWORDS = (
    "无可用凭证",
    "no available credential",
    "no usable credential",
)
# 仅对这些后端启用凭证检测冻结
_CREDENTIAL_GATE_BACKENDS = {"gcli2api-antigravity"}
# 凭证不可用冻结时长（秒）— 1小时兜底，实际由探测任务驱动解冻
_CREDENTIAL_FREEZE_DURATION = 3600
# 凭证探测间隔（秒）
_CREDENTIAL_PROBE_INTERVAL = 60
# 凭证探测最大次数（24小时 = 1440 * 60s）
_CREDENTIAL_PROBE_MAX_ATTEMPTS = 1440


def _is_no_credential_error(backend_key: str, status_code: int, error_text: str) -> bool:
    """
    检测后端返回的错误是否为"凭证不可用"。

    gcli2api 在所有凭证被关闭/耗尽时返回:
        HTTPException(status_code=500, detail="当前无可用凭证，请去控制台获取")

    仅对 _CREDENTIAL_GATE_BACKENDS 中的后端生效。

    Args:
        backend_key: 后端标识
        status_code: HTTP 状态码
        error_text: 响应体文本

    Returns:
        True 如果是凭证不可用错误
    """
    if backend_key not in _CREDENTIAL_GATE_BACKENDS:
        return False
    if status_code not in (500, 503):
        return False
    return any(kw in error_text for kw in _NO_CREDENTIAL_KEYWORDS)


# 后台凭证探测任务引用（防止被 GC）
_credential_probe_tasks: Dict[str, "asyncio.Task"] = {}


def _schedule_credential_probe(backend_key: str) -> None:
    """
    启动后台凭证探测任务。

    每隔 _CREDENTIAL_PROBE_INTERVAL 秒向 gcli2api 发一个最小请求，
    检测凭证是否恢复。恢复后自动解冻后端。

    如果已有探测任务在运行，不重复启动。
    """
    existing = _credential_probe_tasks.get(backend_key)
    if existing and not existing.done():
        log.debug(f"[CREDENTIAL PROBE] Probe task already running for {backend_key}")
        return

    task = asyncio.create_task(_credential_probe_loop(backend_key))
    _credential_probe_tasks[backend_key] = task
    log.info(
        f"[CREDENTIAL PROBE] 🔍 Started credential probe for {backend_key} "
        f"(interval={_CREDENTIAL_PROBE_INTERVAL}s, max={_CREDENTIAL_PROBE_MAX_ATTEMPTS})",
        tag="GATEWAY"
    )


async def _credential_probe_loop(backend_key: str) -> None:
    """
    后台探测循环：定期检测 gcli2api 凭证是否恢复。

    探测策略：发送最小 POST 到 /v1/chat/completions。
    gcli2api 的凭证检查在路由最开始执行，如果凭证不可用直接返回 500，
    不会走到 payload 解析。所以探测 body 可以极简。

    判断逻辑：
    - 仍返回 500 + "无可用凭证" → 继续冻结
    - 返回其他任何响应（包括 400 参数错误）→ 凭证已恢复，解冻
    - 连接失败 → 服务不可达，继续等待
    """
    health_mgr = get_backend_health_manager()

    config = BACKENDS.get(backend_key, {})
    base_url = config.get("base_url", "").rstrip("/")
    if not base_url:
        log.warning(f"[CREDENTIAL PROBE] No base_url for {backend_key}, aborting probe")
        return

    probe_url = f"{base_url}/v1/chat/completions"
    # 最小 payload — 只需触发凭证检查层
    probe_body = {
        "model": "credential-probe",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }

    for i in range(_CREDENTIAL_PROBE_MAX_ATTEMPTS):
        await asyncio.sleep(_CREDENTIAL_PROBE_INTERVAL)

        # 如果后端已经被其他机制解冻（如手动解冻、凭证事件回调），停止探测
        if not await health_mgr.is_frozen(backend_key):
            log.info(
                f"[CREDENTIAL PROBE] ✅ {backend_key} already unfrozen by another mechanism, "
                f"stopping probe (probe #{i + 1})",
                tag="GATEWAY"
            )
            return

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                verify=False,
            ) as probe_client:
                resp = await probe_client.post(probe_url, json=probe_body)
                resp_text = resp.text

                if _is_no_credential_error(backend_key, resp.status_code, resp_text):
                    log.debug(
                        f"[CREDENTIAL PROBE] {backend_key} still has no credentials "
                        f"(HTTP {resp.status_code}, probe #{i + 1})"
                    )
                    continue

                # 非凭证错误 = 凭证层已通过（即使返回 400/422 参数错误也说明凭证可用）
                await health_mgr.unfreeze_backend(backend_key)
                log.info(
                    f"[CREDENTIAL PROBE] ✅ {backend_key} credentials restored! "
                    f"(HTTP {resp.status_code}, probe #{i + 1}), backend unfrozen",
                    tag="GATEWAY"
                )
                return

        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log.debug(
                f"[CREDENTIAL PROBE] {backend_key} unreachable: {e} (probe #{i + 1})"
            )
            continue
        except Exception as e:
            log.debug(
                f"[CREDENTIAL PROBE] {backend_key} probe error: {e} (probe #{i + 1})"
            )
            continue

    log.warning(
        f"[CREDENTIAL PROBE] {backend_key} max probes ({_CREDENTIAL_PROBE_MAX_ATTEMPTS}) reached, "
        f"stopping probe. Backend will unfreeze when freeze expires.",
        tag="GATEWAY"
    )


# ==================== 凭证不可用检测结束 ====================


def _is_403_quota_error(status_code: int, error_body: str) -> bool:
    """
    判断是否是 403 配额相关错误（需要快速降级）

    403 错误有两种类型：
    1. 配额问题（quota/limit）- 需要快速切换到备用凭证或降级模型
    2. 认证问题（auth）- 需要触发凭证验证

    Args:
        status_code: HTTP 状态码
        error_body: 错误响应体

    Returns:
        True 表示是配额问题，应该快速降级
    """
    if status_code != 403:
        return False

    error_lower = error_body.lower()

    # 检查是否包含配额相关关键词
    for keyword in _403_QUOTA_KEYWORDS:
        if keyword in error_lower:
            return True

    return False


def _is_403_auth_error(status_code: int, error_body: str) -> bool:
    """
    判断是否是 403 认证相关错误（需要验证凭证）

    Args:
        status_code: HTTP 状态码
        error_body: 错误响应体

    Returns:
        True 表示是认证问题，应该触发凭证验证
    """
    if status_code != 403:
        return False

    error_lower = error_body.lower()

    # 检查是否包含认证相关关键词
    for keyword in _403_AUTH_KEYWORDS:
        if keyword in error_lower:
            return True

    return False


# [FIX 2026-02-12] H2: 抽取重复的签名 400 恢复逻辑为独立 helper（DRY 原则）
async def _handle_signature_400_recovery(
    status_code: int,
    error_text: str,
    body: Dict[str, Any],
    backend_key: str,
    attempt: int,
    max_retries: int,
) -> Optional[Dict[str, Any]]:
    """
    Handle signature 400 error recovery (strip thinking + retry).

    Ported from upstream handlers/claude.rs + openai.rs.
    Extracted to eliminate duplicate ~35-line blocks in two proxy paths.

    Args:
        status_code: HTTP status code
        error_text: Error response body text
        body: Current request body dict
        backend_key: Backend identifier string
        attempt: Current retry attempt number
        max_retries: Maximum retry count

    Returns:
        None: Not a signature 400 error — caller continues normal error flow
        Dict with "action" key:
          - {"action": "retry", "body": <modified>, "last_error": <str>}
            → caller should update body/last_error and `continue` the retry loop
          - {"action": "fail", "error": <str>}
            → caller should `return False, error`
    """
    if status_code != 400:
        return None

    if not is_signature_400_error(status_code, error_text):
        return None

    # Can still retry
    if attempt < max_retries:
        in_tool_loop = is_in_tool_loop(body)
        log.warning(
            f"[THINKING_RECOVERY] Backend {backend_key} 返回 400 签名错误，"
            f"准备重试 (attempt {attempt}/{max_retries}): "
            f"{error_text[:200]}"
            f"{' [tool_loop detected]' if in_tool_loop else ''}"
        )
        new_body = prepare_signature_retry_body(
            body,
            disable_thinking=True,
            inject_repair_prompt=True,
            close_tool_loop=in_tool_loop,
        )
        delay = SIGNATURE_RETRY_DELAY_MS / 1000.0
        log.info(
            f"[THINKING_RECOVERY] Retry {attempt}/{max_retries} "
            f"for {backend_key} after {delay:.1f}s delay "
            f"(signature 400 recovery)"
        )
        await asyncio.sleep(delay)
        return {
            "action": "retry",
            "body": new_body,
            "last_error": f"Signature 400: {error_text[:100]}",
        }

    # Exhausted retries
    log.warning(
        f"[THINKING_RECOVERY] Backend {backend_key} 签名 400 错误，"
        f"已达最大重试次数，降级处理"
    )
    return {"action": "fail", "error": f"400_SIGNATURE: {error_text[:200]}"}


def _extract_403_reason(error_body: str) -> str:
    """
    从 403 错误响应中提取详细原因

    Args:
        error_body: 错误响应体

    Returns:
        提取的原因描述
    """
    import json

    try:
        # 尝试解析 JSON
        error_json = json.loads(error_body)

        # 常见的错误消息字段
        for key in ['message', 'error', 'detail', 'reason', 'description']:
            if key in error_json:
                value = error_json[key]
                if isinstance(value, str):
                    return value
                elif isinstance(value, dict) and 'message' in value:
                    return value['message']

        return str(error_json)[:200]
    except (json.JSONDecodeError, TypeError):
        # 非 JSON 响应，返回前 200 字符
        return error_body[:200] if error_body else "Unknown 403 error"

# ==================== End of 403 错误快速降级支持 ====================


# [FIX 2026-02-01] 导入 Pre-Request 诊断框架
try:
    # STUB: diagnostics not extracted to akarins-gateway
    try:
        from akarins_gateway.diagnostics.context_validator import ContextValidator
    except ImportError:
        ContextValidator = None
    # STUB: diagnostics not extracted to akarins-gateway
    try:
        from akarins_gateway.diagnostics.diagnostic_types import DiagnosticLevel
    except ImportError:
        DiagnosticLevel = None
    if ContextValidator is not None:
        _context_validator = ContextValidator()
        _DIAGNOSTICS_ENABLED = True
    else:
        _context_validator = None
        _DIAGNOSTICS_ENABLED = False
except (ImportError, Exception) as e:
    _context_validator = None
    _DIAGNOSTICS_ENABLED = False
    import logging
    logging.getLogger(__name__).warning(f"[GATEWAY] Diagnostics disabled: {e}")

# 延迟导入 log，避免循环依赖
try:
    from akarins_gateway.core.log import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

# 延迟导入 http_client
try:
    from akarins_gateway.core.httpx_client import http_client, safe_close_client
except ImportError:
    http_client = None

__all__ = [
    "proxy_request_to_backend",
    "proxy_streaming_request",
    "proxy_streaming_request_with_timeout",
    "route_request_with_fallback",
    "ProxyHandler",
]


# ==================== 限流键构造工具 ====================

def _get_header_value(headers: Dict[str, str], key: str) -> str:
    value = headers.get(key)
    if value:
        return value
    return headers.get(key.lower()) or headers.get(key.upper()) or ""


def _extract_project_id(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    project_id = body.get("project")
    if project_id:
        return str(project_id)
    request = body.get("request")
    if isinstance(request, dict):
        project_id = request.get("project")
        if project_id:
            return str(project_id)
    return ""


def _hash_key(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        if part:
            hasher.update(part.encode("utf-8"))
        hasher.update(b"|")
    return hasher.hexdigest()[:16]


def _build_account_id(backend_key: str, headers: Dict[str, str], body: Any) -> str:
    auth = _get_header_value(headers, "authorization")
    project_id = _extract_project_id(body)
    client_tag = _get_header_value(headers, "x-augment-client") or _get_header_value(headers, "user-agent")
    key_hash = _hash_key(backend_key, auth, project_id, client_tag)
    return f"{backend_key}_{key_hash}"


def _build_model_account_id(account_id: str, model_name: Optional[str]) -> Optional[str]:
    if not model_name:
        return None
    return f"{account_id}:{model_name}"


def _build_backend_limit_id(backend_key: str) -> str:
    return f"backend::{backend_key}"


def _build_rate_limit_key(account_id: str, model_name: Optional[str]) -> str:
    if not model_name:
        return account_id
    return f"{account_id}:{model_name}"


def _summarize_anthropic_messages(messages: Any) -> Dict[str, Any]:
    """
    生成 Anthropic messages 的结构化摘要（仅计数，不输出正文）。
    用于排查 AnyRouter \"invalid claude code request\"。
    """
    summary: Dict[str, Any] = {
        "messages": 0,
        "role_user": 0,
        "role_assistant": 0,
        "role_system": 0,
        "role_other": 0,
        "string_content_msgs": 0,
        "list_content_msgs": 0,
        "empty_content_msgs": 0,
        "tool_use_blocks": 0,
        "tool_result_blocks": 0,
        "thinking_blocks": 0,
        "tool_result_missing_tool_use_refs": 0,
    }

    if not isinstance(messages, list):
        return summary

    summary["messages"] = len(messages)
    seen_tool_use_ids: set[str] = set()
    pending_tool_result_ids: list[str] = []

    for msg in messages:
        if not isinstance(msg, dict):
            summary["role_other"] += 1
            continue

        role = str(msg.get("role", "")).lower()
        if role == "user":
            summary["role_user"] += 1
        elif role == "assistant":
            summary["role_assistant"] += 1
        elif role == "system":
            summary["role_system"] += 1
        else:
            summary["role_other"] += 1

        content = msg.get("content")
        if isinstance(content, str):
            if content:
                summary["string_content_msgs"] += 1
            else:
                summary["empty_content_msgs"] += 1
            continue

        if not isinstance(content, list):
            if not content:
                summary["empty_content_msgs"] += 1
            continue

        summary["list_content_msgs"] += 1
        if not content:
            summary["empty_content_msgs"] += 1
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type", "")).lower()
            if btype == "tool_use":
                summary["tool_use_blocks"] += 1
                _tool_id = block.get("id")
                if isinstance(_tool_id, str) and _tool_id.strip():
                    seen_tool_use_ids.add(_tool_id.strip())
            elif btype == "tool_result":
                summary["tool_result_blocks"] += 1
                _ref_id = block.get("tool_use_id")
                if isinstance(_ref_id, str) and _ref_id.strip():
                    pending_tool_result_ids.append(_ref_id.strip())
            elif btype in ("thinking", "redacted_thinking"):
                summary["thinking_blocks"] += 1

    summary["tool_result_missing_tool_use_refs"] = sum(
        1 for ref in pending_tool_result_ids if ref not in seen_tool_use_ids
    )
    return summary


def _summarize_anthropic_tools(tools: Any) -> Dict[str, Any]:
    """
    生成 Anthropic tools 的结构摘要（仅统计，不输出 schema 正文）。
    """
    summary: Dict[str, Any] = {
        "tools": 0,
        "invalid_items": 0,
        "missing_name": 0,
        "missing_input_schema": 0,
        "non_object_schema": 0,
        "first_names": [],
    }
    if not isinstance(tools, list):
        return summary

    summary["tools"] = len(tools)
    names: list[str] = []

    for item in tools:
        if not isinstance(item, dict):
            summary["invalid_items"] += 1
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            summary["missing_name"] += 1
        else:
            names.append(name.strip())

        if "input_schema" not in item:
            summary["missing_input_schema"] += 1
        else:
            schema = item.get("input_schema")
            if not isinstance(schema, dict):
                summary["non_object_schema"] += 1

    summary["first_names"] = names[:5]
    return summary


def _extract_text_blocks_from_anthropic_content(content: Any) -> Optional[list[dict[str, str]]]:
    """
    将 Anthropic content 归一为 text blocks。
    返回 None 表示包含非 text 块，不适合做 Cursor->AnyRouter 的安全合并。
    """
    blocks: list[dict[str, str]] = []

    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
        return blocks

    if not isinstance(content, list):
        return None

    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            return None
        if str(item.get("type", "")).lower() != "text":
            return None
        text = item.get("text")
        if text is None:
            text = ""
        blocks.append({"type": "text", "text": str(text)})

    return blocks


def _merge_all_user_text_messages_for_anyrouter(messages: Any) -> Tuple[Any, bool]:
    """
    AnyRouter 兼容性补丁：
    仅当消息列表“全部为 user 且内容均为 text”时，合并为单条 user。
    """
    if not isinstance(messages, list) or len(messages) <= 1:
        return messages, False

    merged_blocks: list[dict[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            return messages, False
        if str(msg.get("role", "")).lower() != "user":
            return messages, False
        text_blocks = _extract_text_blocks_from_anthropic_content(msg.get("content"))
        if text_blocks is None:
            return messages, False
        merged_blocks.extend(text_blocks)

    if not merged_blocks:
        return messages, False

    return [{"role": "user", "content": merged_blocks}], True


_MCP_INSTRUCTIONS_BLOCK_RE = re.compile(
    r"<mcp_instructions\b[^>]*>.*?</mcp_instructions>",
    re.IGNORECASE | re.DOTALL,
)


def _strip_mcp_instructions_from_text(text: str) -> Tuple[str, bool]:
    if not isinstance(text, str) or not text:
        return text, False
    cleaned = _MCP_INSTRUCTIONS_BLOCK_RE.sub("", text)
    changed = cleaned != text
    if changed:
        # 只做轻量归一化，避免改动过大影响上游提示词结构。
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, changed


def _strip_anyrouter_cursor_mcp_instructions(messages: Any) -> Tuple[Any, bool, Dict[str, Any]]:
    """
    AnyRouter + Cursor: 剥离消息正文中的 <mcp_instructions> 块。
    观察证据：该块会导致请求画像与历史成功样本产生差异，可能触发 invalid claude code request。
    """
    summary: Dict[str, Any] = {
        "messages": 0,
        "blocks_checked": 0,
        "blocks_changed": 0,
    }
    if not isinstance(messages, list) or not messages:
        return messages, False, summary

    changed = False
    updated_messages: list[Any] = []
    summary["messages"] = len(messages)

    for msg in messages:
        if not isinstance(msg, dict):
            updated_messages.append(msg)
            continue

        content = msg.get("content")
        if isinstance(content, str):
            summary["blocks_checked"] += 1
            cleaned, block_changed = _strip_mcp_instructions_from_text(content)
            if block_changed:
                msg2 = dict(msg)
                msg2["content"] = cleaned
                updated_messages.append(msg2)
                changed = True
                summary["blocks_changed"] += 1
            else:
                updated_messages.append(msg)
            continue

        if not isinstance(content, list):
            updated_messages.append(msg)
            continue

        content_changed = False
        new_content: list[Any] = []
        for block in content:
            if isinstance(block, dict) and str(block.get("type", "")).lower() == "text":
                summary["blocks_checked"] += 1
                original_text = block.get("text")
                cleaned, block_changed = _strip_mcp_instructions_from_text(
                    "" if original_text is None else str(original_text)
                )
                if block_changed:
                    new_block = dict(block)
                    new_block["text"] = cleaned
                    new_content.append(new_block)
                    content_changed = True
                    changed = True
                    summary["blocks_changed"] += 1
                else:
                    new_content.append(block)
            else:
                new_content.append(block)

        if content_changed:
            msg2 = dict(msg)
            msg2["content"] = new_content
            updated_messages.append(msg2)
        else:
            updated_messages.append(msg)

    return (updated_messages if changed else messages), changed, summary


def _apply_anyrouter_cursor_tools_schema_profile(tools: Any) -> Tuple[Any, bool]:
    """
    为 Cursor->AnyRouter 的工具 schema 补齐 Claude Code 常见字段：
    - $schema
    - additionalProperties
    """
    if not isinstance(tools, list) or not tools:
        return tools, False

    changed = False
    updated_tools: list[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            updated_tools.append(tool)
            continue

        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            updated_tools.append(tool)
            continue

        updated_schema = dict(schema)
        tool_changed = False
        if "$schema" not in updated_schema:
            updated_schema["$schema"] = "http://json-schema.org/draft-07/schema#"
            tool_changed = True
            changed = True
        if "additionalProperties" not in updated_schema:
            updated_schema["additionalProperties"] = True
            tool_changed = True
            changed = True

        if tool_changed:
            updated_tool = dict(tool)
            updated_tool["input_schema"] = updated_schema
            updated_tools.append(updated_tool)
        else:
            updated_tools.append(tool)

    return (updated_tools if changed else tools), changed


# TODO(cursor): Cursor tool filtering — ported from gcli2api, untested in akarins-gateway.
#   Needs validation with real Cursor traffic against AnyRouter.
def _filter_anyrouter_cursor_tools(
    tools: Any,
    *,
    force_filter_prefix: bool = False,
    force_max_tools: Optional[int] = None,
) -> Tuple[Any, bool, Dict[str, Any]]:
    """
    AnyRouter + Cursor 工具过滤：
    - 默认剔除 plugin-/user- 前缀的扩展工具（这类工具在 AnyRouter 上更容易触发请求画像校验）
    - 可配置工具总数上限，避免超长工具列表触发上游策略
    """
    summary: Dict[str, Any] = {
        "original": 0,
        "kept": 0,
        "dropped": 0,
        "dropped_prefix": [],
        "trimmed_to_max": 0,
    }
    if not isinstance(tools, list):
        return tools, False, summary

    summary["original"] = len(tools)
    if not tools:
        return tools, False, summary

    filter_enabled = force_filter_prefix or _env_flag("ANYROUTER_CURSOR_FILTER_PLUGIN_TOOLS", default=True)
    if force_max_tools is not None:
        max_tools = max(1, int(force_max_tools))
    else:
        max_tools_raw = str(os.getenv("ANYROUTER_CURSOR_MAX_TOOLS", "19")).strip()
        try:
            max_tools = max(1, int(max_tools_raw))
        except Exception:
            max_tools = 19

    kept_tools: list[Any] = []
    dropped_prefix_names: list[str] = []
    blocked_prefixes = (
        "plugin-",
        "user-",
        "plugin_",
        "user_",
        "mcp__plugin_",
        "mcp__user_",
    )

    for item in tools:
        if not isinstance(item, dict):
            kept_tools.append(item)
            continue

        name = item.get("name")
        if not isinstance(name, str):
            kept_tools.append(item)
            continue

        lowered = name.strip().lower()
        if filter_enabled and any(lowered.startswith(prefix) for prefix in blocked_prefixes):
            dropped_prefix_names.append(name)
            continue
        kept_tools.append(item)

    trimmed = 0
    if len(kept_tools) > max_tools:
        trimmed = len(kept_tools) - max_tools
        kept_tools = kept_tools[:max_tools]

    summary["kept"] = len(kept_tools)
    summary["dropped"] = len(tools) - len(kept_tools)
    summary["dropped_prefix"] = dropped_prefix_names[:8]
    summary["trimmed_to_max"] = trimmed

    changed = summary["dropped"] > 0 or trimmed > 0
    return (kept_tools if changed else tools), changed, summary


# ==================== AnyRouter 抓包工具 ====================

def _env_flag(name: str, default: bool = False) -> bool:
    """[REFACTOR 2026-03-14] Unified flag reader: YAML runtime_flags > env var > default"""
    from .config_loader import get_runtime_flag
    return get_runtime_flag(name, default)


# [FIX 2026-02-17] AnyRouter Cursor 路径对齐 Claude Code header 画像
# 仅用于 AnyRouter Anthropic 模式，降低影响面并便于快速回滚。
_ANYROUTER_CLAUDE_CODE_HEADER_PROFILE = (
    "claude-code-20250219,"
    "prompt-caching-scope-2026-01-05,"
    "adaptive-thinking-2026-01-28"
)

# [FIX 2026-02-24] Gateway proxy 不再使用 curl_cffi Chrome131 TLS 指纹
# Chrome131 TLS 伪装仅用于 antigravity_api.py 直接访问 Google API 防封号，
# Gateway proxy 层面：本地后端走 HTTP 无需 TLS，远程公益站需标准 HTTPS。
# curl_cffi 的 BoringSSL 指纹会导致 anyrouter 等远程站点 TLS 握手失败 (curl error 35)。
_GATEWAY_USE_CURL_CFFI = False


def _mask_secret(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if text.lower().startswith("bearer "):
        token = text[7:]
        if len(token) <= 8:
            return "Bearer ***"
        return f"Bearer {token[:4]}***{token[-4:]}"
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}***{text[-4:]}"


def _sanitize_headers_for_capture(headers: Dict[str, Any]) -> Dict[str, str]:
    sensitive = {"authorization", "x-api-key", "proxy-authorization"}
    sanitized: Dict[str, str] = {}
    if not isinstance(headers, dict):
        return sanitized
    for k in sorted(headers.keys(), key=lambda x: str(x).lower()):
        key = str(k)
        value = headers.get(k)
        if key.lower() in sensitive:
            sanitized[key] = _mask_secret(value)
        else:
            sanitized[key] = str(value) if value is not None else ""
    return sanitized


def _project_messages_for_capture(messages: Any) -> Dict[str, Any]:
    projected: Dict[str, Any] = {
        "summary": _summarize_anthropic_messages(messages),
        "role_sequence": [],
        "messages": [],
    }
    if not isinstance(messages, list):
        return projected

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            projected["role_sequence"].append("other")
            projected["messages"].append({"index": idx, "role": "other", "kind": type(msg).__name__})
            continue

        role = str(msg.get("role", "other")).lower()
        projected["role_sequence"].append(role)
        item: Dict[str, Any] = {
            "index": idx,
            "role": role,
            "keys": sorted(msg.keys()),
        }

        content = msg.get("content")
        if isinstance(content, str):
            item["content_kind"] = "string"
            item["content_length"] = len(content)
        elif isinstance(content, list):
            item["content_kind"] = "list"
            block_types: Dict[str, int] = {}
            for block in content:
                if isinstance(block, dict):
                    btype = str(block.get("type", "unknown")).lower()
                else:
                    btype = "non_dict"
                block_types[btype] = block_types.get(btype, 0) + 1
            item["block_types"] = block_types
            item["content_items"] = len(content)
        else:
            item["content_kind"] = type(content).__name__

        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            item["tool_calls_count"] = len(tool_calls)
            item["tool_call_names"] = [
                str((tc.get("function") or {}).get("name", ""))
                for tc in tool_calls
                if isinstance(tc, dict)
            ][:8]
        elif isinstance(tool_calls, dict):
            item["tool_calls_count"] = len(tool_calls)
            item["tool_call_names"] = []

        if "tool_call_id" in msg:
            item["tool_call_id"] = str(msg.get("tool_call_id"))

        projected["messages"].append(item)
    return projected


def _project_tools_for_capture(tools: Any) -> Dict[str, Any]:
    projected: Dict[str, Any] = {
        "summary": _summarize_anthropic_tools(tools),
        "tools": [],
    }
    if not isinstance(tools, list):
        return projected

    for idx, tool in enumerate(tools):
        if not isinstance(tool, dict):
            projected["tools"].append({"index": idx, "kind": type(tool).__name__})
            continue

        item: Dict[str, Any] = {
            "index": idx,
            "keys": sorted(tool.keys()),
        }

        # Anthropic 格式: {name, input_schema}
        if "name" in tool:
            item["format"] = "anthropic"
            item["name"] = str(tool.get("name", ""))
            schema = tool.get("input_schema")
        # OpenAI 格式: {type: function, function: {name, parameters}}
        elif isinstance(tool.get("function"), dict):
            item["format"] = "openai"
            func = tool.get("function") or {}
            item["name"] = str(func.get("name", ""))
            schema = func.get("parameters")
        else:
            item["format"] = "unknown"
            schema = None

        if isinstance(schema, dict):
            item["schema_type"] = schema.get("type", "")
            item["schema_keys"] = sorted(schema.keys())
            props = schema.get("properties")
            if isinstance(props, dict):
                item["schema_properties"] = len(props)
        else:
            item["schema_type"] = type(schema).__name__

        projected["tools"].append(item)

    return projected


def _detect_client_type_for_capture(headers: Dict[str, str]) -> str:
    try:
        from akarins_gateway.ide_compat import ClientTypeDetector
        info = ClientTypeDetector.detect(dict(headers))
        return str(info.client_type.value)
    except Exception:
        return "unknown"


def _write_anyrouter_capture(
    *,
    capture_id: str,
    backend_key: str,
    incoming_headers: Dict[str, str],
    outgoing_headers: Dict[str, str],
    body: Any,
    endpoint: str,
    original_endpoint: str,
    url: str,
    attempt: int,
    max_attempts: int,
    use_openai_endpoint: bool,
) -> None:
    if backend_key != "anyrouter":
        return
    if not _env_flag("ANYROUTER_CAPTURE_ENABLED", default=False):
        return

    client_type = _detect_client_type_for_capture(incoming_headers)
    client_filter = os.getenv("ANYROUTER_CAPTURE_CLIENTS", "").strip().lower()
    if client_filter:
        allowed = {x.strip() for x in client_filter.split(",") if x.strip()}
        if client_type not in allowed:
            return

    capture_dir_raw = os.getenv("ANYROUTER_CAPTURE_DIR", "data/anyrouter_captures").strip() or "data/anyrouter_captures"
    capture_dir = Path(capture_dir_raw)
    if not capture_dir.is_absolute():
        capture_dir = Path.cwd() / capture_dir

    include_raw_body = _env_flag("ANYROUTER_CAPTURE_INCLUDE_RAW_BODY", default=False)

    body_keys = list(body.keys()) if isinstance(body, dict) else []
    model = ""
    if isinstance(body, dict) and isinstance(body.get("model"), str):
        model = body.get("model", "")

    model_safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", model or "unknown")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    file_name = f"{ts}_{capture_id}_a{attempt}_of_{max_attempts}_{client_type}_{model_safe}.json"
    file_path = capture_dir / file_name

    capture_data: Dict[str, Any] = {
        "capture_version": 1,
        "capture_id": capture_id,
        "timestamp": datetime.now().isoformat(),
        "backend": backend_key,
        "client_type": client_type,
        "endpoint": endpoint,
        "original_endpoint": original_endpoint,
        "url": url,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "use_openai_endpoint": use_openai_endpoint,
        "incoming_headers": _sanitize_headers_for_capture(incoming_headers),
        "outgoing_headers": _sanitize_headers_for_capture(outgoing_headers),
        "body_meta": {
            "keys": body_keys,
            "model": model,
            "stream": bool(body.get("stream")) if isinstance(body, dict) else False,
            "has_thinking": bool(isinstance(body, dict) and "thinking" in body),
            "has_tools": bool(isinstance(body, dict) and "tools" in body),
            "has_system": bool(isinstance(body, dict) and "system" in body),
        },
        "messages_projection": _project_messages_for_capture(body.get("messages") if isinstance(body, dict) else None),
        "tools_projection": _project_tools_for_capture(body.get("tools") if isinstance(body, dict) else None),
    }
    if include_raw_body:
        capture_data["raw_body"] = body

    try:
        capture_dir.mkdir(parents=True, exist_ok=True)
        with file_path.open("w", encoding="utf-8") as f:
            json.dump(capture_data, f, ensure_ascii=False, indent=2)
        log.info(
            f"[GATEWAY][CAPTURE] AnyRouter request captured: client={client_type}, file={file_path}",
            tag="GATEWAY",
        )
    except Exception as capture_err:
        log.warning(
            f"[GATEWAY][CAPTURE] Failed to write AnyRouter capture: {capture_err}",
            tag="GATEWAY",
        )


# ==================== AnyRouter 模型回退工具 ====================

_ANYROUTER_MODEL_ERROR_KEYWORDS = (
    "当前 api 不支持所选模型",
    "不支持所选模型",
    "model not supported",
    "unsupported model",
    "unknown model",
    "model does not exist",
    "模型不存在",
)

_ANYROUTER_OPUS46_DEFAULT_DATE = "20260205"
_ANYROUTER_INVALID_REQUEST_KEYWORDS = (
    "invalid claude code request",
    "invalid claude request",
)

_ANYROUTER_CURSOR_PROFILE_NAMES = (
    "baseline",
    "metadata_ccstyle",
    "adaptive_system",
    "plain_messages",
    "minimal_messages",
)


def _is_anyrouter_invalid_request_error(status_code: Optional[int], error_text: str) -> bool:
    """
    判断 AnyRouter 是否返回了“请求画像不兼容”错误。
    """
    text = str(error_text or "").lower()
    if status_code is None:
        return any(keyword in text for keyword in _ANYROUTER_INVALID_REQUEST_KEYWORDS)
    if status_code not in (400, 422, 500):
        return False
    return any(keyword in text for keyword in _ANYROUTER_INVALID_REQUEST_KEYWORDS)


def _build_anyrouter_cursor_user_id(incoming_headers: Dict[str, str]) -> str:
    """
    为 Cursor 生成稳定的 metadata.user_id，避免固定值 "cursor" 触发上游画像判定。
    """
    if not isinstance(incoming_headers, dict):
        incoming_headers = {}
    # 历史成功样本采用 legacy 画像（_cursor 后缀），作为 baseline。
    seed = (
        incoming_headers.get("x-ag-conversation-id")
        or incoming_headers.get("x-conversation-id")
        or incoming_headers.get("x-request-id")
        or incoming_headers.get("authorization")
        or "cursor-anyrouter"
    )
    digest = hashlib.sha256(str(seed).encode("utf-8", errors="ignore")).hexdigest()
    return f"user_{digest}_cursor"


def _build_anyrouter_cursor_user_id_ccstyle(incoming_headers: Dict[str, str]) -> str:
    """
    Claude Code 风格 user_id 画像：
    user_<account_hash>_account__session_<uuid>
    """
    if not isinstance(incoming_headers, dict):
        incoming_headers = {}
    scid = incoming_headers.get("x-ag-conversation-id") or incoming_headers.get("x-conversation-id")
    auth = incoming_headers.get("authorization") or incoming_headers.get("x-api-key")

    account_seed = str(auth).strip() if isinstance(auth, str) and auth.strip() else "cursor-anyrouter-account"
    account_hash = hashlib.sha256(account_seed.encode("utf-8", errors="ignore")).hexdigest()

    if isinstance(scid, str) and scid.strip():
        session_seed = f"scid:{scid.strip()}"
    elif isinstance(auth, str) and auth.strip():
        session_seed = f"auth:{auth.strip()}"
    else:
        session_seed = "cursor-anyrouter-session"
    session_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, session_seed))
    return f"user_{account_hash}_account__session_{session_uuid}"


def _to_anyrouter_system_blocks(system_value: Any) -> list[dict[str, Any]]:
    """
    归一化 system 为 Anthropic text block 列表。
    """
    if isinstance(system_value, list):
        normalized_blocks: list[dict[str, Any]] = []
        for item in system_value:
            if isinstance(item, dict) and str(item.get("type", "")).lower() == "text":
                text = item.get("text")
                normalized_blocks.append({"type": "text", "text": "" if text is None else str(text)})
            elif isinstance(item, str):
                normalized_blocks.append({"type": "text", "text": item})
        if normalized_blocks:
            return normalized_blocks

    if isinstance(system_value, str) and system_value.strip():
        return [{"type": "text", "text": system_value}]

    # 保持最小侵入：仅注入一条简短 system，避免空字符串 system 被上游误判。
    return [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}]


def _apply_anyrouter_cursor_profile_adaptive(
    body: Any,
    incoming_headers: Dict[str, str],
    force_adaptive_thinking: bool = True,
) -> Tuple[Any, bool]:
    """
    Cursor->AnyRouter 画像对齐（Stage-1）：
    - thinking: enabled/budget -> adaptive（可按需关闭）
    - system: string -> text blocks
    - metadata.user_id: 规范化稳定值
    """
    if not isinstance(body, dict):
        return body, False

    changed = False
    updated = dict(body)

    if force_adaptive_thinking and "thinking" in updated:
        thinking = updated.get("thinking")
        if isinstance(thinking, dict):
            thinking_type = str(thinking.get("type", "")).strip().lower()
            if thinking_type != "adaptive" or set(thinking.keys()) != {"type"}:
                updated["thinking"] = {"type": "adaptive"}
                changed = True
        elif thinking is not None:
            updated["thinking"] = {"type": "adaptive"}
            changed = True

    system_blocks = _to_anyrouter_system_blocks(updated.get("system"))
    if updated.get("system") != system_blocks:
        updated["system"] = system_blocks
        changed = True

    metadata = updated.get("metadata")
    updated_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    user_id = updated_metadata.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip() or user_id.strip().lower() == "cursor":
        updated_metadata["user_id"] = _build_anyrouter_cursor_user_id(incoming_headers)
        changed = True
    if updated.get("metadata") != updated_metadata:
        updated["metadata"] = updated_metadata
        changed = True

    return (updated if changed else body), changed


# TODO(cursor): 5-stage Cursor profile fallback — ported from gcli2api, untested.
#   Stages: baseline → metadata_ccstyle → adaptive_system → plain_messages → minimal_messages
#   Requires real Cursor → AnyRouter traffic to validate each stage transition.
def _maybe_apply_anyrouter_cursor_profile_fallback(
    *,
    backend_key: str,
    client_type: str,
    use_openai_endpoint: bool,
    status_code: Optional[int],
    error_text: str,
    body: Any,
    request_headers: Dict[str, str],
    incoming_headers: Dict[str, str],
    profile_stage: int,
) -> Tuple[Any, Dict[str, str], int, Optional[str], bool]:
    """
    AnyRouter + Cursor 自动画像回退。
    目标：同一次请求内自动切换策略，减少人工反复测试。
    """
    if backend_key != "anyrouter" or client_type != "cursor" or use_openai_endpoint:
        return body, request_headers, profile_stage, None, False
    if not _env_flag("ANYROUTER_CURSOR_PROFILE_FALLBACK", default=True):
        return body, request_headers, profile_stage, None, False
    if not _is_anyrouter_invalid_request_error(status_code, error_text):
        return body, request_headers, profile_stage, None, False

    next_stage = profile_stage + 1
    if next_stage >= len(_ANYROUTER_CURSOR_PROFILE_NAMES):
        return body, request_headers, profile_stage, None, False

    new_body = body
    new_headers = dict(request_headers or {})
    changed = False

    if next_stage == 1:
        if isinstance(new_body, dict):
            stage1_body = dict(new_body)
            stage1_metadata = dict(stage1_body.get("metadata")) if isinstance(stage1_body.get("metadata"), dict) else {}
            ccstyle_user_id = _build_anyrouter_cursor_user_id_ccstyle(incoming_headers)
            if stage1_metadata.get("user_id") != ccstyle_user_id:
                stage1_metadata["user_id"] = ccstyle_user_id
                stage1_body["metadata"] = stage1_metadata
                new_body = stage1_body
                changed = True
        if new_headers.pop("x-app", None) is not None:
            changed = True

    elif next_stage == 2:
        new_body, body_changed = _apply_anyrouter_cursor_profile_adaptive(
            new_body,
            incoming_headers,
            force_adaptive_thinking=True,
        )
        if isinstance(new_body, dict):
            _strict_tools, _strict_changed, _strict_summary = _filter_anyrouter_cursor_tools(
                new_body.get("tools"),
                force_filter_prefix=True,
                force_max_tools=19,
            )
            if _strict_changed:
                stage1_body = dict(new_body)
                stage1_body["tools"] = _strict_tools
                new_body = stage1_body
                changed = True
                log.warning(
                    f"[GATEWAY] anyrouter: cursor stage=adaptive_system strict tool filter "
                    f"(original={_strict_summary.get('original')}, kept={_strict_summary.get('kept')}, "
                    f"dropped_prefix={_strict_summary.get('dropped_prefix')}, "
                    f"trimmed={_strict_summary.get('trimmed_to_max')})",
                    tag="GATEWAY",
                )
        if new_headers.pop("x-app", None) is not None:
            changed = True
        changed = changed or body_changed

    elif next_stage == 3:
        if isinstance(new_body, dict):
            stage3_body = dict(new_body)
            for key in ("tools", "tool_choice", "thinking"):
                if key in stage3_body:
                    stage3_body.pop(key, None)
                    changed = True
            new_body = stage3_body
        if new_headers.pop("anthropic-beta", None) is not None:
            changed = True
        if new_headers.pop("anthropic-dangerous-direct-browser-access", None) is not None:
            changed = True
        if new_headers.pop("x-app", None) is not None:
            changed = True

    elif next_stage == 4:
        if isinstance(new_body, dict):
            stage4_body = dict(new_body)
            # 最小化请求体，排除工具/思考/系统画像影响。
            for key in ("tools", "tool_choice", "thinking", "system", "metadata"):
                if key in stage4_body:
                    stage4_body.pop(key, None)
                    changed = True
            _max_tokens = stage4_body.get("max_tokens")
            if isinstance(_max_tokens, (int, float)) and _max_tokens > 16000:
                stage4_body["max_tokens"] = 16000
                changed = True
            new_body = stage4_body
        if new_headers.pop("anthropic-beta", None) is not None:
            changed = True
        if new_headers.pop("anthropic-dangerous-direct-browser-access", None) is not None:
            changed = True
        if new_headers.pop("x-app", None) is not None:
            changed = True

    if not changed:
        # baseline 已经覆盖了较多画像补丁时，某些阶段可能“无字段可改”。
        # 仍推进阶段，确保同一请求内能继续尝试后续回退策略。
        if _env_flag("ANYROUTER_CURSOR_PROFILE_FORCE_ADVANCE", default=True):
            profile_name = _ANYROUTER_CURSOR_PROFILE_NAMES[next_stage]
            log.warning(
                f"[GATEWAY] anyrouter: cursor profile fallback stage advance without payload mutation "
                f"(status={status_code}) stage={_ANYROUTER_CURSOR_PROFILE_NAMES[profile_stage]} -> {profile_name}",
                tag="GATEWAY",
            )
            return new_body, new_headers, next_stage, profile_name, True
        return body, request_headers, profile_stage, None, False

    profile_name = _ANYROUTER_CURSOR_PROFILE_NAMES[next_stage]
    log.warning(
        f"[GATEWAY] anyrouter: cursor profile fallback triggered "
        f"(status={status_code}) stage={_ANYROUTER_CURSOR_PROFILE_NAMES[profile_stage]} -> {profile_name}",
        tag="GATEWAY",
    )
    return new_body, new_headers, next_stage, profile_name, True


def _is_anyrouter_model_error(status_code: Optional[int], error_text: str) -> bool:
    """判断错误是否属于 AnyRouter 模型名不匹配场景。"""
    if status_code not in (400, 404, 500):
        return False
    text = str(error_text or "").lower()
    return any(keyword in text for keyword in _ANYROUTER_MODEL_ERROR_KEYWORDS)


def _build_anyrouter_model_fallback_candidates(current_model: str) -> list[str]:
    """
    生成 AnyRouter 模型回退候选：
    1) 带日期 <-> 不带日期
    2) 4.6 点号 <-> 4-6 连字符
    3) 对 Opus 4.6 额外补一个已知可用日期后缀（20260205）
    """
    model = str(current_model or "").strip().lower()
    if not model:
        return []

    candidates: list[str] = []

    def _add(value: str) -> None:
        v = str(value or "").strip().lower()
        if v and v not in candidates:
            candidates.append(v)

    # 1) claude-opus-4-6[-YYYYMMDD]
    m_hyphen = re.match(r"^(claude-(?:opus|sonnet|haiku)-(\d)-(\d))(?:-(\d{8}))?$", model)
    if m_hyphen:
        base = m_hyphen.group(1)
        major = m_hyphen.group(2)
        minor = m_hyphen.group(3)
        date = m_hyphen.group(4)

        _add(base)  # 无日期
        if base == "claude-opus-4-6":
            _add(f"{base}-{_ANYROUTER_OPUS46_DEFAULT_DATE}")  # 固定已知可用日期
        if date:
            _add(f"{base}-{date}")  # 原格式（便于统一去重流程）
        _add(f"claude-{base.split('-', 1)[1]}".replace(f"-{major}-{minor}", f"-{major}.{minor}"))  # 点号放后面

    # 2) claude-opus-4.6[-YYYYMMDD]
    m_dot = re.match(r"^(claude-(?:opus|sonnet|haiku)-(\d)\.(\d))(?:-(\d{8}))?$", model)
    if m_dot:
        dotted = m_dot.group(1)
        major = m_dot.group(2)
        minor = m_dot.group(3)
        date = m_dot.group(4)
        hyphen = f"claude-{dotted.split('-', 1)[1]}".replace(f"-{major}.{minor}", f"-{major}-{minor}")

        _add(hyphen)
        if hyphen == "claude-opus-4-6":
            _add(f"{hyphen}-{_ANYROUTER_OPUS46_DEFAULT_DATE}")  # 固定已知可用日期
        if date:
            _add(f"{hyphen}-{date}")
        _add(dotted)

    # 3) 通用日期裁剪兜底
    no_date = re.sub(r"-\d{8}$", "", model)
    if no_date != model:
        _add(no_date)

    # 把当前模型本身放到最后过滤逻辑里处理，这里只返回候选全集
    return candidates


def _maybe_apply_anyrouter_model_fallback(
    *,
    backend_key: str,
    status_code: Optional[int],
    error_text: str,
    body: Any,
    tried_models: set[str],
) -> Tuple[Any, Optional[str], bool]:
    """
    在 AnyRouter 遇到“模型格式/命名不兼容”时，自动切换到下一候选模型并重试。
    """
    if backend_key != "anyrouter" or not isinstance(body, dict):
        return body, None, False
    if not _is_anyrouter_model_error(status_code, error_text):
        return body, None, False

    current_model = body.get("model")
    if not isinstance(current_model, str) or not current_model.strip():
        return body, None, False

    current_norm = current_model.strip().lower()
    if current_norm not in tried_models:
        tried_models.add(current_norm)

    for candidate in _build_anyrouter_model_fallback_candidates(current_model):
        if candidate == current_norm or candidate in tried_models:
            continue
        tried_models.add(candidate)
        new_body = {**body, "model": candidate}
        log.warning(
            f"[GATEWAY] anyrouter: model fallback triggered "
            f"(status={status_code}) {current_model} -> {candidate}",
            tag="GATEWAY",
        )
        return new_body, candidate, True

    return body, None, False


# ---------------------------------------------------------------------------
# [FIX 2026-02-12] Antigravity-Tools Thinking 存活补丁
# ---------------------------------------------------------------------------
# Antigravity-Manager 的 request.rs 有一个 "智能降级" 机制:
#   - should_disable_thinking_due_to_history(): 如果最后一条 assistant 消息
#     有 tool_use 但没有 thinking 块 → 自动禁用 thinking
#   - has_valid_signature_for_function_calls(): 如果消息有 tool_use 但没有
#     ≥50 字符的 thinking signature → 自动禁用 thinking
#
# Claude Code 的对话历史几乎总是有大量 tool_use（MCP 工具调用），
# 但这些历史消息不包含 thinking 块，导致 thinking 被永久禁用。
#
# 解决方案: 在转发请求前，给有 tool_use 但没有 thinking 的 assistant 消息
# 注入一个最小的 thinking 块。这个 thinking 块会在 Antigravity-Manager 的
# handlers/claude.rs 中被降级为纯 text（不影响 Gemini API 调用），
# 但它能骗过上述两个检查，让 thinking 保持启用状态。
# ---------------------------------------------------------------------------

# 64 字符的占位签名（满足 MIN_SIGNATURE_LENGTH = 50 的要求）
_DUMMY_THINKING_SIGNATURE = (
    "dummysig-antigravity-thinking-keepalive-placeholder-xxxxxxxxxxxxxxxx"
)


def _inject_thinking_blocks_for_antigravity_tools(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    为 Antigravity-Tools 后端注入 thinking 块，防止 thinking 被自动禁用。

    遍历 messages 中的 assistant 消息，如果有 tool_use 块但没有 thinking 块，
    则在 content 数组开头插入一个最小的 thinking 块（带 ≥50 字符的 dummy signature）。

    Args:
        body: 请求体字典 (会被 shallow-copy 后修改)

    Returns:
        修改后的请求体 (新字典，不修改原始 body)
    """
    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        return body

    modified = False
    new_messages = []

    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            new_messages.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            new_messages.append(msg)
            continue

        has_tool_use = any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        )
        has_thinking = any(
            isinstance(block, dict) and block.get("type") == "thinking"
            for block in content
        )

        if has_tool_use and not has_thinking:
            # Inject a minimal thinking block at the beginning
            thinking_block = {
                "type": "thinking",
                "thinking": "(thinking context preserved)",
                "signature": _DUMMY_THINKING_SIGNATURE,
            }
            new_content = [thinking_block] + list(content)
            new_msg = {**msg, "content": new_content}
            new_messages.append(new_msg)
            modified = True
        else:
            new_messages.append(msg)

    if modified:
        log.info(
            f"[GATEWAY] Injected thinking blocks into assistant messages "
            f"to prevent Antigravity-Tools auto-disable"
        )
        return {**body, "messages": new_messages}

    return body


def _mark_rate_limit_for_response(
    *,
    backend_key: Optional[str],
    client_type: Optional[str],
    status_code: int,
    account_id: str,
    model_account_id: Optional[str],
    backend_limit_id: str,
    headers: Dict[str, str],
    error_body: str,
    model_name: Optional[str],
) -> None:
    if status_code not in (429, 500, 503, 529):
        return

    # AnyRouter + Cursor 的 invalid request 属于请求画像/参数不兼容，不应触发 5xx 软避让。
    if (
        backend_key == "anyrouter"
        and str(client_type or "").lower() == "cursor"
        and _is_anyrouter_invalid_request_error(status_code, error_body)
    ):
        return

    from .rate_limit_handler import parse_rate_limit_from_response

    if status_code == 429:
        parse_rate_limit_from_response(
            account_id=account_id,
            status_code=status_code,
            headers=headers,
            error_body=error_body,
            model=model_name,
        )
        if model_account_id:
            parse_rate_limit_from_response(
                account_id=model_account_id,
                status_code=status_code,
                headers=headers,
                error_body=error_body,
                model=model_name,
            )
        return

    parse_rate_limit_from_response(
        account_id=backend_limit_id,
        status_code=status_code,
        headers=headers,
        error_body=error_body,
        model=None,
    )


# ==================== 代理处理器类 ====================

class ProxyHandler:
    """
    代理处理器

    支持依赖注入本地处理器，避免硬编码直调。
    """

    def __init__(
        self,
        local_handler: Optional[Callable] = None,
        backends: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """
        初始化代理处理器

        Args:
            local_handler: 本地处理器函数 (用于 antigravity 后端直调)
            backends: 后端配置字典 (默认使用全局配置)
        """
        self._local_handler = local_handler
        self._backends = backends or BACKENDS

    async def proxy_request(
        self,
        backend_key: str,
        endpoint: str,
        body: Dict[str, Any],
        headers: Dict[str, str],
        stream: bool = False,
        method: str = "POST",
    ) -> Tuple[bool, Any]:
        """
        代理请求到后端

        Args:
            backend_key: 后端标识
            endpoint: API 端点
            body: 请求体
            headers: 请求头
            stream: 是否流式响应
            method: HTTP 方法

        Returns:
            Tuple[bool, Any]: (成功标志, 响应内容或错误信息)
        """
        return await proxy_request_to_backend(
            backend_key=backend_key,
            endpoint=endpoint,
            method=method,
            headers=headers,
            body=body,
            stream=stream,
            local_handler=self._local_handler,
            backends=self._backends,
        )


# ==================== Content-Level Validation ====================

def _is_valid_chat_completion_response(response_data: Any, backend_key: str = "") -> bool:
    """
    [FIX 2026-02-26] Content-Level Validation — the critical 4th validation layer.

    Validation layers:
      Layer 1: HTTP status code (< 400)        ← already exists
      Layer 2: Content-Type (not text/html)     ← already exists
      Layer 3: JSON parsability                 ← already exists
      Layer 4: THIS — actual content structure  ← NEW

    Without this layer, garbage 200 responses (e.g., "version out of date",
    maintenance pages returned as valid JSON) short-circuit the entire 16-step
    fallback chain. The proxy returns (True, garbage_data) and anyrouter, ruoli,
    copilot, and all other backends are NEVER tried.

    Only validates non-streaming /chat/completions responses.

    Args:
        response_data: Parsed JSON response data
        backend_key: Backend identifier for logging

    Returns:
        True if response appears to be a valid OpenAI-format chat completion response
    """
    if not isinstance(response_data, dict):
        log.warning(
            f"[{backend_key}] Content validation failed: response is not a dict "
            f"(type={type(response_data).__name__})",
            tag="GATEWAY",
        )
        return False

    # Error response without choices → invalid
    if "error" in response_data and "choices" not in response_data:
        error_preview = str(response_data.get("error", ""))[:200]
        log.warning(
            f"[{backend_key}] Content validation failed: error response without choices: {error_preview}",
            tag="GATEWAY",
        )
        return False

    # Must have non-empty "choices" array
    choices = response_data.get("choices")
    if not choices or not isinstance(choices, list) or len(choices) == 0:
        keys_preview = list(response_data.keys())[:10]
        log.warning(
            f"[{backend_key}] Content validation failed: missing or empty 'choices'. "
            f"Response keys: {keys_preview}",
            tag="GATEWAY",
        )
        return False

    # First choice must have "message" or "delta"
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        log.warning(
            f"[{backend_key}] Content validation failed: first choice is not a dict",
            tag="GATEWAY",
        )
        return False

    message = first_choice.get("message") or first_choice.get("delta")
    if not message or not isinstance(message, dict):
        choice_keys = list(first_choice.keys())
        log.warning(
            f"[{backend_key}] Content validation failed: no 'message'/'delta' in first choice. "
            f"Choice keys: {choice_keys}",
            tag="GATEWAY",
        )
        return False

    # Message must have at least one meaningful field
    has_content = message.get("content") is not None  # empty string "" is valid
    has_tool_calls = bool(message.get("tool_calls"))
    has_refusal = message.get("refusal") is not None
    has_function_call = bool(message.get("function_call"))

    if not (has_content or has_tool_calls or has_refusal or has_function_call):
        msg_keys = list(message.keys())
        log.warning(
            f"[{backend_key}] Content validation failed: message has no "
            f"content/tool_calls/refusal/function_call. Message keys: {msg_keys}",
            tag="GATEWAY",
        )
        return False

    return True


# ==================== Copilot Output-Style 强化注入 ====================
# [FIX 2026-03-13] GitHub Copilot API 倾向于忽略 system prompt 中的自定义输出样式指令。
# 解决方案: 从 Anthropic system 字段中提取 output-style 内容，
# 额外注入到最后一条 user 消息中，确保模型在对话上下文中也能看到样式指令。


def _extract_output_style_from_system(system) -> str | None:
    """
    Extract output-style content from Anthropic system field.

    Scans system text blocks for '# Output Style:' marker and returns
    everything from that marker to the end of the block.

    Args:
        system: Anthropic system field (str or list of TextBlocks)

    Returns:
        Extracted output-style text, or None if not found
    """
    texts = []
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
    elif isinstance(system, str):
        texts.append(system)

    for text in texts:
        # Look for output-style marker (both half-width and full-width colon)
        for marker in ("# Output Style:", "# Output Style："):
            idx = text.find(marker)
            if idx != -1:
                return text[idx:].strip()

    return None


def _reinforce_output_style_for_copilot(body: dict) -> dict:
    """
    Reinforce output-style directives for Copilot backend.

    GitHub Copilot API deprioritizes system prompt content, causing custom
    output style directives (/output-style) to be ignored. This function
    extracts output-style content from the Anthropic system field and injects
    it into the last user message as reinforcement.

    The content stays in the system field (for backends that respect it)
    AND appears in the user message (for Copilot which doesn't).

    Only applies to Anthropic /messages format requests.

    Args:
        body: Anthropic format request body with 'system' and 'messages'

    Returns:
        Modified body with output-style injected into last user message,
        or original body if no output-style found
    """
    system = body.get("system")
    if not system:
        return body

    # Step 1: Extract output-style content
    output_style_text = _extract_output_style_from_system(system)
    if not output_style_text:
        return body

    # Step 2: Find last user message to inject into
    messages = body.get("messages", [])
    if not messages:
        return body

    new_messages = list(messages)
    injected = False

    for i in range(len(new_messages) - 1, -1, -1):
        msg = new_messages[i]
        if msg.get("role") != "user":
            continue

        content = msg.get("content", "")
        reinforcement = (
            "<output_style_reinforcement>\n"
            "CRITICAL: The following output style rules MUST be followed "
            "in your response. These are non-negotiable directives:\n\n"
            f"{output_style_text}\n"
            "</output_style_reinforcement>"
        )

        # Inject as prepended content block (preserves original content structure)
        if isinstance(content, list):
            new_content = [
                {"type": "text", "text": reinforcement},
            ] + list(content)
            new_messages[i] = {**msg, "content": new_content}
        elif isinstance(content, str):
            new_messages[i] = {
                **msg,
                "content": [
                    {"type": "text", "text": reinforcement},
                    {"type": "text", "text": content},
                ],
            }
        else:
            continue

        injected = True
        break

    if not injected:
        return body

    log.info(
        f"Reinforced output-style for Copilot backend "
        f"({len(output_style_text)} chars injected into last user message)",
        tag="COPILOT",
    )
    return {**body, "messages": new_messages}


# ==================== 代理函数 ====================

async def proxy_request_to_backend(
    backend_key: str,
    endpoint: str,
    method: str,
    headers: Dict[str, str],
    body: Any,
    stream: bool = False,
    local_handler: Optional[Callable] = None,
    backends: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[bool, Any]:
    """
    代理请求到指定后端（带重试机制）

    Args:
        backend_key: 后端标识
        endpoint: API 端点
        method: HTTP 方法
        headers: 请求头
        body: 请求体
        stream: 是否流式响应
        local_handler: 本地处理器 (用于 antigravity 后端直调)
        backends: 后端配置字典

    Returns:
        Tuple[bool, Any]: (成功标志, 响应内容或错误信息)
    """
    if backends is None:
        backends = BACKENDS

    backend = backends.get(backend_key)
    if not backend:
        return False, f"Backend {backend_key} not found"

    # ==================== 本地 Antigravity：service 直调（避免 127.0.0.1 回环） ====================
    if backend_key == "gcli2api-antigravity" and endpoint == "/chat/completions" and method.upper() == "POST":
        # 尝试使用本地处理器
        handler = local_handler
        if handler is None:
            try:
                # STUB: antigravity_service (ENABLE_ANTIGRAVITY feature flag)
                try:
                    from akarins_gateway.gateway.backends.antigravity.service import handle_openai_chat_completions
                except ImportError:
                    handle_openai_chat_completions = None
                handler = handle_openai_chat_completions
            except ImportError:
                pass

        if handler is not None:
            try:
                # ✅ [FIX 2026-02-02] Gateway 快速失败模式：通过请求头通知 Antigravity
                # 遇到 429 时快速失败，让 Gateway 进行降级到其他后端
                gateway_headers = dict(headers) if isinstance(headers, dict) else dict(headers)
                gateway_headers["x-gateway-fast-fail"] = "true"
                
                resp = await handler(body=body, headers=gateway_headers)

                status_code = getattr(resp, "status_code", 200)
                if stream:
                    if status_code >= 400:
                        async def error_stream():
                            error_msg = json.dumps({"error": "Backend error", "status": status_code})
                            yield f"data: {error_msg}\n\n"
                        return True, error_stream()

                    if isinstance(resp, StarletteStreamingResponse):
                        return True, resp.body_iterator

                    # 非预期：流式请求返回了非 StreamingResponse
                    return False, f"Backend error: {status_code}"

                # 非流式
                if status_code >= 400:
                    return False, f"Backend error: {status_code}"

                resp_body = getattr(resp, "body", b"")
                if isinstance(resp_body, bytes):
                    parsed = json.loads(resp_body.decode("utf-8", errors="ignore") or "{}")
                elif isinstance(resp_body, str):
                    parsed = json.loads(resp_body or "{}")
                else:
                    parsed = resp_body

                # [FIX 2026-02-26] Content-Level Validation: reject garbage 200 responses
                # that would short-circuit the entire fallback chain
                if not _is_valid_chat_completion_response(parsed, backend_key):
                    return False, f"{backend_key}: Invalid chat completion response structure"
                return True, parsed

            except HTTPException as e:
                if stream:
                    status = int(getattr(e, "status_code", 500))

                    async def error_stream(status_code: int = status):
                        error_msg = json.dumps({"error": "Backend error", "status": status_code})
                        yield f"data: {error_msg}\n\n"
                    return True, error_stream()
                return False, f"Backend error: {e.status_code}"
            except Exception as e:
                log.error(f"Local antigravity service call failed: {e}", tag="GATEWAY")
                if stream:
                    msg = str(e)

                    async def error_stream(error_message: str = msg):
                        error_msg = json.dumps({"error": error_message})
                        yield f"data: {error_msg}\n\n"
                    return True, error_stream()
                return False, str(e)

    # 保留调用方传入的原始 endpoint，后续即使被重写也能基于原始端点做判断
    original_endpoint = endpoint

    # 对 Copilot 后端应用模型名称映射
    if backend_key == "copilot" and body and isinstance(body, dict) and "model" in body:
        original_model = body.get("model", "")
        mapped_model = map_model_for_copilot(original_model)
        if mapped_model != original_model:
            if hasattr(log, 'route'):
                log.route(f"Model mapped: {original_model} -> {mapped_model}", tag="COPILOT")
            body = {**body, "model": mapped_model}

    # [FIX 2026-03-13] Copilot 后端: 强化 output-style 指令
    # GitHub Copilot API 倾向于忽略 system prompt 中的自定义输出样式指令（如 /output-style）
    # 从 Anthropic system 字段提取 output-style 内容，额外注入到最后一条 user 消息中
    if backend_key == "copilot" and original_endpoint == "/messages" and body and isinstance(body, dict):
        body = _reinforce_output_style_for_copilot(body)

    # ==================== Kiro Gateway: OpenAI -> Anthropic 格式转换 ====================
    # [FIX 2026-01-19] Kiro Gateway 使用 Anthropic 格式的 /messages 端点
    # [FIX 2026-03-10] 只有 Claude 模型才需要 OpenAI->Anthropic 转换
    # DeepSeek/MiniMax/Qwen 等非 Claude 模型保持 OpenAI /chat/completions 格式
    # 这里使用 original_endpoint 判断是否来自 OpenAI /chat/completions，避免后续重写影响
    if backend_key == "kiro-gateway" and original_endpoint == "/chat/completions" and body and isinstance(body, dict):
        model_name = body.get("model", "unknown")
        _is_claude_model = model_name.lower().startswith("claude")

        if _is_claude_model:
            # Claude 模型：转换为 Anthropic /messages 格式
            endpoint = "/messages"
            original_max_tokens = body.get("max_tokens", "not_set")
            log.info(f"[GATEWAY] 🎯 KIRO GATEWAY: Converting endpoint /chat/completions -> /messages (model={model_name}, original_max_tokens={original_max_tokens})", tag="GATEWAY")

            # 转换请求体: OpenAI -> Anthropic 格式
            body = _convert_openai_to_anthropic_body(body)
            converted_model = body.get("model", "unknown")
            converted_max_tokens = body.get("max_tokens", "not_set")
            log.info(f"[GATEWAY] 🎯 KIRO GATEWAY: Converted request body to Anthropic format (model={converted_model}, max_tokens={converted_max_tokens}, stream={body.get('stream', False)})", tag="GATEWAY")

            # [DEBUG] 输出转换后的消息数量和工具定义
            messages_count = len(body.get("messages", []))
            has_tools = "tools" in body
            tools_count = len(body.get("tools", [])) if has_tools else 0
            has_thinking = "thinking" in body

            # [DEBUG] 统计消息的 token 数量（粗略估算）
            total_message_chars = 0
            for msg in body.get("messages", []):
                content = msg.get("content", "")
                if isinstance(content, str):
                    total_message_chars += len(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and "text" in part:
                            total_message_chars += len(part["text"])
            estimated_tokens = total_message_chars // 4  # 粗略估算：4 字符 ≈ 1 token

            log.info(
                f"[GATEWAY] 🎯 KIRO GATEWAY: messages_count={messages_count}, "
                f"estimated_tokens={estimated_tokens}, has_tools={has_tools}, "
                f"tools_count={tools_count}, has_thinking={has_thinking}",
                tag="GATEWAY"
            )
        else:
            # [FIX 2026-03-10] 非 Claude 模型（DeepSeek/MiniMax/Qwen 等）：
            # 保持 OpenAI /chat/completions 格式，不做任何转换
            log.info(
                f"[GATEWAY] 🎯 KIRO GATEWAY: Non-Claude model {model_name}, "
                f"keeping OpenAI /chat/completions format (no conversion)",
                tag="GATEWAY"
            )

    # ==================== [REFACTOR 2026-02-14] Public Station: Unified Handling ====================
    # All public station behaviors (auth, UA, thinking strip, format conversion, URL routing)
    # are now delegated to PublicStationManager, replacing ~160 lines of hardcoded if/elif chains.
    _psm = _get_psm()
    _is_public = _psm.is_public_station(backend_key)
    _public_response_conversion_required = False
    _anyrouter_use_openai_endpoint = False
    _anyrouter_client_type = _detect_client_type_for_capture(headers) if backend_key == "anyrouter" else ""
    # TODO(cursor): Cursor → AnyRouter /chat/completions path — ported from gcli2api, untested.
    #   Includes: request mode detection, OpenAI→Anthropic conversion, model normalization,
    #   field whitelist, tool filtering, CC header injection, UA override.
    #   Claude Code → /v1/messages path is DONE and tested.
    if backend_key == "anyrouter" and original_endpoint == "/chat/completions":
        # AnyRouter request mode:
        # - openai: force OpenAI /chat/completions passthrough
        # - anthropic: force OpenAI->Anthropic /v1/messages conversion
        # - auto (default): Cursor client prefers OpenAI passthrough, others keep Anthropic conversion
        _anyrouter_request_mode = os.getenv("ANYROUTER_REQUEST_MODE", "auto").strip().lower()
        _legacy_openai_toggle = os.getenv("ANYROUTER_USE_OPENAI_ENDPOINT", "").strip().lower() in ("1", "true", "yes", "on")
        if _legacy_openai_toggle:
            _anyrouter_request_mode = "openai"

        if _anyrouter_request_mode in ("openai", "force_openai"):
            _anyrouter_use_openai_endpoint = True
        elif _anyrouter_request_mode in ("anthropic", "force_anthropic"):
            _anyrouter_use_openai_endpoint = False
        else:
            try:
                from akarins_gateway.ide_compat import ClientTypeDetector
                from akarins_gateway.ide_compat.client_detector import ClientType
                _client_info = ClientTypeDetector.detect(headers)
                _anyrouter_client_type = str(_client_info.client_type.value)
                # [FIX 2026-02-17] AnyRouter auto mode: Cursor 默认走 Anthropic /v1/messages
                # 实测结论：
                # 1) /v1/chat/completions 在当前 AnyRouter 账号下对 Claude 模型返回“不支持模型”
                # 2) Cursor -> Anthropic 转换路径原先会剥离日期后缀，导致 claude-opus-4-6 触发
                #    \"invalid claude code request\"
                # 因此 auto 模式下对 Cursor 关闭 OpenAI passthrough，改走 Anthropic 路径。
                _anyrouter_use_openai_endpoint = False
                log.info(
                    f"[GATEWAY] anyrouter request mode=auto, detected client={_client_info.client_type.value}, "
                    f"use_openai_endpoint={_anyrouter_use_openai_endpoint}",
                    tag="GATEWAY"
                )
            except Exception as detect_err:
                # Detection failure falls back to Anthropic path (current stable behavior)
                log.warning(
                    f"[GATEWAY] anyrouter auto mode client detection failed, fallback to anthropic conversion: {detect_err}",
                    tag="GATEWAY"
                )
                _anyrouter_use_openai_endpoint = False
                _anyrouter_client_type = _detect_client_type_for_capture(headers)

    # AnyRouter: format conversion and endpoint handling
    anyrouter_base_url = None
    anyrouter_api_key = None
    if _is_public and _psm.needs_request_conversion(backend_key):
        # AnyRouter 使用 Anthropic 格式，需要特殊处理
        station = _psm.get(backend_key)
        if station:
            anyrouter_base_url, anyrouter_api_key = station.get_rotation_endpoint()

        if not anyrouter_base_url or not anyrouter_api_key:
            log.warning(f"{backend_key}: No endpoints or API keys configured", tag="GATEWAY")
            return False, f"{backend_key} not configured"

        # OpenAI format -> Anthropic format conversion
        if original_endpoint == "/chat/completions" and body and isinstance(body, dict):
            if _anyrouter_use_openai_endpoint:
                # OpenAI-compatible providers typically expose /v1/chat/completions.
                # Using /chat/completions may hit a website route and return HTML challenge pages.
                endpoint = "/v1/chat/completions"
                # [FIX 2026-02-16] AnyRouter OpenAI 端点模型名规范化
                # 以 AnyRouter /v1/models 实测结果为准：Claude 模型使用连字符版本号
                # （例如 claude-opus-4-6，而非 claude-opus-4.6）。
                # 因此这里将点号版本统一转为连字符版本。
                _raw_model = body.get("model")
                if isinstance(_raw_model, str) and _raw_model.strip():
                    import re as _re
                    _normalized_model = _raw_model.strip().lower()
                    # 去掉 thinking 与日期后缀，避免 OpenAI 兼容端点误判
                    _normalized_model = _re.sub(r"-thinking$", "", _normalized_model)
                    _normalized_model = _re.sub(r"-\d{8}$", "", _normalized_model)
                    # 版本格式统一：claude-xxx-4.6 -> claude-xxx-4-6
                    _m = _re.match(
                        r"^(claude-(?:opus|sonnet|haiku))-(\d)\.(\d)$",
                        _normalized_model,
                    )
                    if _m:
                        _normalized_model = f"{_m.group(1)}-{_m.group(2)}-{_m.group(3)}"
                    if _normalized_model != _raw_model:
                        body = {**body, "model": _normalized_model}
                        log.info(
                            f"[GATEWAY] {backend_key}: Normalized model for AnyRouter OpenAI endpoint: "
                            f"{_raw_model} -> {_normalized_model}",
                            tag="GATEWAY",
                        )
                log.warning(
                    f"{backend_key}: ANYROUTER_USE_OPENAI_ENDPOINT enabled, bypassing OpenAI->Anthropic conversion",
                    tag="GATEWAY",
                )
            else:
                endpoint = "/v1/messages"
                _public_response_conversion_required = True
                log.debug(f"{backend_key}: Converting endpoint /chat/completions -> /v1/messages", tag="GATEWAY")
                body = _convert_openai_to_anthropic_body(body)
                log.debug(f"{backend_key}: Converted request body to Anthropic format", tag="GATEWAY")

            if not _anyrouter_use_openai_endpoint:
                # [FIX 2026-02-17] AnyRouter Anthropic 模式首发模型与 Claude Code 对齐
                # 结论：Claude Code 直通 /messages 成功时使用的是 claude-opus-4-6（无日期后缀）。
                # 为避免 OpenAI->Anthropic 转换路径把 Cursor 请求固定到某个日期版本池，
                # 这里将 claude-opus-4-6-YYYYMMDD 归一为 claude-opus-4-6 作为 primary。
                # 若上游报模型不支持/invalid，再由 _maybe_apply_anyrouter_model_fallback() 尝试 dated 候选。
                if isinstance(body, dict):
                    _model_val = body.get("model")
                    if isinstance(_model_val, str):
                        _model_norm = _model_val.strip().lower()
                        _m = re.match(r"^(claude-opus-4-6)-\d{8}$", _model_norm)
                        if _m:
                            _primary_model = _m.group(1)
                            if _primary_model != _model_norm:
                                body = {**body, "model": _primary_model}
                                log.info(
                                    f"[GATEWAY] {backend_key}: Normalized AnyRouter Anthropic primary model: "
                                    f"{_model_val} -> {_primary_model}",
                                    tag="GATEWAY",
                                )
                        else:
                            log.info(
                                f"[GATEWAY] {backend_key}: Keep Anthropic model for AnyRouter: {_model_val}",
                                tag="GATEWAY",
                            )

                # [FIX 2026-02-17] AnyRouter + Cursor：将“全 user 文本消息”合并为单条 user
                # 目的：对齐 Claude Code 的消息打包形态，规避 AnyRouter 的请求画像校验误判。
                if (
                    _env_flag("ANYROUTER_CURSOR_MERGE_USER_MESSAGES", default=True)
                    and _anyrouter_client_type == "cursor"
                ):
                    _orig_messages = body.get("messages") if isinstance(body, dict) else None
                    _merged_messages, _did_merge = _merge_all_user_text_messages_for_anyrouter(_orig_messages)
                    if _did_merge and isinstance(body, dict):
                        body = {**body, "messages": _merged_messages}
                        log.info(
                            f"[GATEWAY] {backend_key}: Merged Cursor user messages for AnyRouter "
                            f"({len(_orig_messages)} -> {len(_merged_messages)})",
                            tag="GATEWAY",
                        )
                if (
                    _env_flag("ANYROUTER_CURSOR_STRIP_MCP_INSTRUCTIONS", default=True)
                    and _anyrouter_client_type == "cursor"
                    and isinstance(body, dict)
                ):
                    _stripped_messages, _strip_changed, _strip_summary = _strip_anyrouter_cursor_mcp_instructions(
                        body.get("messages")
                    )
                    if _strip_changed:
                        body = {**body, "messages": _stripped_messages}
                        log.info(
                            f"[GATEWAY] {backend_key}: Stripped mcp_instructions for Cursor->AnyRouter "
                            f"(messages={_strip_summary.get('messages')}, "
                            f"blocks_changed={_strip_summary.get('blocks_changed')}, "
                            f"blocks_checked={_strip_summary.get('blocks_checked')})",
                            tag="GATEWAY",
                        )

                # TODO(cursor): Cursor body profile — ported from gcli2api, untested in akarins-gateway.
                #   Includes: adaptive profile, max_tokens cap, tool filtering, schema profiling.
                # [FIX 2026-02-17] AnyRouter + Cursor：补齐 Claude Code 常见字段画像
                # - 添加 metadata（若缺失）
                # - 添加 system（若缺失）
                # - 将 max_tokens 收敛到 32000（与 Claude Code 常见值一致）
                if (
                    _env_flag("ANYROUTER_CURSOR_BODY_PROFILE", default=True)
                    and _anyrouter_client_type == "cursor"
                    and isinstance(body, dict)
                ):
                    _body_changed = False
                    _profiled_body, _profiled_changed = _apply_anyrouter_cursor_profile_adaptive(
                        body,
                        headers,
                        force_adaptive_thinking=True,
                    )
                    if _profiled_changed and isinstance(_profiled_body, dict):
                        body = _profiled_body
                        _body_changed = True
                    _cursor_profile_max_tokens = int(os.getenv("ANYROUTER_CURSOR_MAX_TOKENS", "32000"))
                    _cur_max_tokens = body.get("max_tokens")
                    if isinstance(_cur_max_tokens, (int, float)) and _cur_max_tokens > _cursor_profile_max_tokens:
                        body["max_tokens"] = _cursor_profile_max_tokens
                        _body_changed = True
                    _filtered_tools, _tools_filtered, _filter_summary = _filter_anyrouter_cursor_tools(body.get("tools"))
                    if _tools_filtered:
                        body["tools"] = _filtered_tools
                        _body_changed = True
                        log.info(
                            f"[GATEWAY] {backend_key}: Filtered Cursor tools for AnyRouter "
                            f"(original={_filter_summary.get('original')}, kept={_filter_summary.get('kept')}, "
                            f"dropped_prefix={_filter_summary.get('dropped_prefix')}, "
                            f"trimmed={_filter_summary.get('trimmed_to_max')})",
                            tag="GATEWAY",
                        )
                    if _env_flag("ANYROUTER_CURSOR_TOOL_SCHEMA_PROFILE", default=True):
                        _updated_tools, _tools_changed = _apply_anyrouter_cursor_tools_schema_profile(body.get("tools"))
                        if _tools_changed:
                            body["tools"] = _updated_tools
                            _body_changed = True
                    if _body_changed:
                        log.info(
                            f"[GATEWAY] {backend_key}: Applied Cursor body profile "
                            f"(has_system={'system' in body}, has_metadata={'metadata' in body}, "
                            f"max_tokens={body.get('max_tokens')}, tools={len(body.get('tools') or [])})",
                            tag="GATEWAY",
                        )

            if not _anyrouter_use_openai_endpoint:
                # [DEBUG 2026-02-16] Codex 协助诊断：dump 转换后完整请求体关键字段
                if isinstance(body, dict):
                    _body_keys = list(body.keys())
                    _model_val = body.get("model", "N/A")
                    _max_tokens_val = body.get("max_tokens", "N/A")
                    _has_thinking = "thinking" in body
                    _has_stop = "stop" in body
                    _has_stop_seq = "stop_sequences" in body
                    _has_system = "system" in body
                    _has_tools = "tools" in body
                    _has_metadata = "metadata" in body
                    _msg_count = len(body.get("messages", []))
                    _msg_summary = _summarize_anthropic_messages(body.get("messages", []))
                    _tools_summary = _summarize_anthropic_tools(body.get("tools"))
                    log.info(
                        f"[GATEWAY] {backend_key}: Converted body dump → "
                        f"keys={_body_keys}, model={_model_val}, max_tokens={_max_tokens_val}, "
                        f"msgs={_msg_count}, thinking={_has_thinking}, "
                        f"stop={_has_stop}, stop_sequences={_has_stop_seq}, "
                        f"system={_has_system}, tools={_has_tools}, metadata={_has_metadata}, "
                        f"msg_summary={_msg_summary}, tools_summary={_tools_summary}",
                        tag="GATEWAY"
                    )

                    # [FIX 2026-02-16] 公益站 Anthropic 请求体字段白名单
                    # Codex debug 诊断: 转换后可能残留 OpenAI 专属字段（stop, n, logprobs 等），
                    # 导致 AnyRouter 返回 "invalid claude code request"。
                    # 只保留 Anthropic Messages API 合法字段。
                    _ANTHROPIC_ALLOWED_FIELDS = {
                        "model", "messages", "system", "max_tokens", "stream",
                        "thinking", "stop_sequences", "temperature", "top_p", "top_k",
                        "tools", "tool_choice", "metadata",
                    }
                    _extra_keys = set(body.keys()) - _ANTHROPIC_ALLOWED_FIELDS
                    if _extra_keys:
                        body = {k: v for k, v in body.items() if k in _ANTHROPIC_ALLOWED_FIELDS}
                        log.info(
                            f"[GATEWAY] {backend_key}: Stripped non-Anthropic fields: {_extra_keys}",
                            tag="GATEWAY"
                        )

                    # [ROLLBACK 2026-02-16] thinking/max_tokens 限制已回滚
                    # 实测证明：剥离 thinking + cap max_tokens 后仍然 500 "invalid claude code request"
                    # 因此 thinking 和 max_tokens 不是根因，保留原始值以维持 thinking 性能
        elif endpoint == "/messages" or endpoint == "/v1/messages":
            if not endpoint.startswith("/v1"):
                endpoint = "/v1/messages"
            log.debug(f"{backend_key}: Using Anthropic format endpoint {endpoint}", tag="GATEWAY")
            if backend_key == "anyrouter" and isinstance(body, dict):
                _msg_summary = _summarize_anthropic_messages(body.get("messages", []))
                _tools_summary = _summarize_anthropic_tools(body.get("tools"))
                log.info(
                    f"[GATEWAY] {backend_key}: Anthropic passthrough "
                    f"model={body.get('model', 'N/A')}, keys={list(body.keys())}, "
                    f"max_tokens={body.get('max_tokens', 'N/A')}, "
                    f"thinking={'thinking' in body}, tools={'tools' in body}, "
                    f"msg_summary={_msg_summary}, tools_summary={_tools_summary}",
                    tag="GATEWAY",
                )

    log.debug(f"{backend_key}: Using base URL {anyrouter_base_url}", tag="GATEWAY")

    # Build URL dynamically per attempt so public-station rotation can take effect immediately.
    def _build_effective_url_for_attempt() -> str:
        if _is_public:
            effective_url = _psm.get_effective_url(
                backend_key,
                backend.get("base_url", ""),
                endpoint,
            )
            if effective_url:
                return effective_url
        return f"{backend.get('base_url', '')}{endpoint}"

    url = _build_effective_url_for_attempt()

    # 根据请求类型选择超时时间
    if stream:
        timeout = backend.get("stream_timeout", backend.get("timeout", 300.0))
    else:
        timeout = backend.get("timeout", 60.0)

    # 获取最大重试次数
    max_retries = backend.get("max_retries", RETRY_CONFIG.get("max_retries", 3))
    # [FIX 2026-02-17] AnyRouter 至少保留 2 次重试（3 次总尝试）
    # 原因：当前 anyrouter 配置 max_retries=1，在 URL 轮换后经常只剩最后一次可用尝试，
    # 模型 fallback 会被触发但无法真正发起下一次请求。
    if backend_key == "anyrouter" and max_retries < 2:
        log.info(
            f"[GATEWAY] anyrouter: raise max_retries {max_retries} -> 2 for model-fallback effectiveness",
            tag="GATEWAY",
        )
        max_retries = 2
    if (
        backend_key == "anyrouter"
        and _anyrouter_client_type == "cursor"
        and not _anyrouter_use_openai_endpoint
        and max_retries < 4
    ):
        log.info(
            f"[GATEWAY] anyrouter: raise max_retries {max_retries} -> 4 for cursor profile-fallback effectiveness",
            tag="GATEWAY",
        )
        max_retries = 4

    # 构建请求头
    request_headers = {
        "Content-Type": "application/json",
        "Authorization": headers.get("authorization", "Bearer dummy"),
    }
    # [FIX 2026-02-02] P0: 流式请求必须声明 Accept: text/event-stream，避免公益站返回 HTML
    if stream:
        request_headers["Accept"] = "text/event-stream"
    # Preserve upstream client identity (important for backend routing/features)
    user_agent = headers.get("user-agent") or headers.get("User-Agent")
    if user_agent:
        request_headers["User-Agent"] = user_agent
        # Keep a copy even if a downstream client overwrites User-Agent
        request_headers["X-Forwarded-User-Agent"] = user_agent

    # Forward a small allowlist of gateway control headers
    for h in (
        "x-augment-client",
        "x-bugment-client",
        "x-augment-request",
        "x-bugment-request",
        # Augment signed-request headers (preserve for downstream logging/compat)
        "x-signature-version",
        "x-signature-timestamp",
        "x-signature-signature",
        "x-signature-vector",
        "x-disable-thinking-signature",
        "x-request-id",
        # [FIX 2026-01-20] SCID 架构 - 转发会话ID header
        "x-ag-conversation-id",
        "x-conversation-id",
        # [FIX 2026-02-14] 转发 Anthropic API 关键 headers
        # 公益站（AnyRouter/Ruoli）需要这些 header 来正确处理 thinking 和新模型
        "anthropic-version",
        "anthropic-beta",
        "anthropic-dangerous-direct-browser-access",
    ):
        v = headers.get(h) or headers.get(h.lower()) or headers.get(h.upper())
        if v:
            request_headers[h] = v

    # [REFACTOR 2026-02-14] Public station: unified auth + header injection
    if _is_public:
        request_headers = _psm.prepare_headers(backend_key, request_headers, backend)
        log.debug(
            f"{backend_key}: Headers prepared via PublicStationManager "
            f"(anthropic-version={request_headers.get('anthropic-version', 'N/A')}, "
            f"has_auth={'Authorization' in request_headers or 'x-api-key' in request_headers})",
            tag="GATEWAY"
        )
        if _anyrouter_use_openai_endpoint:
            # OpenAI endpoint path does not need Anthropic-specific headers.
            request_headers.pop("anthropic-version", None)
            request_headers.pop("anthropic-beta", None)
            request_headers.pop("anthropic-dangerous-direct-browser-access", None)
            # AnyRouter OpenAI-compatible endpoint expects Bearer token.
            # PublicStationManager sets x-api-key for Anthropic mode, so we bridge it here.
            _anyrouter_api_key = (
                request_headers.get("x-api-key")
                or request_headers.get("X-API-Key")
            )
            if _anyrouter_api_key:
                request_headers["Authorization"] = f"Bearer {_anyrouter_api_key}"
                log.info(
                    f"{backend_key}: Using OpenAI endpoint mode, bridged x-api-key -> Authorization",
                    tag="GATEWAY",
                )
            else:
                log.warning(
                    f"{backend_key}: OpenAI endpoint mode enabled but x-api-key missing",
                    tag="GATEWAY",
                )
            log.info(
                f"{backend_key}: Using OpenAI endpoint mode, stripped Anthropic headers",
                tag="GATEWAY",
            )
        elif backend_key == "anyrouter":
            # TODO(cursor): AnyRouter + Cursor CC header injection — untested in akarins-gateway.
            #   Injects Claude Code anthropic-beta profile + UA override for Cursor clients.
            # [FIX 2026-02-17] AnyRouter + Cursor: 对齐 Claude Code 的 Anthropic header 画像
            # 可通过 ANYROUTER_CURSOR_CLAUDE_CODE_HEADERS=0 快速关闭。
            if _env_flag("ANYROUTER_CURSOR_CLAUDE_CODE_HEADERS", default=True) and _anyrouter_client_type == "cursor":
                request_headers["anthropic-beta"] = _ANYROUTER_CLAUDE_CODE_HEADER_PROFILE
                request_headers["anthropic-dangerous-direct-browser-access"] = "true"
                log.info(
                    f"{backend_key}: Applied Claude Code header profile for Cursor "
                    f"(anthropic-beta={request_headers.get('anthropic-beta')})",
                    tag="GATEWAY",
                )
            # [FIX 2026-02-17] AnyRouter + Cursor: 对齐 Claude Code 的 forwarded UA 画像
            if _env_flag("ANYROUTER_CURSOR_FORWARD_CLAUDE_UA", default=True) and _anyrouter_client_type == "cursor":
                _claude_cli_ua = os.getenv(
                    "ANYROUTER_CURSOR_FORWARD_UA",
                    "claude-cli/2.1.34 (external, cli)",
                ).strip()
                if _claude_cli_ua:
                    request_headers["X-Forwarded-User-Agent"] = _claude_cli_ua
                    if _env_flag("ANYROUTER_CURSOR_FORWARD_X_APP", default=False):
                        request_headers["x-app"] = "cli"
                    else:
                        request_headers.pop("x-app", None)
                    log.info(
                        f"{backend_key}: Applied Claude Code forwarded UA profile for Cursor "
                        f"(x-forwarded-user-agent={_claude_cli_ua})",
                        tag="GATEWAY",
                    )
            # [FIX 2026-02-17] AnyRouter 出站时剥离 SCID header，避免上游画像校验误判
            # 可通过 ANYROUTER_DROP_SCID_HEADER=0 关闭。
            if _env_flag("ANYROUTER_DROP_SCID_HEADER", default=True):
                _dropped = False
                if request_headers.pop("x-ag-conversation-id", None) is not None:
                    _dropped = True
                if request_headers.pop("x-conversation-id", None) is not None:
                    _dropped = True
                if _dropped:
                    log.info(
                        f"{backend_key}: Dropped SCID headers for downstream compatibility",
                        tag="GATEWAY",
                    )

    # [REFACTOR 2026-02-14] Public station: unified thinking suffix stripping
    if _is_public:
        body = _psm.prepare_body(backend_key, body)

    _anyrouter_tried_models: set[str] = set()
    if backend_key == "anyrouter" and isinstance(body, dict):
        _initial_model = body.get("model")
        if isinstance(_initial_model, str) and _initial_model.strip():
            _anyrouter_tried_models.add(_initial_model.strip().lower())
    _anyrouter_cursor_profile_stage = 0
    _anyrouter_capture_id = uuid.uuid4().hex[:12] if backend_key == "anyrouter" else ""

    last_error = None
    last_status_code = None
    
    # [FIX 2026-01-23] 生成 account_id（用于限流跟踪）
    account_id = _build_account_id(backend_key, headers, body)
    model_name = body.get("model") if isinstance(body, dict) else None
    model_account_id = _build_model_account_id(account_id, model_name)
    backend_limit_id = _build_backend_limit_id(backend_key)
    
    # [FIX 2026-01-23] 请求前检查限流状态
    from .rate_limit_handler import get_rate_limit_tracker
    tracker = get_rate_limit_tracker()
    
    # 检查后端级别限流（用于 5xx 软避让）
    if tracker.is_rate_limited(backend_limit_id):
        remaining = tracker.get_reset_seconds(backend_limit_id)
        if remaining:
            log.warning(f"[RATE_LIMIT] 后端 {backend_key} 软避让中，剩余 {remaining:.1f} 秒，跳过请求")
            return False, f"Backend {backend_key} cooling down, retry after {remaining:.1f}s"

    # 检查账号级别限流
    if tracker.is_rate_limited(account_id):
        remaining = tracker.get_reset_seconds(account_id)
        if remaining:
            log.warning(f"[RATE_LIMIT] 账号 {account_id} 仍在限流中，剩余 {remaining:.1f} 秒，跳过请求")
            return False, f"Account rate limited, retry after {remaining:.1f}s"
    
    # 检查模型级别限流（如果有模型名）
    if model_account_id:
        if tracker.is_rate_limited(model_account_id):
            remaining = tracker.get_reset_seconds(model_account_id)
            if remaining:
                log.warning(f"[RATE_LIMIT] 模型 {model_name} 仍在限流中，剩余 {remaining:.1f} 秒，跳过请求")
                return False, f"Model {model_name} rate limited, retry after {remaining:.1f}s"
    
    # [FIX 2026-01-23] 使用 RateLimiter 限制请求频率（主动削峰）
    # [FIX 2026-02-02] 间隔从 500ms 降低到 150ms，减少 Claude Code 高频调用延迟
    from akarins_gateway.core.rate_limiter import get_keyed_rate_limiter
    rate_limiter = get_keyed_rate_limiter(min_interval_ms=150)  # 最小间隔 150ms
    await rate_limiter.wait(_build_rate_limit_key(account_id, model_name))  # 等待直到可以进行下一次调用
    
    # [FIX 2026-01-23] 使用并发控制，限制每个后端的并发请求数
    # [FIX 2026-02-01] 导入 PermitAcquireTimeout 异常
    from .concurrency import BackendPermit, PermitAcquireTimeout

    _public_rotated_in_retry = False

    for attempt in range(max_retries + 1):  # +1 因为第一次不算重试
        try:
            if attempt > 0:
                # [FIX 2026-01-23] 使用改进的 calculate_retry_delay（支持 Retry-After 解析）
                # 注意：这里没有 status_code 等信息，使用默认指数退避
                delay = calculate_retry_delay(attempt - 1)
                log.warning(f"Retry {attempt}/{max_retries} for {backend_key} after {delay:.1f}s delay")
                await asyncio.sleep(delay)

            # 每次尝试都重新解析 URL，确保 Public Station 轮询可以在同一请求内生效
            url = _build_effective_url_for_attempt()
            log.debug(
                f"[GATEWAY] {backend_key}: attempt={attempt + 1}/{max_retries + 1}, url={url}",
                tag="GATEWAY",
            )
            if (
                backend_key == "anyrouter"
                and _anyrouter_client_type == "cursor"
                and not _anyrouter_use_openai_endpoint
            ):
                _stage_name = _ANYROUTER_CURSOR_PROFILE_NAMES[
                    min(_anyrouter_cursor_profile_stage, len(_ANYROUTER_CURSOR_PROFILE_NAMES) - 1)
                ]
                log.info(
                    f"[GATEWAY] anyrouter: cursor profile stage={_stage_name} (attempt={attempt + 1}/{max_retries + 1})",
                    tag="GATEWAY",
                )
            _write_anyrouter_capture(
                capture_id=_anyrouter_capture_id,
                backend_key=backend_key,
                incoming_headers=headers,
                outgoing_headers=request_headers,
                body=body,
                endpoint=endpoint,
                original_endpoint=original_endpoint,
                url=url,
                attempt=attempt + 1,
                max_attempts=max_retries + 1,
                use_openai_endpoint=_anyrouter_use_openai_endpoint,
            )

            # [FIX 2026-01-23] 获取后端并发许可（防止瞬时并发过高）
            async with BackendPermit(backend_key):
                if stream:
                    # 流式请求（带超时）
                    # 这里传入 original_endpoint，确保下游流式转换逻辑能够基于原始端点判断
                    stream_success, stream_result = await proxy_streaming_request_with_timeout(
                        url=url,
                        method=method,
                        headers=request_headers,
                        body=body,
                        timeout=timeout,
                        backend_key=backend_key,
                        client_type=_anyrouter_client_type,
                        endpoint=original_endpoint,
                        public_response_conversion_required=_public_response_conversion_required,
                        skip_rate_limit_check=(attempt > 0),
                    )
                    if stream_success:
                        return True, stream_result

                    last_error = str(stream_result)
                    _stream_error_lower = last_error.lower()
                    _stream_status_code = None
                    import re as _re
                    _status_match = _re.search(
                        r"(?:status(?:=|:)\s*|backend error:\s*)(\d{3})",
                        _stream_error_lower,
                    )
                    if _status_match:
                        _stream_status_code = int(_status_match.group(1))
                    last_status_code = _stream_status_code

                    # TODO(cursor): Cursor profile fallback in retry loop — untested.
                    (
                        body,
                        request_headers,
                        _anyrouter_cursor_profile_stage,
                        _profile_name,
                        _profile_switched,
                    ) = _maybe_apply_anyrouter_cursor_profile_fallback(
                        backend_key=backend_key,
                        client_type=_anyrouter_client_type,
                        use_openai_endpoint=_anyrouter_use_openai_endpoint,
                        status_code=_stream_status_code,
                        error_text=last_error,
                        body=body,
                        request_headers=request_headers,
                        incoming_headers=headers,
                        profile_stage=_anyrouter_cursor_profile_stage,
                    )
                    if _profile_switched and _profile_name and attempt < max_retries:
                        last_error = f"AnyRouter cursor profile fallback -> {_profile_name}"
                        continue

                    body, _fallback_model, _fallback_switched = _maybe_apply_anyrouter_model_fallback(
                        backend_key=backend_key,
                        status_code=_stream_status_code,
                        error_text=last_error,
                        body=body,
                        tried_models=_anyrouter_tried_models,
                    )
                    if _fallback_switched and _fallback_model and attempt < max_retries:
                        model_name = _fallback_model
                        model_account_id = _build_model_account_id(account_id, model_name)
                        last_error = f"AnyRouter model fallback -> {model_name}"
                        continue

                    _retryable_stream = False
                    if _stream_status_code is not None:
                        _retryable_stream = should_retry(
                            _stream_status_code,
                            attempt,
                            max_retries,
                            error_body=last_error,
                            account_id=account_id,
                            backend_limit_id=backend_limit_id,
                        )
                        if (
                            not _retryable_stream
                            and attempt < max_retries
                            and "html" in _stream_error_lower
                        ):
                            _retryable_stream = True
                        # 公益站轮转域名可能存在端点能力不一致：
                        # 某些镜像不支持 /v1/messages，会返回 404 page not found。
                        # 对此类错误继续轮转，不要提前终止。
                        if (
                            not _retryable_stream
                            and _is_public
                            and attempt < max_retries
                            and _stream_status_code == 404
                            and ("page not found" in _stream_error_lower or "not found" in _stream_error_lower)
                            and "/v1/messages" in str(url).lower()
                        ):
                            _retryable_stream = True
                            log.warning(
                                f"[GATEWAY] {backend_key}: endpoint /v1/messages not supported on current mirror, "
                                f"continue rotating public station URL",
                                tag="GATEWAY",
                            )
                    elif attempt < max_retries:
                        _retryable_stream = any(
                            kw in _stream_error_lower
                            for kw in ("connection", "timeout", "status=5", "backend error: 5", "html")
                        )

                    if _retryable_stream and _is_public:
                        _rotate_public_url = True
                        # AnyRouter + Cursor: invalid claude code request 更像请求画像问题，
                        # 切换镜像会引入额外变量（连接/404 噪声）。默认保持同一镜像做确定性重试。
                        if (
                            backend_key == "anyrouter"
                            and _anyrouter_client_type == "cursor"
                            and _is_anyrouter_invalid_request_error(_stream_status_code, last_error)
                            and _env_flag("ANYROUTER_CURSOR_NO_ROTATE_ON_INVALID", default=True)
                        ):
                            _rotate_public_url = False
                            log.info(
                                f"[GATEWAY] {backend_key}: keep current URL on invalid-claude-code-request "
                                f"for deterministic retries",
                                tag="GATEWAY",
                            )

                        if _rotate_public_url:
                            _psm.on_failure(backend_key)
                            _public_rotated_in_retry = True
                            _next_url = _build_effective_url_for_attempt()
                            log.warning(
                                f"[GATEWAY] {backend_key}: streaming failover rotate url -> {_next_url}",
                                tag="GATEWAY",
                            )

                    if _retryable_stream and attempt < max_retries:
                        continue
                    break
                else:
                    # 非流式请求
                    # [FIX 2026-02-01] 为非流式请求也设置较短的连接超时
                    connect_timeout = 5.0 if "127.0.0.1" in url or "localhost" in url else 15.0
                    non_stream_timeout = httpx.Timeout(
                        connect=connect_timeout,  # 连接超时：本地 5 秒，远程 15 秒
                        read=timeout,             # 读取超时
                        write=30.0,               # 写入超时
                        pool=30.0,                # [FIX 2026-02-02] 连接池超时从 10s 提高到 30s
                    )
                    if http_client is not None and _GATEWAY_USE_CURL_CFFI:
                        async with http_client.get_client(timeout=non_stream_timeout) as client:
                            if method.upper() == "POST":
                                response = await client.post(url, json=body, headers=request_headers)
                            elif method.upper() == "GET":
                                response = await client.get(url, headers=request_headers)
                            else:
                                return False, f"Unsupported method: {method}"

                            last_status_code = response.status_code

                            if response.status_code >= 400:
                                error_text = response.text
                                log.warning(f"Backend {backend_key} returned error {response.status_code}: {error_text[:200]}")

                                # [FIX 2026-02-03] 403 配额错误快速降级（不等待重试）
                                if response.status_code == 403:
                                    reason = _extract_403_reason(error_text)
                                    if _is_403_quota_error(response.status_code, error_text):
                                        log.warning(
                                            f"[403 QUOTA] Backend {backend_key} 返回 403 配额错误，"
                                            f"快速降级不重试。原因: {reason}"
                                        )
                                        # 记录后端健康状态
                                        health_mgr = get_backend_health_manager()
                                        await health_mgr.record_failure(backend_key, error_code=403)
                                        # 返回特殊错误码，触发 Gateway 快速降级
                                        return False, f"403_QUOTA: {reason}"
                                    elif _is_403_auth_error(response.status_code, error_text):
                                        log.warning(
                                            f"[403 AUTH] Backend {backend_key} 返回 403 认证错误，"
                                            f"需要验证凭证。原因: {reason}"
                                        )
                                        # 返回特殊错误码，触发凭证验证
                                        return False, f"403_AUTH: {reason}"
                                    else:
                                        log.warning(
                                            f"[403 UNKNOWN] Backend {backend_key} 返回 403 未知错误: {reason}"
                                        )
                                        # 未知 403 错误，快速降级
                                        return False, f"403: {reason}"

                                # ✅ [FIX 2026-02-12] 400 签名错误恢复（DRY: 委托给 _handle_signature_400_recovery）
                                sig_recovery = await _handle_signature_400_recovery(
                                    response.status_code, error_text, body,
                                    backend_key, attempt, max_retries,
                                )
                                if sig_recovery is not None:
                                    if sig_recovery["action"] == "retry":
                                        body = sig_recovery["body"]
                                        last_error = sig_recovery["last_error"]
                                        continue
                                    else:  # action == "fail"
                                        return False, sig_recovery["error"]

                                # [FIX 2026-01-21] Copilot 402 熔断：余额不足时开启熔断器
                                if backend_key == "copilot" and response.status_code == 402:
                                    # 检测 quota_exceeded 错误
                                    if "quota" in error_text.lower() or "no quota" in error_text.lower():
                                        open_copilot_circuit_breaker(f"402 余额不足: {error_text[:100]}")

                                # [FIX 2026-01-21] 记录后端健康状态
                                health_mgr = get_backend_health_manager()
                                await health_mgr.record_failure(backend_key, error_code=response.status_code)

                                # [NEW 2026-03-02] gcli2api 凭证不可用检测 → 冻结后端并快速降级
                                if _is_no_credential_error(backend_key, response.status_code, error_text):
                                    await health_mgr.freeze_backend(
                                        backend_key, duration=_CREDENTIAL_FREEZE_DURATION,
                                        reason=f"No available credentials: {error_text[:150]}"
                                    )
                                    _schedule_credential_probe(backend_key)
                                    log.warning(
                                        f"[CREDENTIAL GATE] 🔒 {backend_key} frozen: no available credentials. "
                                        f"Background probe started (interval={_CREDENTIAL_PROBE_INTERVAL}s)",
                                        tag="GATEWAY"
                                    )
                                    return False, f"NO_CREDENTIALS: {error_text[:200]}"

                                # [FIX 2026-01-23] 解析限流信息并标记限流状态
                                _mark_rate_limit_for_response(
                                    backend_key=backend_key,
                                    client_type=_anyrouter_client_type,
                                    status_code=response.status_code,
                                    account_id=account_id,
                                    model_account_id=model_account_id,
                                    backend_limit_id=backend_limit_id,
                                    headers=dict(response.headers),
                                    error_body=error_text,
                                    model_name=model_name,
                                )

                                # TODO(cursor): Cursor profile fallback in retry loop — untested.
                                (
                                    body,
                                    request_headers,
                                    _anyrouter_cursor_profile_stage,
                                    _profile_name,
                                    _profile_switched,
                                ) = _maybe_apply_anyrouter_cursor_profile_fallback(
                                    backend_key=backend_key,
                                    client_type=_anyrouter_client_type,
                                    use_openai_endpoint=_anyrouter_use_openai_endpoint,
                                    status_code=response.status_code,
                                    error_text=error_text,
                                    body=body,
                                    request_headers=request_headers,
                                    incoming_headers=headers,
                                    profile_stage=_anyrouter_cursor_profile_stage,
                                )
                                if _profile_switched and _profile_name and attempt < max_retries:
                                    last_error = f"AnyRouter cursor profile fallback -> {_profile_name}"
                                    continue

                                body, _fallback_model, _fallback_switched = _maybe_apply_anyrouter_model_fallback(
                                    backend_key=backend_key,
                                    status_code=response.status_code,
                                    error_text=error_text,
                                    body=body,
                                    tried_models=_anyrouter_tried_models,
                                )
                                if _fallback_switched and _fallback_model and attempt < max_retries:
                                    model_name = _fallback_model
                                    model_account_id = _build_model_account_id(account_id, model_name)
                                    last_error = f"AnyRouter model fallback -> {model_name}"
                                    continue

                                # 检查是否应该重试（传入 error_body 和 account_id）
                                if should_retry(
                                    response.status_code,
                                    attempt,
                                    max_retries,
                                    error_body=error_text,
                                    account_id=account_id,
                                    backend_limit_id=backend_limit_id,
                                ):
                                    # 计算智能延迟（考虑 Retry-After 和限流信息）
                                    delay = calculate_retry_delay(
                                        attempt - 1,
                                        status_code=response.status_code,
                                        retry_after_header=response.headers.get("Retry-After"),
                                        error_body=error_text,
                                        account_id=account_id,
                                        model=model_name,
                                        backend_limit_id=backend_limit_id,
                                    )
                                    log.warning(f"Retry {attempt}/{max_retries} for {backend_key} after {delay:.1f}s delay (status {response.status_code})")
                                    await asyncio.sleep(delay)
                                    last_error = f"Backend error: {response.status_code}"
                                    if _is_public:
                                        _psm.on_failure(backend_key)
                                        _public_rotated_in_retry = True
                                        log.warning(
                                            f"[GATEWAY] {backend_key}: retryable HTTP {response.status_code}, rotate public station endpoint",
                                            tag="GATEWAY",
                                        )
                                    continue

                                return False, f"Backend error: {response.status_code}"
                            
                            # [FIX 2026-01-24] 处理成功响应（status_code < 400）
                            # 检查 Content-Type，防止 HTML 响应被当作 JSON 处理
                            content_type = response.headers.get("content-type", "").lower()
                            if "text/html" in content_type:
                                error_text = response.text
                                log.error(
                                    f"[{backend_key}] ❌ 返回了 HTML 页面而不是 JSON！\n"
                                    f"Content-Type: {content_type}\n"
                                    f"响应前 500 字符: {error_text[:500]}"
                                )
                                
                                # 尝试从 HTML 中提取错误信息
                                error_hint = "Backend returned HTML instead of JSON"
                                if "api key" in error_text.lower():
                                    error_hint = "API Key invalid or expired"
                                elif "quota" in error_text.lower() or "balance" in error_text.lower():
                                    error_hint = "API quota exceeded or insufficient balance"
                                elif "unauthorized" in error_text.lower():
                                    error_hint = "Unauthorized - check API key"
                                
                                return False, f"{backend_key}: {error_hint}"
                            
                            # [FIX 2026-01-21] 记录后端健康状态
                            health_mgr = get_backend_health_manager()
                            await health_mgr.record_success(backend_key)

                            # [FIX 2026-01-23] 请求成功，标记成功并重置失败计数
                            tracker.mark_success(account_id)
                            if model_account_id:
                                tracker.mark_success(model_account_id)
                            
                            # 获取响应
                            try:
                                response_data = response.json()
                            except Exception as json_err:
                                log.error(f"[{backend_key}] ❌ JSON 解析失败: {json_err}\n响应前 500 字符: {response.text[:500]}")
                                return False, f"{backend_key}: Failed to parse JSON response"

                            # [FIX 2026-01-22] Kiro Gateway: 只有 /chat/completions 端点需要转换，/messages 端点直接透传
                            # 这里基于 original_endpoint 判断，避免前面重写端点导致条件失效
                            # [FIX 2026-03-10] Only Claude models need Anthropic→OpenAI response conversion;
                            # non-Claude models (DeepSeek/MiniMax/Qwen) use native OpenAI format via Kiro
                            if backend_key == "kiro-gateway" and original_endpoint == "/chat/completions":
                                _kiro_resp_model = body.get("model", "") if isinstance(body, dict) else ""
                                if _kiro_resp_model.lower().startswith("claude"):
                                    response_data = _convert_anthropic_to_openai_response(response_data)
                                    log.debug(f"Kiro Gateway: Converted response to OpenAI format", tag="GATEWAY")

                            # [REFACTOR 2026-02-14] Public station: unified response conversion
                            if (
                                _psm.needs_response_conversion(backend_key)
                                and original_endpoint == "/chat/completions"
                                and _public_response_conversion_required
                            ):
                                response_data = _convert_anthropic_to_openai_response(response_data)
                                log.debug(f"{backend_key}: Converted response to OpenAI format", tag="GATEWAY")

                            # [FIX 2026-02-26] Content-Level Validation: reject garbage 200 responses
                            if original_endpoint == "/chat/completions" and not stream:
                                if not _is_valid_chat_completion_response(response_data, backend_key):
                                    return False, f"{backend_key}: Invalid chat completion response structure"
                            return True, response_data
                    else:
                        # 没有 http_client，使用 httpx 直接请求
                        # [FIX 2026-02-01] 设置较短的连接超时
                        async with httpx.AsyncClient(timeout=non_stream_timeout) as client:
                            if method.upper() == "POST":
                                response = await client.post(url, json=body, headers=request_headers)
                            elif method.upper() == "GET":
                                response = await client.get(url, headers=request_headers)
                            else:
                                return False, f"Unsupported method: {method}"

                            last_status_code = response.status_code

                            if response.status_code >= 400:
                                error_text = response.text
                                
                                # [FIX 2026-01-24] 使用统一的 HTTP 错误日志系统记录详细错误信息（流式非流式共用）
                                log_http_error(
                                    status_code=response.status_code,
                                    backend_key=backend_key,
                                    response_body=error_text,
                                    response_headers=dict(response.headers),
                                    account_id=account_id,
                                    model_name=model_name,
                                    level="warning" if response.status_code < 500 else "error",
                                )

                                # ✅ [FIX 2026-02-12] 400 签名错误恢复（DRY: 委托给 _handle_signature_400_recovery）
                                sig_recovery = await _handle_signature_400_recovery(
                                    response.status_code, error_text, body,
                                    backend_key, attempt, max_retries,
                                )
                                if sig_recovery is not None:
                                    if sig_recovery["action"] == "retry":
                                        body = sig_recovery["body"]
                                        last_error = sig_recovery["last_error"]
                                        continue
                                    else:  # action == "fail"
                                        return False, sig_recovery["error"]

                                # [FIX 2026-01-21] Copilot 402 熔断：余额不足时开启熔断器
                                if backend_key == "copilot" and response.status_code == 402:
                                    # 检测 quota_exceeded 错误
                                    if "quota" in error_text.lower() or "no quota" in error_text.lower():
                                        open_copilot_circuit_breaker(f"402 余额不足: {error_text[:100]}")

                                # [FIX 2026-01-21] 记录后端健康状态
                                health_mgr = get_backend_health_manager()
                                await health_mgr.record_failure(backend_key, error_code=response.status_code)

                                # [NEW 2026-03-02] gcli2api 凭证不可用检测 → 冻结后端并快速降级
                                if _is_no_credential_error(backend_key, response.status_code, error_text):
                                    await health_mgr.freeze_backend(
                                        backend_key, duration=_CREDENTIAL_FREEZE_DURATION,
                                        reason=f"No available credentials: {error_text[:150]}"
                                    )
                                    _schedule_credential_probe(backend_key)
                                    log.warning(
                                        f"[CREDENTIAL GATE] 🔒 {backend_key} frozen: no available credentials. "
                                        f"Background probe started (interval={_CREDENTIAL_PROBE_INTERVAL}s)",
                                        tag="GATEWAY"
                                    )
                                    return False, f"NO_CREDENTIALS: {error_text[:200]}"

                                # [FIX 2026-01-23] 解析限流信息并标记限流状态
                                _mark_rate_limit_for_response(
                                    backend_key=backend_key,
                                    client_type=_anyrouter_client_type,
                                    status_code=response.status_code,
                                    account_id=account_id,
                                    model_account_id=model_account_id,
                                    backend_limit_id=backend_limit_id,
                                    headers=dict(response.headers),
                                    error_body=error_text,
                                    model_name=model_name,
                                )

                                # TODO(cursor): Cursor profile fallback in retry loop — untested.
                                (
                                    body,
                                    request_headers,
                                    _anyrouter_cursor_profile_stage,
                                    _profile_name,
                                    _profile_switched,
                                ) = _maybe_apply_anyrouter_cursor_profile_fallback(
                                    backend_key=backend_key,
                                    client_type=_anyrouter_client_type,
                                    use_openai_endpoint=_anyrouter_use_openai_endpoint,
                                    status_code=response.status_code,
                                    error_text=error_text,
                                    body=body,
                                    request_headers=request_headers,
                                    incoming_headers=headers,
                                    profile_stage=_anyrouter_cursor_profile_stage,
                                )
                                if _profile_switched and _profile_name and attempt < max_retries:
                                    last_error = f"AnyRouter cursor profile fallback -> {_profile_name}"
                                    continue

                                body, _fallback_model, _fallback_switched = _maybe_apply_anyrouter_model_fallback(
                                    backend_key=backend_key,
                                    status_code=response.status_code,
                                    error_text=error_text,
                                    body=body,
                                    tried_models=_anyrouter_tried_models,
                                )
                                if _fallback_switched and _fallback_model and attempt < max_retries:
                                    model_name = _fallback_model
                                    model_account_id = _build_model_account_id(account_id, model_name)
                                    last_error = f"AnyRouter model fallback -> {model_name}"
                                    continue

                                # 检查是否应该重试
                                if should_retry(
                                    response.status_code,
                                    attempt,
                                    max_retries,
                                    error_body=error_text,
                                    account_id=account_id,
                                    backend_limit_id=backend_limit_id,
                                ):
                                    # 计算智能延迟
                                    delay = calculate_retry_delay(
                                        attempt - 1,
                                        status_code=response.status_code,
                                        retry_after_header=response.headers.get("Retry-After"),
                                        error_body=error_text,
                                        account_id=account_id,
                                        model=model_name,
                                        backend_limit_id=backend_limit_id,
                                    )
                                    log.warning(f"Retry {attempt}/{max_retries} for {backend_key} after {delay:.1f}s delay (status {response.status_code})")
                                    await asyncio.sleep(delay)
                                    last_error = f"Backend error: {response.status_code}"
                                    if _is_public:
                                        _psm.on_failure(backend_key)
                                        _public_rotated_in_retry = True
                                        log.warning(
                                            f"[GATEWAY] {backend_key}: retryable HTTP {response.status_code}, rotate public station endpoint",
                                            tag="GATEWAY",
                                        )
                                    continue

                                return False, f"Backend error: {response.status_code}"

                            # [FIX 2026-01-24] 处理成功响应（status_code < 400）
                            # 检查 Content-Type，防止 HTML 响应被当作 JSON 处理
                            content_type = response.headers.get("content-type", "").lower()
                            if "text/html" in content_type:
                                error_text = response.text
                                log.error(
                                    f"[{backend_key}] ❌ 返回了 HTML 页面而不是 JSON！\n"
                                    f"Content-Type: {content_type}\n"
                                    f"响应前 500 字符: {error_text[:500]}"
                                )
                                
                                # 尝试从 HTML 中提取错误信息
                                error_hint = "Backend returned HTML instead of JSON"
                                if "api key" in error_text.lower():
                                    error_hint = "API Key invalid or expired"
                                elif "quota" in error_text.lower() or "balance" in error_text.lower():
                                    error_hint = "API quota exceeded or insufficient balance"
                                elif "unauthorized" in error_text.lower():
                                    error_hint = "Unauthorized - check API key"
                                
                                return False, f"{backend_key}: {error_hint}"

                            # [FIX 2026-01-21] 记录后端健康状态
                            health_mgr = get_backend_health_manager()
                            await health_mgr.record_success(backend_key)

                            # [FIX 2026-01-23] 请求成功，标记成功并重置失败计数
                            tracker.mark_success(account_id)
                            if model_account_id:
                                tracker.mark_success(model_account_id)
                            
                            # 获取响应
                            try:
                                response_data = response.json()
                            except Exception as json_err:
                                log.error(f"[{backend_key}] ❌ JSON 解析失败: {json_err}\n响应前 500 字符: {response.text[:500]}")
                                return False, f"{backend_key}: Failed to parse JSON response"

                            # [FIX 2026-01-22] Kiro Gateway: 只有 /chat/completions 端点需要转换，/messages 端点直接透传
                            # 这里基于 original_endpoint 判断，避免前面重写端点导致条件失效
                            # [FIX 2026-03-10] Only Claude models need Anthropic→OpenAI response conversion;
                            # non-Claude models (DeepSeek/MiniMax/Qwen) use native OpenAI format via Kiro
                            if backend_key == "kiro-gateway" and original_endpoint == "/chat/completions":
                                _kiro_resp_model = body.get("model", "") if isinstance(body, dict) else ""
                                if _kiro_resp_model.lower().startswith("claude"):
                                    response_data = _convert_anthropic_to_openai_response(response_data)
                                    log.debug(f"Kiro Gateway: Converted response to OpenAI format", tag="GATEWAY")

                            # [REFACTOR 2026-02-14] Public station: unified response conversion
                            if (
                                _psm.needs_response_conversion(backend_key)
                                and original_endpoint == "/chat/completions"
                                and _public_response_conversion_required
                            ):
                                response_data = _convert_anthropic_to_openai_response(response_data)
                                log.debug(f"{backend_key}: Converted response to OpenAI format", tag="GATEWAY")

                            # [FIX 2026-02-26] Content-Level Validation: reject garbage 200 responses
                            if original_endpoint == "/chat/completions" and not stream:
                                if not _is_valid_chat_completion_response(response_data, backend_key):
                                    return False, f"{backend_key}: Invalid chat completion response structure"
                            return True, response_data
                    # 注意：并发许可在 BackendPermit 的 __aexit__ 中自动释放

        except PermitAcquireTimeout:
            # [FIX 2026-02-01] 并发许可获取超时，后端可能过载，快速失败不重试
            log.warning(f"Backend {backend_key} permit acquire timeout, skipping")
            last_error = "Backend overloaded (permit timeout)"
            break  # 不重试，让 Gateway 尝试下一个后端
        except httpx.TimeoutException:
            log.warning(f"Backend {backend_key} timeout (attempt {attempt + 1}/{max_retries + 1})")
            last_error = "Request timeout"
            if attempt < max_retries:
                if _is_public:
                    _psm.on_failure(backend_key)
                    _public_rotated_in_retry = True
                    log.warning(
                        f"[GATEWAY] {backend_key}: timeout retry, rotate public station endpoint",
                        tag="GATEWAY",
                    )
                continue
        except httpx.ConnectError as e:
            error_msg = str(e)
            log.warning(
                f"Backend {backend_key} connection failed (attempt {attempt + 1}/{max_retries + 1}, url={url}): {error_msg[:200]}"
            )
            last_error = f"Connection failed (url={url})"

            # [FIX 2026-02-03] 检测连接拒绝错误，自动冻结后端
            health_mgr = get_backend_health_manager()
            if health_mgr.is_connection_refused_error(error_msg):
                # 连接被拒绝 = 后端服务未运行，冻结 5 分钟
                await health_mgr.freeze_backend(
                    backend_key,
                    duration=300,  # 5 分钟
                    reason=f"Connection refused: {error_msg[:150]}"
                )
                # 不再重试，直接跳出让 Gateway 尝试下一个后端
                break

            if attempt < max_retries:
                if _is_public:
                    _psm.on_failure(backend_key)
                    _public_rotated_in_retry = True
                    log.warning(
                        f"[GATEWAY] {backend_key}: connection retry, rotate public station endpoint",
                        tag="GATEWAY",
                    )
                continue
        except Exception as e:
            log.error(f"Backend {backend_key} request failed: {e}")
            last_error = str(e)
            # 对于未知错误，不重试
            break

    # 所有重试都失败
    log.error(f"Backend {backend_key} failed after {max_retries + 1} attempts. Last error: {last_error}")
    
    # [FIX 2026-01-21] 记录后端健康状态（最终失败）
    health_mgr = get_backend_health_manager()
    await health_mgr.record_failure(backend_key, error_code=last_status_code or 0)

    # [NEW 2026-02-25] HTTP circuit breaker — auto-freeze on consecutive failures
    # Classifies the error (auth/server/timeout/connection) and checks if consecutive
    # failures have reached the per-backend threshold. If so, freezes the backend
    # with a category-specific duration (auth=10min, server=2min, timeout=1min, conn=5min).
    try:
        error_category = health_mgr.classify_http_error(last_status_code or 0, last_error or "")
        cb_threshold = BACKENDS.get(backend_key, {}).get("circuit_breaker_threshold", 5)
        await health_mgr.record_http_failure(
            backend_key,
            error_category=error_category,
            threshold=cb_threshold,
            status_code=last_status_code or 0,
        )
    except Exception as _cb_err:
        log.debug(f"[CIRCUIT BREAKER] Error in circuit breaker check: {_cb_err}", tag="GATEWAY")

    # [REFACTOR 2026-02-14] Public station: unified failure handling (URL rotation etc.)
    if _psm.is_public_station(backend_key):
        if _public_rotated_in_retry:
            log.info(
                f"{backend_key}: Public station rotation already applied during retries, skip final extra rotation",
                tag="GATEWAY",
            )
        else:
            _psm.on_failure(backend_key)
            log.info(f"{backend_key}: Public station failure handled by PublicStationManager", tag="GATEWAY")
    
    return False, last_error or "Unknown error"


async def proxy_streaming_request_with_timeout(
    url: str,
    method: str,
    headers: Dict[str, str],
    body: Any,
    timeout: float,
    backend_key: str = "unknown",
    client_type: Optional[str] = None,
    endpoint: str = "",
    public_response_conversion_required: bool = False,
    skip_rate_limit_check: bool = False,
) -> Tuple[bool, Any]:
    """
    处理流式代理请求（带超时和错误处理）

    Args:
        url: 请求URL
        method: HTTP方法
        headers: 请求头
        body: 请求体
        timeout: 超时时间（秒）
        backend_key: 后端标识（用于日志）
        skip_rate_limit_check: 是否跳过限流前置检查（用于同请求内重试）

    Returns:
        Tuple[bool, Any]: (成功标志, 流生成器或错误信息)
    """
    try:
        # [FIX 2026-01-23] 生成 account_id 和 model_name（用于限流跟踪）
        account_id = _build_account_id(backend_key, headers, body)
        model_name = body.get("model") if isinstance(body, dict) else None
        model_account_id = _build_model_account_id(account_id, model_name)
        backend_limit_id = _build_backend_limit_id(backend_key)
        from .rate_limit_handler import get_rate_limit_tracker
        tracker = get_rate_limit_tracker()
        
        if not skip_rate_limit_check:
            # [FIX 2026-01-23] 请求前检查限流状态
            # 检查后端级别限流（用于 5xx 软避让）
            if tracker.is_rate_limited(backend_limit_id):
                remaining = tracker.get_reset_seconds(backend_limit_id)
                if remaining:
                    log.warning(f"[RATE_LIMIT] 后端 {backend_key} 软避让中，剩余 {remaining:.1f} 秒，跳过流式请求")
                    return False, f"Backend {backend_key} cooling down (status=503), retry after {remaining:.1f}s"

            # 检查账号级别限流
            if tracker.is_rate_limited(account_id):
                remaining = tracker.get_reset_seconds(account_id)
                if remaining:
                    log.warning(f"[RATE_LIMIT] 账号 {account_id} 仍在限流中，剩余 {remaining:.1f} 秒，跳过流式请求")
                    return False, f"Account rate limited (status=429), retry after {remaining:.1f}s"

            # 检查模型级别限流（如果有模型名）
            if model_account_id and tracker.is_rate_limited(model_account_id):
                remaining = tracker.get_reset_seconds(model_account_id)
                if remaining:
                    log.warning(f"[RATE_LIMIT] 模型 {model_name} 仍在限流中，剩余 {remaining:.1f} 秒，跳过流式请求")
                    return False, f"Model {model_name} rate limited (status=429), retry after {remaining:.1f}s"
        
        # [FIX 2026-01-23] 使用 RateLimiter 限制请求频率（主动削峰）
        # [FIX 2026-02-02] 间隔从 500ms 降低到 150ms，减少 Claude Code 高频调用延迟
        from akarins_gateway.core.rate_limiter import get_keyed_rate_limiter
        rate_limiter = get_keyed_rate_limiter(min_interval_ms=150)  # 最小间隔 150ms
        await rate_limiter.wait(_build_rate_limit_key(account_id, model_name))  # 等待直到可以进行下一次调用
        
        # 创建带超时的客户端
        # [FIX 2026-02-01] 减少连接超时时间，快速失败
        # 原因：如果后端服务没运行，30 秒连接超时会导致整个请求链路非常慢
        # 本地服务（Kiro Gateway、Copilot）应该在 5 秒内响应，否则视为不可用
        connect_timeout = 5.0 if "127.0.0.1" in url or "localhost" in url else 15.0
        timeout_config = httpx.Timeout(
            connect=connect_timeout,  # 连接超时：本地 5 秒，远程 15 秒
            read=timeout,             # 读取超时（流式数据）
            write=30.0,               # 写入超时
            pool=30.0,                # [FIX 2026-02-02] 连接池超时从 10s 提高到 30s
        )
        client = httpx.AsyncClient(timeout=timeout_config)

        # [FIX 2026-02-12] Antigravity-Tools thinking 存活补丁
        # Antigravity-Manager 有两个自动禁用 thinking 的检查:
        #   1) should_disable_thinking_due_to_history(): 最后一条 assistant 消息有 tool_use 但没 thinking → 禁用
        #   2) has_valid_signature_for_function_calls(): 有 tool_use 但没有 ≥50 字符 signature → 禁用
        # Claude Code 的历史消息几乎总是有 tool_use 但没 thinking，导致 thinking 被永久禁用。
        # 解决方案: 在转发前给 assistant 消息注入最小 thinking 块，骗过这两个检查。
        # 注入的 thinking 块会在 Antigravity-Manager 的 handlers/claude.rs 中被转为纯 text，
        # 不影响实际的 Gemini API 调用。
        if backend_key == "antigravity-tools" and isinstance(body, dict):
            body = _inject_thinking_blocks_for_antigravity_tools(body)

        stream_ctx = client.stream(method, url, json=body, headers=headers)

        try:
            response = await stream_ctx.__aenter__()
        except httpx.ReadTimeout:
            await safe_close_client(client)
            return False, "Request timeout (status=504)"
        except httpx.ConnectTimeout:
            await safe_close_client(client)
            return False, "Connection timeout (status=504)"
        except httpx.ConnectError as e:
            error_msg = str(e)
            await safe_close_client(client)
            log.warning(
                f"[GATEWAY] {backend_key}: stream connect error url={url} detail={error_msg[:300]}",
                tag="GATEWAY",
            )

            # [FIX 2026-02-03] 检测连接拒绝错误，自动冻结后端
            health_mgr = get_backend_health_manager()
            if health_mgr.is_connection_refused_error(error_msg):
                await health_mgr.freeze_backend(
                    backend_key,
                    duration=300,
                    reason=f"Connection refused (streaming): {error_msg[:150]}"
                )

            return False, f"Connection failed (status=502, url={url})"
        except Exception as e:
            await safe_close_client(client)
            return False, str(e)

        # [FIX 2026-01-24] 检查响应状态码（失败时直接触发降级链）
        if response.status_code >= 400:
            error_text = await response.aread()
            error_body_str = error_text.decode("utf-8", errors="ignore") if isinstance(error_text, bytes) else str(error_text)

            # [FIX 2026-01-24] 使用统一的 HTTP 错误日志系统记录流式请求错误
            log_http_error(
                status_code=response.status_code,
                backend_key=backend_key,
                response_body=error_body_str,
                response_headers=dict(response.headers),
                account_id=account_id,
                model_name=model_name,
                level="warning" if response.status_code < 500 else "error",
            )

            # [FIX 2026-01-23] 解析限流信息并标记限流状态
            _mark_rate_limit_for_response(
                backend_key=backend_key,
                client_type=client_type,
                status_code=response.status_code,
                account_id=account_id,
                model_account_id=model_account_id,
                backend_limit_id=backend_limit_id,
                headers=dict(response.headers),
                error_body=error_body_str,
                model_name=model_name,
            )

            # [NEW 2026-03-02] gcli2api 凭证不可用检测 → 冻结后端并快速降级（流式路径）
            if _is_no_credential_error(backend_key, response.status_code, error_body_str):
                health_mgr = get_backend_health_manager()
                await health_mgr.freeze_backend(
                    backend_key, duration=_CREDENTIAL_FREEZE_DURATION,
                    reason=f"No available credentials (streaming): {error_body_str[:150]}"
                )
                _schedule_credential_probe(backend_key)
                log.warning(
                    f"[CREDENTIAL GATE] 🔒 {backend_key} frozen: no available credentials. "
                    f"Background probe started (interval={_CREDENTIAL_PROBE_INTERVAL}s)",
                    tag="GATEWAY"
                )
                await stream_ctx.__aexit__(None, None, None)
                await safe_close_client(client)
                return False, f"NO_CREDENTIALS: {error_body_str[:200]}"

            await stream_ctx.__aexit__(None, None, None)
            await safe_close_client(client)
            _error_brief = " ".join(error_body_str.split())[:240]
            if _error_brief:
                return False, f"Backend error: {response.status_code} | {_error_brief}"
            return False, f"Backend error: {response.status_code}"

        # [FIX 2026-01-24] 检查 Content-Type，防止 HTML 响应被当作 SSE 流处理
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type or "<!doctype html" in content_type:
            error_text = await response.aread()
            error_body = error_text.decode("utf-8", errors="ignore") if isinstance(error_text, bytes) else str(error_text)

            # 记录前 500 字符用于调试
            log.error(
                f"[{backend_key}] ❌ 返回了 HTML 页面而不是 SSE 流！\n"
                f"Content-Type: {content_type}\n"
                f"响应前 500 字符: {error_body[:500]}"
            )

            # 尝试从 HTML 中提取错误信息
            error_hint = "Backend returned HTML instead of SSE stream"
            if "api key" in error_body.lower():
                error_hint = "API Key invalid or expired"
            elif "quota" in error_body.lower() or "balance" in error_body.lower():
                error_hint = "API quota exceeded or insufficient balance"
            elif "unauthorized" in error_body.lower():
                error_hint = "Unauthorized - check API key"

            await stream_ctx.__aexit__(None, None, None)
            await safe_close_client(client)
            return False, f"{backend_key}: {error_hint} (status={response.status_code})"

        async def stream_generator():
            # 注意：chunk_timeout 检查已移除
            # 原因：之前的逻辑是在收到 chunk 后才检查时间差，这是错误的。
            # 当模型需要长时间思考（如 Claude 写长文档）时，两个 chunk 之间可能超过 120 秒，
            # 但只要最终收到了数据，就不应该超时。
            # httpx 的 read=timeout 配置已经处理了真正的读取超时。

            yielded_any = False
            saw_done = False

            try:
                # [FIX 2026-01-23] 流式请求成功开始，标记成功（但不清除限流记录，因为流还在进行中）
                # 注意：流式请求的成功标记应该在流完成后进行，这里先不处理
                if hasattr(log, 'success'):
                    log.success(f"Streaming started", tag=backend_key.upper())

                # [FIX 2026-01-22] Kiro Gateway: 只有 /chat/completions 端点需要转换，/messages 端点直接透传
                # [FIX 2026-03-10] Only Claude models need Anthropic SSE→OpenAI SSE conversion;
                # non-Claude models (DeepSeek/MiniMax/Qwen) use native OpenAI SSE format via Kiro
                _kiro_stream_model = body.get("model", "") if isinstance(body, dict) else ""
                if backend_key == "kiro-gateway" and endpoint == "/chat/completions" and _kiro_stream_model.lower().startswith("claude"):
                    # Claude model: Anthropic SSE → OpenAI SSE conversion
                    async for converted_chunk in _convert_anthropic_stream_to_openai(response.aiter_bytes()):
                        if converted_chunk:
                            yielded_any = True
                            if "[DONE]" in converted_chunk:
                                saw_done = True
                            yield converted_chunk
                # [REFACTOR 2026-02-14] Public station: unified stream conversion
                elif (
                    _get_psm().needs_response_conversion(backend_key)
                    and endpoint == "/chat/completions"
                    and public_response_conversion_required
                ):
                    # Anthropic SSE → OpenAI SSE conversion for public stations
                    async for converted_chunk in _convert_anthropic_stream_to_openai(response.aiter_bytes()):
                        if converted_chunk:
                            yielded_any = True
                            if "[DONE]" in converted_chunk:
                                saw_done = True
                            yield converted_chunk
                # [FIX 2026-02-12] Antigravity-Tools: 拦截内联 <thinking> 标签并转换为结构化 thinking 块
                # 如果上游已返回原生 thinking 块则自动透传
                elif backend_key == "antigravity-tools":
                    log.info(
                        f"[GATEWAY] Antigravity-Tools thinking interceptor ACTIVATED "
                        f"for model={body.get('model', '?') if isinstance(body, dict) else '?'}, "
                        f"endpoint={endpoint}"
                    )
                    try:
                        async for converted_chunk in intercept_thinking_in_anthropic_sse(
                            response.aiter_bytes(),
                        ):
                            if converted_chunk:
                                yielded_any = True
                                if "[DONE]" in converted_chunk:
                                    saw_done = True
                                yield converted_chunk
                    except Exception as intercept_err:
                        # Interception failed – fall back to raw pass-through
                        log.warning(
                            f"[GATEWAY] Thinking interceptor failed, "
                            f"falling back to pass-through: {intercept_err}",
                        )
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                yielded_any = True
                                if b"[DONE]" in chunk:
                                    saw_done = True
                                yield chunk.decode("utf-8", errors="ignore")
                else:
                    # Other backends: Anthropic format direct pass-through
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            yielded_any = True
                            if b"[DONE]" in chunk:
                                saw_done = True
                            yield chunk.decode("utf-8", errors="ignore")

                # [FIX 2026-01-23] 流式请求完成，标记成功并重置失败计数
                tracker.mark_success(account_id)
                if model_account_id:
                    tracker.mark_success(model_account_id)

                if hasattr(log, 'success'):
                    log.success(f"Streaming completed", tag=backend_key.upper())

            except httpx.ReadTimeout:
                log.warning(f"Read timeout from {backend_key} after {timeout}s")
                error_msg = json.dumps({
                    'error': {
                        'type': 'network',
                        'reason': 'timeout',
                        'message': 'Request timed out',
                        'retryable': True
                    }
                })
                yield f"data: {error_msg}\n\n"
            except httpx.ConnectTimeout:
                log.warning(f"Connect timeout to {backend_key}")
                error_msg = json.dumps({
                    'error': {
                        'type': 'network',
                        'reason': 'timeout',
                        'message': 'Request timed out',
                        'retryable': True
                    }
                })
                yield f"data: {error_msg}\n\n"
            except httpx.RemoteProtocolError as e:
                # Some upstreams (notably enterprise proxies) may close a chunked response
                # without a proper terminating chunk, even though the client has already
                # received the semantic end marker (e.g. SSE "[DONE]").
                #
                # If we already forwarded any bytes (or saw "[DONE]"), treat this as a
                # benign end-of-stream to avoid breaking Bugment parsers and spamming logs.
                if "incomplete chunked read" in str(e).lower():
                    if saw_done or yielded_any:
                        log.warning(
                            f"Ignoring benign upstream RemoteProtocolError after completion: {e}",
                            tag=backend_key.upper(),
                        )
                        return
                log.error(f"Streaming protocol error from {backend_key}: {e}")
                error_msg = json.dumps({'error': str(e)})
                yield f"data: {error_msg}\n\n"
            except asyncio.CancelledError:
                # Downstream client disconnected/cancelled (common for prompt enhancer or UI refresh).
                # Stop consuming the upstream stream quietly.
                return
            except Exception as e:
                log.error(f"Streaming error from {backend_key}: {e}")
                error_msg = json.dumps({'error': str(e)})
                yield f"data: {error_msg}\n\n"
            finally:
                try:
                    await stream_ctx.__aexit__(None, None, None)
                finally:
                    try:
                        await safe_close_client(client)
                    except Exception:
                        # Avoid noisy event-loop "connection_lost" traces on Windows Proactor when the
                        # peer has already reset the connection.
                        pass

        return True, stream_generator()

    except Exception as e:
        log.error(f"Failed to start streaming from {backend_key}: {e}")
        return False, str(e)


async def proxy_streaming_request(
    url: str,
    method: str,
    headers: Dict[str, str],
    body: Any,
    timeout: float,
) -> Tuple[bool, Any]:
    """处理流式代理请求（兼容旧接口）"""
    try:
        client = httpx.AsyncClient(timeout=None)

        async def stream_generator():
            try:
                async with client.stream(method, url, json=body, headers=headers) as response:
                    if response.status_code >= 400:
                        error_text = await response.aread()
                        error_body_str = error_text.decode("utf-8", errors="ignore") if isinstance(error_text, bytes) else str(error_text)
                        
                        # [FIX 2026-01-24] 使用统一的 HTTP 错误日志系统
                        log_http_error(
                            status_code=response.status_code,
                            backend_key="<streaming>",  # 这个函数没有 backend_key 参数，使用占位符
                            response_body=error_body_str,
                            response_headers=dict(response.headers),
                            level="warning" if response.status_code < 500 else "error",
                        )
                        
                        error_msg = json.dumps({'error': 'Backend error', 'status': response.status_code})
                        yield f"data: {error_msg}\n\n"
                        return

                    async for chunk in response.aiter_bytes():
                        if chunk:
                            yield chunk.decode("utf-8", errors="ignore")
            except httpx.RemoteProtocolError as e:
                # See proxy_streaming_request_with_timeout() for rationale.
                if "incomplete chunked read" in str(e).lower():
                    return
                error_msg = json.dumps({'error': str(e)})
                yield f"data: {error_msg}\n\n"
            except asyncio.CancelledError:
                return
            finally:
                try:
                    await safe_close_client(client)
                except Exception:
                    pass

        return True, stream_generator()

    except Exception as e:
        log.error(f"Streaming request failed: {e}")
        return False, str(e)


def _is_cross_model_entry(requested_model: str, target_model: str) -> bool:
    """
    判断 target_model 相对于 requested_model 是否是跨模型降级

    IDE 客户端禁止跨模型降级，只允许跨后端寻找同名同型号模型。
    跨模型包括：1) 不同池 (Claude <-> Gemini)  2) 同池不同型号 (claude-sonnet-4 vs claude-sonnet-4.5)

    复用 config_loader.normalize_model_for_comparison 的归一化逻辑，与路由配置一致。
    """
    if not requested_model or not target_model:
        return False
    try:
        from akarins_gateway.gateway.config_loader import normalize_model_for_comparison
        req_norm = normalize_model_for_comparison(requested_model)
        tgt_norm = normalize_model_for_comparison(target_model)
        if req_norm == tgt_norm:
            return False  # 同名同型号，非跨模型
    except ImportError:
        # 回退：简单比较
        req_lower = requested_model.lower().replace("4-5", "4.5")
        tgt_lower = target_model.lower().replace("4-5", "4.5")
        if req_lower == tgt_lower:
            return False
    try:
        # STUB: fallback_manager not extracted to akarins-gateway
        try:
            from akarins_gateway.fallback_manager import get_model_pool
        except ImportError:
            get_model_pool = None
        req_pool = get_model_pool(requested_model)
        tgt_pool = get_model_pool(target_model)
        if req_pool != tgt_pool:
            return True  # 跨池 = 跨模型
    except ImportError:
        pass
    # 同池但型号不同 = 跨模型（如 claude-sonnet-4.5 -> claude-sonnet-4）
    return True


# ==================== [NEW 2026-02-25] Phase 3: 启动时后端健康探测 ====================

# 启动探测冻结时长（秒）
STARTUP_PROBE_FREEZE_DURATION = 300
# [FIX 2026-02-26] 连接失败重试配置：慢启动后端（如 zerogravity）可能尚未就绪
STARTUP_PROBE_MAX_RETRIES = 3
STARTUP_PROBE_RETRY_DELAY = 2.0  # 秒


def _is_local_address(host: str) -> bool:
    """检查是否为本地地址（用于自引用检测）"""
    import socket as _socket
    if host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        return True
    if host.startswith("127."):  # 127.0.0.0/8 回环网段
        return True
    try:
        if host.lower() == _socket.gethostname().lower():
            return True
    except Exception:
        pass
    return False


# ==================== [NEW 2026-02-26] 凭证驱动冻结/解冻回调 ====================
async def _on_antigravity_credential_change(event: "CredentialChangeEvent") -> None:
    """
    CredentialManager 凭证变更回调：驱动 antigravity 冻结/解冻。
    
    当 antigravity 凭证被启用/添加时解冻后端；
    当凭证被禁用/删除且无剩余可用凭证时冻结后端。
    """
    if not event.is_antigravity:
        return

    health_mgr = get_backend_health_manager()
    backend_key = "gcli2api-antigravity"

    if event.change_type in ("enabled", "added"):
        # 凭证恢复可用 → 无条件解冻（unfreeze_backend 对未冻结后端是安全的 no-op）
        await health_mgr.unfreeze_backend(backend_key)
        log.info(
            f"[CREDENTIAL GATE] Unfreezing antigravity: "
            f"credential '{event.credential_name}' {event.change_type}",
            tag="GATEWAY"
        )

    elif event.change_type in ("disabled", "removed"):
        # 凭证丢失 → 检查是否还有剩余可用凭证
        try:
            # STUB: antigravity_anthropic_router (ENABLE_ANTIGRAVITY feature flag)
            try:
                from akarins_gateway.gateway.backends.antigravity.router import get_credential_manager as _get_ag_cm
            except ImportError:
                _get_ag_cm = None
            _ag_cm = await _get_ag_cm()
            if not await _ag_cm.has_usable_antigravity_credentials():
                await health_mgr.freeze_backend(
                    backend_key, duration=3600,  # 1小时安全兜底（事件驱动解冻，非时间驱动）
                    reason=f"No usable antigravity credentials "
                           f"(last: {event.credential_name} {event.change_type})"
                )
                log.warning(
                    f"[CREDENTIAL GATE] Freezing antigravity: "
                    f"no usable credentials "
                    f"(last: {event.credential_name} {event.change_type})",
                    tag="GATEWAY"
                )
        except Exception as e:
            log.debug(f"[CREDENTIAL GATE] Check failed (fail-open): {e}", tag="GATEWAY")
# ==================== 凭证驱动冻结/解冻回调结束 ====================


async def probe_backends_on_startup() -> Dict[str, bool]:
    """
    Gateway 启动时对所有启用的后端做一次 HTTP-level 健康探测。
    不可达的后端会被预冻结（5min），避免首次请求浪费连接尝试。

    [FIX 2026-02-25] 从 TCP-only 升级为 HTTP HEAD 探测：
    - TCP 端口探测只验证端口是否开放，无法区分预期服务和其他进程
    - HTTP HEAD 验证实际 HTTP 服务正在响应

    [FIX 2026-02-26 v2] 探测策略优化：
    - 连接失败重试：慢启动后端（如 zerogravity）有 3 次重试机会，每次间隔 2 秒
    - HTTP 状态码分类：404/5xx 触发冻结（端点不存在或服务异常），
      2xx/401/403/405/429 等才算可达（服务在运行）
    - 自引用检测：跳过 base_url 指向网关自身端口的后端
    - Windows 异常兼容：覆盖 Windows 特有的 socket 异常类型

    Returns:
        Dict[backend_key, reachable: bool]
    """
    from urllib.parse import urlparse
    from akarins_gateway.core.config import get_server_port, get_server_host

    health_mgr = get_backend_health_manager()
    results: Dict[str, bool] = {}

    # [FIX 2026-02-26] 获取网关自身端口，用于自引用检测
    try:
        gateway_port = get_server_port()
        gateway_host = get_server_host()
    except Exception as e:
        log.warning(
            f"[STARTUP] 无法读取网关端口/主机配置，使用默认值: {e}",
            tag="GATEWAY"
        )
        gateway_port = 7861
        gateway_host = "0.0.0.0"

    # Create a shared httpx client for all probes (runs once at startup)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(5.0),
        verify=False,  # Don't fail on self-signed certs for local backends
        follow_redirects=True,
    ) as probe_client:
        for backend_key, config in BACKENDS.items():
            if not config.get("enabled", True):
                log.debug(f"[STARTUP] {backend_key}: 已禁用，跳过探测", tag="GATEWAY")
                continue

            base_url = config.get("base_url", "")
            if not base_url:
                log.debug(f"[STARTUP] Skipping backend with no base_url: {backend_key}")
                results[backend_key] = False
                continue

            # Parse host:port for logging
            try:
                parsed = urlparse(base_url)
                host = parsed.hostname or "127.0.0.1"
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
            except Exception as e:
                log.debug(f"[STARTUP] Cannot parse URL for {backend_key}: {e}")
                results[backend_key] = True  # Fail open
                continue

            # [FIX 2026-02-26] 自引用检测：跳过指向网关自身端口的后端
            # lifespan 阶段 HTTP 服务尚未就绪，探测自己必然失败
            if _is_local_address(host) and port == gateway_port:
                # [NEW 2026-02-26] antigravity 是自托管服务，用凭证可用性代替 HTTP 探测
                if backend_key == "gcli2api-antigravity":
                    try:
                        # STUB: antigravity_anthropic_router (ENABLE_ANTIGRAVITY feature flag)
                        try:
                            from akarins_gateway.gateway.backends.antigravity.router import get_credential_manager as _get_ag_cm
                        except ImportError:
                            _get_ag_cm = None
                        _ag_cm = await _get_ag_cm()
                        has_creds = await _ag_cm.has_usable_antigravity_credentials()
                        if not has_creds:
                            await health_mgr.freeze_backend(
                                backend_key, duration=3600,
                                reason="Startup: no usable antigravity credentials"
                            )
                            log.warning(
                                f"[STARTUP] Pre-freezing antigravity: no usable credentials",
                                tag="GATEWAY"
                            )
                            results[backend_key] = False
                            continue
                        else:
                            log.info(
                                f"[STARTUP] antigravity has usable credentials",
                                tag="GATEWAY"
                            )
                            results[backend_key] = True
                            continue
                    except Exception as e:
                        log.debug(
                            f"[STARTUP] Cannot check antigravity credentials (fail-open): {e}",
                            tag="GATEWAY"
                        )
                # 其他自引用后端 fail-open
                log.info(
                    f"[STARTUP] Skipping self-referential backend: {backend_key} "
                    f"(localhost:{port} is gateway's own port)"
                )
                results[backend_key] = True  # Fail open — it's ourselves
                continue

            # HTTP-level probe with retry for connection failures
            # [FIX 2026-02-26 v2] 连接失败重试 + HTTP 状态码分类
            # - 连接级失败（refused/timeout）: 重试最多 STARTUP_PROBE_MAX_RETRIES 次
            #   帮助慢启动后端（如 zerogravity）有足够时间就绪
            # - HTTP 响应状态码分类:
            #   * 2xx/401/403/405/429: 服务在运行 → reachable
            #   * 404: 端点不存在 → freeze（API 未正确部署）
            #   * 5xx: 服务器错误 → freeze（服务异常）
            probe_url = base_url.rstrip("/")
            last_exc = None
            response = None

            for attempt in range(1, STARTUP_PROBE_MAX_RETRIES + 1):
                try:
                    response = await probe_client.head(probe_url)
                    last_exc = None
                    break  # 收到 HTTP 响应，无论状态码都退出重试循环
                except (
                    httpx.ConnectError,
                    httpx.ConnectTimeout,
                    httpx.ReadTimeout,
                    OSError,
                ) as e:
                    last_exc = e
                    if attempt < STARTUP_PROBE_MAX_RETRIES:
                        log.info(
                            f"[STARTUP] ⏳ {backend_key} 连接失败 ({host}:{port}), "
                            f"重试 {attempt}/{STARTUP_PROBE_MAX_RETRIES}... "
                            f"({type(e).__name__})",
                            tag="GATEWAY"
                        )
                        await asyncio.sleep(STARTUP_PROBE_RETRY_DELAY)
                    # 最后一次失败在循环外处理
                except Exception as e:
                    last_exc = e
                    break  # 非连接级异常不重试，直接处理

            # 处理探测结果
            if response is not None:
                status = response.status_code
                # 状态码分类：404 = 端点不存在，5xx = 服务异常
                # [FIX 2026-03-02] 只有自研后端的 404 才 freeze；
                # 其他后端（copilot, kiro-gateway, ruoli 等）根 URL 返回 404 是正常的
                # （它们没有根路由处理器，但实际 API 端点 /v1/chat/completions 正常工作）
                _STRICT_404_FREEZE_BACKENDS = {"gcli2api-antigravity", "antigravity-tools", "ruoli"}
                if status == 404:
                    if backend_key in _STRICT_404_FREEZE_BACKENDS:
                        # 自研后端根 URL 应该返回 2xx，404 表示端点异常
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
                        # 其他后端根 URL 404 是正常的（无根路由），服务本身可达
                        log.info(
                            f"[STARTUP] ✅ {backend_key} returned HTTP 404 at {host}:{port} "
                            f"— root endpoint has no handler, but service is likely reachable"
                        )
                        results[backend_key] = True
                elif status >= 500:
                    await health_mgr.freeze_backend(
                        backend_key, duration=STARTUP_PROBE_FREEZE_DURATION,
                        reason=f"Startup probe: HTTP {status} at {host}:{port} — server error"
                    )
                    log.warning(
                        f"[STARTUP] ❄️ {backend_key} returned HTTP {status} at {host}:{port} "
                        f"— server error, pre-frozen for 5min"
                    )
                    results[backend_key] = False
                else:
                    # 2xx, 401, 403, 405, 429 等 — 服务在运行
                    log.info(
                        f"[STARTUP] ✅ {backend_key} is reachable at {host}:{port} "
                        f"(HTTP {status})"
                    )
                    results[backend_key] = True
            elif last_exc is not None:
                # 所有重试都失败了，或遇到非连接级异常
                if isinstance(last_exc, (httpx.ConnectError, httpx.ConnectTimeout,
                                          httpx.ReadTimeout, OSError)):
                    await health_mgr.freeze_backend(
                        backend_key, duration=STARTUP_PROBE_FREEZE_DURATION,
                        reason=f"Startup probe: {host}:{port} unreachable after {STARTUP_PROBE_MAX_RETRIES} retries"
                    )
                    log.warning(
                        f"[STARTUP] ❄️ {backend_key} unreachable at {host}:{port} after "
                        f"{STARTUP_PROBE_MAX_RETRIES} retries, pre-frozen for 5min"
                    )
                else:
                    await health_mgr.freeze_backend(
                        backend_key, duration=STARTUP_PROBE_FREEZE_DURATION,
                        reason=f"Startup probe error: {type(last_exc).__name__}: {str(last_exc)[:200]}"
                    )
                    log.warning(
                        f"[STARTUP] ❄️ {backend_key} probe failed at {host}:{port}: "
                        f"{type(last_exc).__name__}: {str(last_exc)[:200]}, pre-frozen for 5min"
                    )
                results[backend_key] = False

    return results

# ==================== 启动探测结束 ====================


async def route_request_with_fallback(
    endpoint: str,
    method: str,
    headers: Dict[str, str],
    body: Any,
    model: Optional[str] = None,
    stream: bool = False,
    local_handler: Optional[Callable] = None,
    backends: Optional[Dict[str, Dict[str, Any]]] = None,
    enable_cross_pool_fallback: Optional[bool] = None,
) -> Any:
    """
    带故障转移的请求路由

    优先使用指定后端，失败时自动切换到备用后端

    路由策略：
    1. Antigravity (priority=1) - 优先使用，支持 Claude 4.5 (sonnet/opus) 和 Gemini 2.5/3
    2. Kiro Gateway (priority=2) - Claude 模型的降级后端（包括 haiku）
    3. AnyRouter (priority=3) - 公益站第三方 API（支持所有 Claude）
    4. Copilot (priority=4) - 最终兜底，支持所有模型

    [FIX 2026-01-21] Opus 模型特殊处理：
    - 所有后端失败后，才进行跨模型降级到 Gemini
    - 降级顺序: AG (所有凭证) -> Kiro -> AnyRouter -> Copilot -> 跨模型

    Haiku 模型特殊处理：
    - Antigravity 不支持 Haiku，会被跳过
    - 直接走 Kiro -> AnyRouter -> Copilot 链路
    - 全部失败后降级到 gemini-3-flash

    Args:
        endpoint: API 端点
        method: HTTP 方法
        headers: 请求头
        body: 请求体
        model: 模型名称 (用于选择后端)
        stream: 是否流式响应
        local_handler: 本地处理器
        backends: 后端配置字典

    Returns:
        响应内容

    Raises:
        HTTPException: 所有后端都失败时
    """
    # [FIX 2026-02-03] 追踪活跃请求，用于 SmartWarmup 静默期检测
    # 当有活跃请求时，SmartWarmup 会推迟预热操作，避免抢占网络资源
    tracker = get_active_request_tracker()
    async with tracker.track_request():
        return await _route_request_with_fallback_impl(
            endpoint=endpoint,
            method=method,
            headers=headers,
            body=body,
            model=model,
            stream=stream,
            local_handler=local_handler,
            backends=backends,
            enable_cross_pool_fallback=enable_cross_pool_fallback,
        )


async def _route_request_with_fallback_impl(
    endpoint: str,
    method: str,
    headers: Dict[str, str],
    body: Any,
    model: Optional[str] = None,
    stream: bool = False,
    local_handler: Optional[Callable] = None,
    backends: Optional[Dict[str, Dict[str, Any]]] = None,
    enable_cross_pool_fallback: Optional[bool] = None,
) -> Any:
    """
    route_request_with_fallback 的内部实现

    [FIX 2026-02-03] 从 route_request_with_fallback 拆分出来，
    以便在 track_request() 上下文管理器内执行
    """
    if backends is None:
        backends = BACKENDS

    # ==================== [FIX 2026-02-02] IDE 跨模型降级限制 ====================
    # 当 enable_cross_pool_fallback 未显式传递时，从 headers 自动检测客户端类型
    if enable_cross_pool_fallback is None:
        try:
            from akarins_gateway.ide_compat import ClientTypeDetector
            client_info = ClientTypeDetector.detect(headers)
            enable_cross_pool_fallback = client_info.enable_cross_pool_fallback
            log.debug(
                f"[GATEWAY] Auto-detected enable_cross_pool_fallback={enable_cross_pool_fallback} "
                f"(client={client_info.display_name})",
                tag="GATEWAY"
            )
        except Exception as e:
            log.warning(f"[GATEWAY] Failed to detect client type, defaulting enable_cross_pool_fallback=True: {e}", tag="GATEWAY")
            enable_cross_pool_fallback = True
    # ==================== End of IDE 跨模型降级限制 ====================

    # ==================== [FIX 2026-02-01] Pre-Request 诊断集成 ====================
    # 在发送请求前进行上下文完整性检查
    if _DIAGNOSTICS_ENABLED and _context_validator and isinstance(body, dict):
        messages = body.get("messages", [])
        tools = body.get("tools")
        thinking_config = body.get("thinking")

        # 生成 SCID（如果 body 中没有）
        scid = body.get("scid", "")
        if not scid and messages:
            # 使用第一条消息内容生成简单 SCID
            import hashlib
            first_content = str(messages[0].get("content", ""))[:100]
            scid = f"diag_{hashlib.sha256(first_content.encode()).hexdigest()[:12]}"

        if messages:
            try:
                fixed_messages, report, fix_stats = _context_validator.validate_and_fix(
                    scid=scid,
                    messages=messages,
                    tools=tools,
                    thinking_config=thinking_config
                )

                if fixed_messages != messages:
                    body["messages"] = fixed_messages
                    messages = fixed_messages
                    log.warning(
                        f"[GATEWAY][DIAGNOSTICS] Auto-fix applied before routing: "
                        f"messages={fix_stats.get('messages_before', len(messages))} -> {fix_stats.get('messages_after', len(fixed_messages))}, "
                        f"dropped_unanchored_tool_results={fix_stats.get('dropped_unanchored_tool_results', 0)}, "
                        f"dropped_mismatched_tool_results={fix_stats.get('dropped_mismatched_tool_results', 0)}",
                        tag="GATEWAY"
                    )

                # 记录诊断结果
                if not report.is_valid:
                    log.warning(
                        f"[GATEWAY][DIAGNOSTICS] Pre-request validation FAILED: "
                        f"errors={report.error_count}, warnings={report.warning_count}, "
                        f"scid={scid[:20]}...",
                        tag="GATEWAY"
                    )
                    # 记录每个 ERROR 级别的问题
                    for diag in report.diagnostics:
                        if diag.level == DiagnosticLevel.ERROR:
                            log.error(
                                f"[GATEWAY][DIAGNOSTICS] {diag.category.value}: {diag.message}",
                                tag="GATEWAY"
                            )
                    # 注意：当前仅记录日志，不阻止请求
                    # 未来可以添加 block_on_error 配置来阻止请求
                elif report.warning_count > 0:
                    log.info(
                        f"[GATEWAY][DIAGNOSTICS] Pre-request validation passed with warnings: "
                        f"warnings={report.warning_count}, scid={scid[:20]}...",
                        tag="GATEWAY"
                    )
                else:
                    log.debug(
                        f"[GATEWAY][DIAGNOSTICS] Pre-request validation passed: scid={scid[:20]}...",
                        tag="GATEWAY"
                    )
            except Exception as e:
                # 诊断失败不应阻止请求
                log.warning(f"[GATEWAY][DIAGNOSTICS] Validation failed with exception: {e}", tag="GATEWAY")
    # ==================== End of Pre-Request 诊断 ====================

    # ✅ [FIX 2026-01-22] 优先使用 model_routing 配置的降级链
    log.info(f"[GATEWAY] route_request_with_fallback called: model={model}, endpoint={endpoint}", tag="GATEWAY")
    routing_rule = get_model_routing_rule(model) if model else None
    
    if routing_rule:
        log.info(f"[GATEWAY] Found routing_rule for {model}: enabled={routing_rule.enabled}", tag="GATEWAY")
    else:
        log.debug(f"[GATEWAY] No routing_rule found for {model}, will use default priority", tag="GATEWAY")
    
    backend_chain = None
    
    if routing_rule and routing_rule.enabled and routing_rule.backend_chain:
        # 使用配置的降级链
        log.info(f"[GATEWAY] Found model_routing rule for {model}: enabled={routing_rule.enabled}, chain_length={len(routing_rule.backend_chain)}", tag="GATEWAY")
        backend_chain = []
        # [FIX 2026-02-25] Hoist health_mgr outside loop to avoid repeated singleton lookup
        _chain_health_mgr = get_backend_health_manager()
        for entry in routing_rule.backend_chain:
            backend_config = backends.get(entry.backend, {})
            target_model = entry.model
            backend_key = entry.backend

            # [FIX 2026-02-15] Guard against ghost backends:
            # If a backend is referenced in gateway.yaml but not registered in BACKENDS
            # (e.g. dkapi/cifang disabled in stations.py), backends.get() returns {}.
            # Empty dict has no base_url and would cause connection failures.
            if not backend_config or "base_url" not in backend_config:
                log.warning(
                    f"[GATEWAY] ⚠️ Skipping ghost backend '{backend_key}': "
                    f"not registered in BACKENDS (config empty or missing base_url)",
                    tag="GATEWAY"
                )
                continue

            backend_enabled = backend_config.get("enabled", True)

            log.debug(f"[GATEWAY] Checking backend {backend_key}: enabled={backend_enabled}, target_model={target_model}", tag="GATEWAY")

            if not backend_enabled:
                log.debug(f"[GATEWAY] Skipping {backend_key} (disabled)", tag="GATEWAY")
                continue

            # [NEW 2026-02-25] P1: 链构建阶段预过滤冻结后端
            # 冻结后端在链构建时就被排除，执行循环中的 is_frozen() 检查保留为双重保险
            if await _chain_health_mgr.is_frozen(backend_key):
                remaining = await _chain_health_mgr.get_freeze_remaining(backend_key)
                log.info(
                    f"[GATEWAY] ❄️ Pre-filtering frozen backend {backend_key} from chain "
                    f"(remaining: {remaining:.1f}s)",
                    tag="GATEWAY"
                )
                continue

            # [NEW 2026-02-25] P0: Antigravity 凭证门 — 无可用弹药时跳过
            # [FIX 2026-02-26] 升级为 has_usable_antigravity_credentials()，同时过滤 disabled 凭证
            if backend_key == "gcli2api-antigravity":
                try:
                    # STUB: antigravity_anthropic_router (ENABLE_ANTIGRAVITY feature flag)
                    try:
                        from akarins_gateway.gateway.backends.antigravity.router import get_credential_manager as _get_ag_cred_mgr
                    except ImportError:
                        _get_ag_cred_mgr = None
                    _ag_cred_mgr = await _get_ag_cred_mgr()
                    if not await _ag_cred_mgr.has_usable_antigravity_credentials():
                        log.warning(
                            f"[GATEWAY] Pre-filtering antigravity: no usable credentials",
                            tag="GATEWAY"
                        )
                        continue
                except Exception as e:
                    log.debug(f"[GATEWAY] Cannot check antigravity credentials: {e}", tag="GATEWAY")
                    # Fail open: check failure does not block — let the request try normally

            # ==================== [REFACTOR 2026-02-21] Phase B-2: Unified backend capability check ====================
            # 公益站使用 PublicStationManager 动态检查，其他后端使用 YAML backend_capabilities
            if _get_psm().is_public_station(backend_key):
                supported = _get_psm().supports_model(backend_key, target_model)
                station_name = _get_psm().get(backend_key).display_name if _get_psm().get(backend_key) else backend_key
                if not supported:
                    log.warning(f"[GATEWAY] ⚠️ Skipping {station_name}: model {target_model} not supported", tag="GATEWAY")
                    continue
                else:
                    log.info(f"[GATEWAY] ✅ {station_name} supports {target_model}, adding to chain", tag="GATEWAY")
            elif not is_backend_capable(backend_key, target_model):
                log.warning(f"[GATEWAY] ⚠️ Skipping {backend_key}: model {target_model} not supported (YAML capability)", tag="GATEWAY")
                continue
            else:
                log.info(f"[GATEWAY] ✅ {backend_key} supports {target_model}, adding to chain", tag="GATEWAY")
            # ====================================================================================
            
            # [FIX 2026-02-02] IDE 客户端禁止跨模型降级：排除 target_model 与 model 不同的条目
            if not enable_cross_pool_fallback and _is_cross_model_entry(model or "", target_model or ""):
                log.info(
                    f"[GATEWAY] Skipping cross-model entry for IDE client: {backend_key}({target_model}) "
                    f"(requested model={model})",
                    tag="GATEWAY"
                )
                continue
            
            backend_chain.append((backend_key, backend_config, target_model))
            log.debug(f"[GATEWAY] Added {backend_key} to chain (target_model={target_model})", tag="GATEWAY")
        
        if backend_chain:
            # [NEW 2026-01-24] 显示完整的降级链路径
            chain_path = " → ".join([f"{b[0]}({b[2]})" for b in backend_chain])
            log.info(f"[GATEWAY] ✅ Using model_routing chain for {model}: {[b[0] for b in backend_chain]}", tag="GATEWAY")
            log.info(f"[GATEWAY] 📍 完整降级链路径: {chain_path}", tag="GATEWAY")
        else:
            log.warning(f"[GATEWAY] ⚠️ model_routing chain for {model} is empty after filtering!", tag="GATEWAY")
            log.warning(f"[GATEWAY] ⚠️ Will fallback to default priority order (antigravity priority=1, kiro-gateway priority=2)", tag="GATEWAY")
    
    # [FIX 2026-03-14] CRITICAL-1: Respect default_routing / catch_all chains from gateway.yaml
    # Three-step fallback: default_routing → catch_all → global priority sort
    # Track which rule built the chain for fallback_on determination (fixes C1 + I2 from code review)
    _chain_source_rule = None  # Will hold the DefaultRoutingRule that built the chain

    # Step 2: Try default_routing chain (pattern-based rules from gateway.yaml)
    if not backend_chain and model:
        default_rule = get_default_routing_rule(model)
        if default_rule and default_rule.chain:
            log.info(
                f"[GATEWAY] Found default_routing rule for {model} "
                f"(pattern={default_rule.pattern}), building chain",
                tag="GATEWAY"
            )
            backend_chain = []
            _dr_health_mgr = get_backend_health_manager()
            for entry in default_rule.chain:
                backend_key = entry.backend
                backend_config = backends.get(backend_key, {})
                if not backend_config or "base_url" not in backend_config:
                    log.warning(
                        f"[GATEWAY] ⚠️ Skipping ghost backend '{backend_key}' in default_routing",
                        tag="GATEWAY"
                    )
                    continue
                if not backend_config.get("enabled", True):
                    continue
                if await _dr_health_mgr.is_frozen(backend_key):
                    remaining = await _dr_health_mgr.get_freeze_remaining(backend_key)
                    log.info(
                        f"[GATEWAY] ❄️ Pre-filtering frozen backend {backend_key} "
                        f"from default_routing chain (remaining: {remaining:.1f}s)",
                        tag="GATEWAY"
                    )
                    continue
                if not _is_model_supported_by_backend(backend_key, model):
                    log.debug(
                        f"[GATEWAY] Skipping {backend_key} in default_routing: "
                        f"model {model} not supported",
                        tag="GATEWAY"
                    )
                    continue
                backend_chain.append((backend_key, backend_config, model))

            if backend_chain:
                chain_path = " → ".join([b[0] for b in backend_chain])
                log.info(
                    f"[GATEWAY] ✅ Using default_routing chain for {model}: {chain_path}",
                    tag="GATEWAY"
                )
                _chain_source_rule = default_rule  # Track source for fallback_on
            else:
                log.warning(
                    f"[GATEWAY] ⚠️ default_routing chain for {model} is empty after filtering",
                    tag="GATEWAY"
                )
                backend_chain = None  # Reset to trigger next step

    # Step 2b: Try catch_all chain
    if not backend_chain and model:
        catch_all = get_catch_all_routing()
        if catch_all and catch_all.chain:
            log.info(f"[GATEWAY] Trying catch_all routing chain for {model}", tag="GATEWAY")
            backend_chain = []
            _ca_health_mgr = get_backend_health_manager()
            for entry in catch_all.chain:
                backend_key = entry.backend
                backend_config = backends.get(backend_key, {})
                if not backend_config or "base_url" not in backend_config:
                    continue
                if not backend_config.get("enabled", True):
                    continue
                if await _ca_health_mgr.is_frozen(backend_key):
                    continue
                if not _is_model_supported_by_backend(backend_key, model):
                    continue
                backend_chain.append((backend_key, backend_config, model))

            if backend_chain:
                chain_path = " → ".join([b[0] for b in backend_chain])
                log.info(
                    f"[GATEWAY] ✅ Using catch_all chain for {model}: {chain_path}",
                    tag="GATEWAY"
                )
                _chain_source_rule = catch_all  # Track source for fallback_on
            else:
                backend_chain = None  # Reset to trigger next step

    # Step 3: Final fallback — global priority sort (existing logic)
    if not backend_chain:
        log.info(f"[GATEWAY] No routing chain found for {model}, using global priority order", tag="GATEWAY")
        specified_backend = get_backend_for_model(model) if model else None
        sorted_backends = get_sorted_backends()

        if specified_backend:
            # 将指定后端移到最前面
            sorted_backends = [(k, v) for k, v in sorted_backends if k == specified_backend] + \
                             [(k, v) for k, v in sorted_backends if k != specified_backend]

        # [FIX 2026-03-10] Filter by capability — only try backends that support this model
        # Put capable backends first, then remaining backends as fallback
        capable = []
        fallback = []
        for k, v in sorted_backends:
            if model and _is_model_supported_by_backend(k, model):
                capable.append((k, v, model))
            else:
                fallback.append((k, v, model))

        if capable:
            backend_chain = capable + fallback
            log.info(
                f"[GATEWAY] Capability-filtered chain for {model}: "
                f"capable={[b[0] for b in capable]}, fallback={[b[0] for b in fallback]}",
                tag="GATEWAY"
            )
        else:
            # No backend claims support — keep all (legacy behavior for unknown models)
            backend_chain = [(k, v, model) for k, v in sorted_backends]
            log.warning(f"[GATEWAY] No backend claims support for {model}, trying all", tag="GATEWAY")

    last_error = None

    # [FIX 2026-03-16] CRITICAL-2 + C1 fix: Determine active fallback_on from the ACTUAL chain source
    # Uses _chain_source_rule tracked during chain building to avoid re-querying and to cover catch_all.
    # If fallback_on is empty/not configured, all errors trigger fallback (backward compatible).
    active_fallback_on = set()
    if routing_rule and routing_rule.enabled and routing_rule.fallback_on:
        active_fallback_on = routing_rule.fallback_on
        log.debug(f"[GATEWAY] Using model_routing fallback_on: {active_fallback_on}", tag="GATEWAY")
    elif _chain_source_rule is not None and hasattr(_chain_source_rule, 'fallback_on') and _chain_source_rule.fallback_on:
        active_fallback_on = _chain_source_rule.fallback_on
        log.debug(f"[GATEWAY] Using chain_source fallback_on: {active_fallback_on}", tag="GATEWAY")

    # [FIX 2026-01-23] 获取模型名称（用于限流跟踪）
    model_name = body.get("model") if isinstance(body, dict) else model
    
    # [FIX 2026-01-23] 在尝试后端前，检查是否有后端在限流中
    from .rate_limit_handler import get_rate_limit_tracker
    tracker = get_rate_limit_tracker()
    
    # 过滤掉限流中的后端
    available_backends = []
    for backend_key, backend_config, target_model in backend_chain:
        account_id = _build_account_id(backend_key, headers, body)
        model_account_id = _build_model_account_id(account_id, model_name)
        backend_limit_id = _build_backend_limit_id(backend_key)

        # 检查后端级别限流（用于 5xx 软避让）
        if tracker.is_rate_limited(backend_limit_id):
            remaining = tracker.get_reset_seconds(backend_limit_id)
            if remaining:
                log.warning(f"[FALLBACK] 后端 {backend_config.get('name', backend_key)} 软避让中，剩余 {remaining:.1f} 秒，跳过")
                continue
        
        # 检查账号级别限流
        if tracker.is_rate_limited(account_id):
            remaining = tracker.get_reset_seconds(account_id)
            if remaining:
                log.warning(f"[FALLBACK] 后端 {backend_config.get('name', backend_key)} 账号限流中，剩余 {remaining:.1f} 秒，跳过")
                continue
        
        # 检查模型级别限流（如果有模型名）
        if model_account_id:
            if tracker.is_rate_limited(model_account_id):
                remaining = tracker.get_reset_seconds(model_account_id)
                if remaining:
                    log.warning(f"[FALLBACK] 后端 {backend_config.get('name', backend_key)} 模型 {model_name} 限流中，剩余 {remaining:.1f} 秒，跳过")
                    continue
        
        available_backends.append((backend_key, backend_config, target_model))
    
    # 如果没有可用后端，仍然尝试所有后端（避免所有后端都被误判为限流）
    if not available_backends:
        log.warning("[FALLBACK] 所有后端都在限流中，但仍尝试请求（可能是误判）")
        available_backends = backend_chain

    # [SEC 2026-02-21] SEC-9: 请求放大保护计数器
    _fallback_attempt_count = 0

    for backend_key, backend_config, target_model in available_backends:
        # [FIX 2026-02-03 v2] 检查后端是否被冻结（Connection Refused 自动冻结）
        # 注意：is_frozen() 和 get_freeze_remaining() 现在是线程安全的 async 方法
        health_mgr = get_backend_health_manager()
        if await health_mgr.is_frozen(backend_key):
            remaining = await health_mgr.get_freeze_remaining(backend_key)
            log.warning(
                f"[GATEWAY] ⏸️ Skipping frozen backend {backend_key} "
                f"(remaining: {remaining:.1f}s)",
                tag="GATEWAY"
            )
            continue

        # ✅ [FIX 2026-01-22] 如果使用 model_routing，更新请求体中的模型
        request_body = body
        if target_model and target_model != model and isinstance(body, dict):
            request_body = body.copy()
            request_body["model"] = target_model
            log.debug(f"[GATEWAY] Using target model {target_model} instead of {model} for backend {backend_key}", tag="GATEWAY")

        # [FIX 2026-01-21] Copilot 熔断器检查
        # 如果 Copilot 已返回 402 余额不足，跳过该后端
        if backend_key == "copilot" and is_copilot_circuit_open():
            log.debug(f"Skipping Copilot (circuit breaker open - quota exceeded)", tag="GATEWAY")
            continue

        # [SEC 2026-02-21] SEC-9: 请求放大保护
        _fallback_attempt_count += 1
        if _fallback_attempt_count > MAX_FALLBACK_ATTEMPTS:
            log.warning(
                f"[GATEWAY] ⛔ MAX_FALLBACK_ATTEMPTS ({MAX_FALLBACK_ATTEMPTS}) reached, "
                f"stopping fallback chain for {endpoint} (model={model})",
                tag="GATEWAY"
            )
            break

        log.info(f"[GATEWAY] 🔄 Trying backend: {backend_config.get('name', backend_key)} ({backend_key}) for {endpoint} (model={target_model or model})", tag="GATEWAY")
        
        # ✅ [DEBUG 2026-01-22] 特别标记 Kiro Gateway 请求
        if backend_key == "kiro-gateway":
            log.info(f"[GATEWAY] 🎯 KIRO GATEWAY REQUEST: model={target_model or model}, endpoint={endpoint}", tag="GATEWAY")

        success, result = await proxy_request_to_backend(
            backend_key=backend_key,
            endpoint=endpoint,
            method=method,
            headers=headers,
            body=request_body,
            stream=stream,
            local_handler=local_handler,
            backends=backends,
        )

        # [FIX 2026-01-21] 记录后端健康状态
        health_mgr = get_backend_health_manager()

        if success:
            await health_mgr.record_success(backend_key)
            if hasattr(log, 'success'):
                log.success(f"Request succeeded via {backend_config.get('name', backend_key)}", tag="GATEWAY")
            return result

        await health_mgr.record_failure(backend_key)
        last_error = result

        # [FIX 2026-03-16] I1 fix: Tighten regex to avoid matching non-HTTP numbers
        # (e.g., IP octets like "192", timeouts like "300 seconds")
        # Priority: "status 429" > "HTTP/1.1 503" > leading 3-digit code > fallback any 3-digit
        _fb_status_code = None
        _fb_error_type = None
        if isinstance(result, str):
            _fb_match = re.search(
                r'(?:status[_\s]*(?:code)?[:\s=]*(\d{3}))'   # "status_code=429", "status: 503"
                r'|(?:HTTP/\d\.\d\s+(\d{3}))'                # "HTTP/1.1 429"
                r'|(?:^(\d{3})\b)',                           # leading "429 Too Many Requests"
                result, re.IGNORECASE
            )
            if _fb_match:
                _fb_status_code = int(next(g for g in _fb_match.groups() if g is not None))
            if "timeout" in result.lower():
                _fb_error_type = "timeout"
            elif "connection" in result.lower():
                _fb_error_type = "connection_error"

        # Check if this error type warrants fallback to next backend
        if active_fallback_on:
            # fallback_on is configured — only continue if error matches
            should_continue = (
                (_fb_status_code is not None and _fb_status_code in active_fallback_on) or
                (_fb_error_type is not None and _fb_error_type in active_fallback_on)
            )
            if not should_continue:
                log.warning(
                    f"[GATEWAY] ⛔ Error {_fb_status_code}/{_fb_error_type} not in "
                    f"fallback_on={active_fallback_on}, stopping chain for {backend_config.get('name', backend_key)}",
                    tag="GATEWAY"
                )
                break  # Don't try next backend — error type not in fallback_on
            else:
                log.info(
                    f"[GATEWAY] Error {_fb_status_code}/{_fb_error_type} matches "
                    f"fallback_on, continuing to next backend",
                    tag="GATEWAY"
                )
        # else: no fallback_on configured, try all backends (backward compatible)

        log.warning(f"Backend {backend_config.get('name', backend_key)} failed: {result}, trying next...", tag="GATEWAY")

    # ==================== [REFACTOR 2026-02-21] Phase B-3: YAML-driven cross-model fallback ====================
    # 跨模型降级：所有后端失败后，根据 YAML cross_model_fallback 规则降级到其他模型
    if enable_cross_pool_fallback:
        fallback_rule = get_cross_model_fallback(model or "")
        if fallback_rule:
            fb_backend_key = fallback_rule.backend
            fb_backend_config = backends.get(fb_backend_key, {})
            fb_health_mgr = get_backend_health_manager()

            # [FIX 2026-02-21] I5: 检查降级目标后端是否可用（enabled + frozen + circuit breaker）
            fb_skip = False
            if not fb_backend_config.get("enabled", True):
                log.debug(f"[GATEWAY FALLBACK] Cross-model fallback backend {fb_backend_key} is disabled, skipping", tag="GATEWAY")
                fb_skip = True
            elif await fb_health_mgr.is_frozen(fb_backend_key):
                remaining = await fb_health_mgr.get_freeze_remaining(fb_backend_key)
                log.warning(f"[GATEWAY FALLBACK] Cross-model fallback backend {fb_backend_key} is frozen (remaining: {remaining:.1f}s), skipping", tag="GATEWAY")
                fb_skip = True
            elif fb_backend_key == "copilot" and is_copilot_circuit_open():
                log.debug(f"[GATEWAY FALLBACK] Cross-model fallback to copilot skipped (circuit breaker open)", tag="GATEWAY")
                fb_skip = True

            if not fb_skip:
                # [SEC 2026-02-21] SEC-9: 请求放大保护
                _fallback_attempt_count += 1
                if _fallback_attempt_count > MAX_FALLBACK_ATTEMPTS:
                    log.warning(
                        f"[GATEWAY] ⛔ MAX_FALLBACK_ATTEMPTS ({MAX_FALLBACK_ATTEMPTS}) reached, "
                        f"skipping cross-model fallback for {endpoint} (model={model})",
                        tag="GATEWAY"
                    )
                    fb_skip = True

            if not fb_skip:
                log.warning(
                    f"[GATEWAY FALLBACK] 所有后端失败，尝试跨模型降级: {model} -> {fallback_rule.fallback_model} "
                    f"(backend: {fb_backend_key})",
                    tag="GATEWAY"
                )

                fallback_body = body.copy() if isinstance(body, dict) else body
                if isinstance(fallback_body, dict):
                    fallback_body["model"] = fallback_rule.fallback_model
                    fallback_body = sanitize_model_params(fallback_body, fallback_rule.fallback_model)

                success, result = await proxy_request_to_backend(
                    backend_key=fb_backend_key,
                    endpoint=endpoint,
                    method=method,
                    headers=headers,
                    body=fallback_body,
                    stream=stream,
                    local_handler=local_handler,
                    backends=backends,
                )

                if success:
                    if hasattr(log, 'success'):
                        log.success(f"[GATEWAY FALLBACK] 跨模型降级成功: {model} -> {fallback_rule.fallback_model}", tag="GATEWAY")
                    return result
                else:
                    log.error(f"[GATEWAY FALLBACK] 跨模型降级也失败: {result}", tag="GATEWAY")
                    last_error = result
        else:
            log.debug("[GATEWAY] No cross-model fallback rule for this model, skipping", tag="GATEWAY")

    # ==================== [REFACTOR 2026-02-21] Phase B-3: YAML-driven final fallback ====================
    # 最终兜底：根据 YAML final_fallback 配置尝试最后的后端
    final_fb = get_final_fallback()
    if final_fb and final_fb.enabled:
        fb_backend = final_fb.backend
        fb_config = backends.get(fb_backend, {})
        if fb_config.get("enabled", True):
            # [FIX 2026-02-21] I6: 检查 frozen 状态 + circuit breaker
            final_health_mgr = get_backend_health_manager()
            should_skip = False
            if await final_health_mgr.is_frozen(fb_backend):
                remaining = await final_health_mgr.get_freeze_remaining(fb_backend)
                log.warning(f"[GATEWAY FALLBACK] Final fallback backend {fb_backend} is frozen (remaining: {remaining:.1f}s), skipping", tag="GATEWAY")
                should_skip = True
            elif final_fb.respect_circuit_breaker and is_copilot_circuit_open():
                log.debug(f"[GATEWAY FALLBACK] Final fallback to {fb_backend} skipped (circuit breaker open)", tag="GATEWAY")
                should_skip = True
            # [SEC 2026-02-21] SEC-9: 请求放大保护
            if not should_skip and _fallback_attempt_count >= MAX_FALLBACK_ATTEMPTS:
                log.warning(
                    f"[GATEWAY] ⛔ MAX_FALLBACK_ATTEMPTS ({MAX_FALLBACK_ATTEMPTS}) reached, "
                    f"skipping final fallback for {endpoint} (model={model})",
                    tag="GATEWAY"
                )
                should_skip = True
            if not should_skip:
                log.warning(f"[GATEWAY FALLBACK] 尝试 {fb_backend} 作为最终兜底", tag="GATEWAY")
                # Note: Copilot backend model mapping is handled inside proxy_request_to_backend()
                # (lines ~1667-1673): it calls map_model_for_copilot() when backend_key == "copilot",
                # so passing the original body with the original model name here is safe.
                # The mapping handles: claude-opus-4.6 -> claude-opus-4-6, gemini variants, etc.
                success, result = await proxy_request_to_backend(
                    backend_key=fb_backend,
                    endpoint=endpoint,
                    method=method,
                    headers=headers,
                    body=body,
                    stream=stream,
                    local_handler=local_handler,
                    backends=backends,
                )
                if success:
                    if hasattr(log, 'success'):
                        log.success(f"[GATEWAY FALLBACK] {fb_backend} 兜底成功", tag="GATEWAY")
                    return result
                else:
                    log.error(f"[GATEWAY FALLBACK] {fb_backend} 兜底也失败: {result}", tag="GATEWAY")
                    last_error = result

    # 所有后端、降级和 Copilot 都失败
    raise HTTPException(
        status_code=503,
        detail=f"All backends failed. Last error: {last_error}"
    )
