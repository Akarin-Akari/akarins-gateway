"""
Gateway Anthropic 格式端点

包含 /v1/messages 等 Anthropic 兼容端点。

从 unified_gateway_router.py 抽取的 Anthropic 格式端点。

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-01-18
"""

from typing import Dict, Any, List, Tuple
import json
import os
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse, JSONResponse

from akarins_gateway.streaming_constants import STREAMING_HEADERS
from ..proxy import route_request_with_fallback

# 延迟导入 log，避免循环依赖
try:
    from akarins_gateway.core.log import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

# 延迟导入认证依赖
try:
    from akarins_gateway.core.auth import authenticate_bearer, authenticate_bearer_allow_local_dummy
except ImportError:
    # 提供默认的认证函数
    async def authenticate_bearer():
        return "dummy"
    async def authenticate_bearer_allow_local_dummy():
        return "dummy"

# [FIX 2026-03-03] 导入 PCC（渐进式上下文压缩）替代粗暴裁剪
try:
    from akarins_gateway.context_truncation import apply_pcc_before_request
except ImportError:
    apply_pcc_before_request = None

router = APIRouter()

__all__ = ["router"]


# ─── Anthropic-format context truncation (safety net) ────────────────────────
# Prevents oversized requests (e.g. /compact with 200K+ tokens) from
# overwhelming the backend.  This is a coarse filter — zerogravity's MITM
# trim.rs handles fine-grained Gemini-format trimming downstream.
#
# Token estimation: len(json.dumps(messages)) // 4  (ASCII-escaped JSON).
# For mixed CJK/English this slightly overestimates, which is safer.

_GATEWAY_ANTHROPIC_TOKEN_LIMIT: int = int(
    os.environ.get("GATEWAY_MAX_ANTHROPIC_TOKENS", "160000")
)


def _truncate_anthropic_messages(body: dict) -> Tuple[dict, bool]:
    """
    Coarse context truncation for Anthropic Messages API requests.

    Three-phase approach (mirrors zerogravity trim.rs):
      1. Estimate total tokens from messages[] + system prompt
      2. If over limit: keep first 2 + last 5 messages, drop middle
      3. If still over: iteratively halve until under limit

    Returns (body, was_truncated).  Body is mutated in-place for efficiency
    (the caller owns the dict from request.json()).
    """
    messages = body.get("messages")
    if not messages or not isinstance(messages, list) or len(messages) <= 7:
        return body, False

    # --- Estimate total input tokens ---
    msg_json_len = len(json.dumps(messages))  # ensure_ascii=True by default
    system = body.get("system", "")
    if isinstance(system, str):
        sys_len = len(system)
    elif isinstance(system, list):
        sys_len = len(json.dumps(system))
    else:
        sys_len = 0

    est_tokens = (msg_json_len + sys_len) // 4

    if est_tokens <= _GATEWAY_ANTHROPIC_TOKEN_LIMIT:
        return body, False

    original_count = len(messages)
    original_tokens = est_tokens

    # --- Phase 1: Keep first 2 + last 5, drop middle ---
    keep_front, keep_back = 2, 5
    if len(messages) > keep_front + keep_back:
        messages = messages[:keep_front] + messages[-keep_back:]

    # --- Phase 2: Iteratively halve until under limit ---
    for _ in range(10):
        est_tokens = len(json.dumps(messages)) // 4
        if est_tokens <= _GATEWAY_ANTHROPIC_TOKEN_LIMIT or len(messages) <= 2:
            break
        half = len(messages) // 2
        if half < 2:
            break
        messages = [messages[0]] + messages[half:]

    final_tokens = len(json.dumps(messages)) // 4
    body["messages"] = messages

    log.info(
        f"Truncated Anthropic messages: {original_count}→{len(messages)} msgs, "
        f"~{original_tokens}→{final_tokens} est tokens "
        f"(limit={_GATEWAY_ANTHROPIC_TOKEN_LIMIT})",
        tag="GATEWAY",
    )
    return body, True


# ==================== Anthropic Messages 端点 ====================

@router.post("/v1/messages")
@router.post("/messages")  # 别名路由，兼容 Base URL 为 /gateway 的客户端
async def anthropic_messages(
    request: Request,
    token: str = Depends(authenticate_bearer)
):
    """Anthropic Messages API 兼容端点"""
    log.info(f"Messages request received", tag="GATEWAY")
    
    # 检测客户端类型（用于日志和后续处理）
    try:
        from akarins_gateway.ide_compat import ClientTypeDetector
        client_info = ClientTypeDetector.detect(dict(request.headers))
    except Exception as e:
        log.warning(f"Failed to detect client type: {e}", tag="GATEWAY")
        client_info = None
    
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # ── Context compression: prevent oversized requests from reaching backend ──
    # [FIX 2026-03-03] 使用 PCC（渐进式上下文压缩）替代粗暴裁剪
    # PCC 保证 tool_use/tool_result 配对完整性、保留 thinking 签名
    try:
        if apply_pcc_before_request is not None:
            messages = body.get("messages", [])
            if messages and len(messages) > 7:
                compressed_messages, pcc_stats = apply_pcc_before_request(
                    messages,
                    model_name=body.get("model", "claude"),
                    max_output_tokens=body.get("max_tokens", 16384),
                    compress_tool_results=True,
                    client_type=client_info.client_type if client_info else None,
                )
                tokens_saved = pcc_stats.get("total_tokens_saved", 0)
                if tokens_saved > 0:
                    body["messages"] = compressed_messages
                    log.info(
                        f"PCC compressed: {pcc_stats['original_messages']}→{pcc_stats['final_messages']} msgs, "
                        f"~{pcc_stats['original_tokens']}→{pcc_stats['final_tokens']} tokens "
                        f"(saved {tokens_saved})",
                        tag="GATEWAY",
                    )
        else:
            # Fallback: PCC 不可用时使用旧的粗暴裁剪
            body, _ = _truncate_anthropic_messages(body)
    except Exception as e:
        log.warning(f"Context compression failed (continuing with original): {e}", tag="GATEWAY")

    model = body.get("model", "")
    stream = body.get("stream", False)

    headers = dict(request.headers)

    result = await route_request_with_fallback(
        endpoint="/messages",
        method="POST",
        headers=headers,
        body=body,
        model=model,
        stream=stream,
        enable_cross_pool_fallback=client_info.enable_cross_pool_fallback if client_info else None,
    )

    if stream and hasattr(result, "__anext__"):
        return StreamingResponse(
            result,
            media_type="text/event-stream",
            headers=STREAMING_HEADERS,
        )

    return JSONResponse(content=result)


@router.post("/v1/messages/count_tokens")
@router.post("/messages/count_tokens")  # 别名路由，兼容 Base URL 为 /gateway 的客户端
async def anthropic_messages_count_tokens(
    request: Request,
    token: str = Depends(authenticate_bearer)
):
    """
    Anthropic Messages API 兼容的 token 计数端点。

    Claude CLI 在执行 /context 命令时会调用此端点来统计 token 使用量。
    这是一个辅助端点，不消耗配额，只返回估算的 token 数量。
    """
    log.info(f"Count tokens request received", tag="GATEWAY")

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # 简单估算 token 数量
    input_tokens = 0

    try:
        messages = body.get("messages", [])
        system_prompt = body.get("system", "")

        # 粗略估算：每4个字符约等于1个token（对于混合中英文）
        total_chars = len(system_prompt) if isinstance(system_prompt, str) else 0

        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                # 多模态内容
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            total_chars += len(item.get("text", ""))
                        elif item.get("type") == "image":
                            # 图片大约消耗 1000 tokens
                            total_chars += 4000

        # 粗略估算
        input_tokens = max(1, total_chars // 4)

    except Exception as e:
        log.warning(f"Token estimation failed: {e}", tag="GATEWAY")
        input_tokens = 100  # 默认值

    log.debug(f"Estimated input tokens: {input_tokens}", tag="GATEWAY")

    return JSONResponse(content={"input_tokens": input_tokens})
