"""
Gateway OpenAI 格式端点

包含 /v1/chat/completions 等 OpenAI 兼容端点。

从 unified_gateway_router.py 抽取的 OpenAI 格式端点。

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-01-18
"""

from typing import Dict, Any, AsyncGenerator
import os
import json
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse, JSONResponse

from akarins_gateway.streaming_constants import STREAMING_HEADERS
from ..normalization import normalize_request_body
from ..proxy import route_request_with_fallback
from ..scid import apply_scid_and_sanitization, wrap_stream_with_writeback, writeback_non_streaming_response

# History Cache Manager for long conversations
from akarins_gateway.ide_compat.history_cache import HistoryCacheManager

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

router = APIRouter()

__all__ = ["router", "convert_sse_to_augment_ndjson"]


# ==================== History Cache Manager ====================
# [NEW 2026-01-24] 历史缓存管理器，解决长对话上下文丢失问题
# - 缓存完整历史（不删除）
# - 智能选择消息发送给后端
# - 控制请求体大小 ≤ 200KB

_history_cache = HistoryCacheManager(
    backend="lru",
    max_cache_size=1000,
    strategy="smart",
    recent_count=10
)

HISTORY_CACHE_MAX_MESSAGES_DEFAULT = int(os.getenv("HISTORY_CACHE_MAX_MESSAGES_DEFAULT", "20"))
HISTORY_CACHE_MAX_MESSAGES_TOOL = int(os.getenv("HISTORY_CACHE_MAX_MESSAGES_TOOL", "40"))

try:
    from akarins_gateway.core.log import log
    log.info("[GATEWAY OPENAI] 历史缓存管理器已初始化", tag="GATEWAY")
except:
    pass


def _summarize_message_roles(messages: Any) -> Dict[str, int]:
    """统计消息角色分布，便于定位 History Cache 选择行为。"""
    summary = {
        "total": 0,
        "system": 0,
        "user": 0,
        "assistant": 0,
        "tool": 0,
        "other": 0,
    }
    if not isinstance(messages, list):
        return summary

    summary["total"] = len(messages)
    for msg in messages:
        if not isinstance(msg, dict):
            summary["other"] += 1
            continue
        role = str(msg.get("role", "")).lower()
        if role in ("system", "user", "assistant", "tool"):
            summary[role] += 1
        else:
            summary["other"] += 1
    return summary


# ==================== OpenAI Chat Completions 端点 ====================

@router.post("/v1/chat/completions")
@router.post("/chat/completions")  # 别名路由，兼容 Base URL 为 /gateway 的客户端
async def chat_completions(
    request: Request,
    token: str = Depends(authenticate_bearer)
):
    """统一聊天完成端点 - 自动路由到最佳后端"""
    log.info(f"Chat request received", tag="GATEWAY")
    
    # 检测客户端类型（用于日志和后续处理）
    try:
        from akarins_gateway.ide_compat import ClientTypeDetector
        client_info = ClientTypeDetector.detect(dict(request.headers))
    except Exception as e:
        log.warning(f"Failed to detect client type: {e}", tag="GATEWAY")
        client_info = None
    
    try:
        raw_body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # DEBUG: Log incoming messages to diagnose tool call issues
    raw_messages = raw_body.get("messages", [])
    log.debug(f" Incoming messages count: {len(raw_messages)}")
    for i, msg in enumerate(raw_messages[-5:]):  # Only log last 5 messages
        if isinstance(msg, dict):
            role = msg.get("role", "unknown")
            has_content = "content" in msg and msg["content"] is not None
            has_tool_calls = "tool_calls" in msg
            tool_call_id = msg.get("tool_call_id", None)
            log.debug(f" Message {i}: role={role}, has_content={has_content}, has_tool_calls={has_tool_calls}, tool_call_id={tool_call_id}")
            if role == "tool":
                log.debug(f" Tool result message: {json.dumps(msg, ensure_ascii=False)[:500]}")
            if role == "assistant" and has_tool_calls:
                log.debug(f" Assistant tool_calls: {json.dumps(msg.get('tool_calls', []), ensure_ascii=False)[:500]}")

    # Normalize request body to standard OpenAI format
    body = normalize_request_body(raw_body)
    headers = dict(request.headers)
    
    # ================================================================
    # [SCID] Step 1: 应用 SCID 架构和消息净化
    # ================================================================
    scid, client_info, state_manager, _ = apply_scid_and_sanitization(
        headers=headers,
        raw_body=raw_body,
        body=body,
    )

    model = body.get("model", "")
    stream = body.get("stream", False)

    # ================================================================
    # [SCID] Step 2: 添加 SCID 到请求头和请求体（供下游使用）
    # ================================================================
    if scid:
        headers["x-ag-conversation-id"] = scid
        # [FIX 2026-01-22] 将SCID添加到请求体中，供antigravity_router使用
        # antigravity_router需要SCID来从权威历史恢复thinking块
        body["_scid"] = scid

    # ================================================================
    # [NEW 2026-01-24] History Cache: 存储完整历史 + 智能选择
    # ================================================================
    if scid:
        messages = body.get("messages", [])
        pre_select_summary = _summarize_message_roles(messages)

        # 工具会话适当放宽历史选择窗口，降低“旧工具结果被选掉”导致的重复调用
        has_tool_context = False
        for _m in messages:
            if not isinstance(_m, dict):
                continue
            _role = _m.get("role")
            if _role == "tool":
                has_tool_context = True
                break
            if _role == "assistant":
                _tcs = _m.get("tool_calls")
                if isinstance(_tcs, list) and len(_tcs) > 0:
                    has_tool_context = True
                    break
            _content = _m.get("content")
            if isinstance(_content, list):
                if any(
                    isinstance(_it, dict) and _it.get("type") in ("tool_use", "tool_result")
                    for _it in _content
                ):
                    has_tool_context = True
                    break

        select_limit = HISTORY_CACHE_MAX_MESSAGES_TOOL if has_tool_context else HISTORY_CACHE_MAX_MESSAGES_DEFAULT
        
        # 存储完整历史（不删除）
        _history_cache.store_full_history(scid, messages)
        
        # 智能选择消息（从完整历史中选择最重要的）
        selected_messages = _history_cache.select_for_backend(
            scid, 
            max_messages=select_limit
        )
        selected_summary = _summarize_message_roles(selected_messages)
        
        # 替换原始消息为精选消息
        body["messages"] = selected_messages
        
        log.info(
            f"[HISTORY CACHE] SCID: {scid[:8]}... - "
            f"存储 {len(messages)} 消息, 选择 {len(selected_messages)} 消息发送给后端 "
            f"(limit={select_limit}, tool_context={has_tool_context}), "
            f"before_roles={pre_select_summary}, after_roles={selected_summary}",
            tag="GATEWAY"
        )

    # ================================================================
    # [NEW 2026-03-04] Tool Semantic Conversion: 隐藏 Claude Code 工具指纹
    # ================================================================
    from akarins_gateway.converters.tool_semantic_converter import apply_tool_semantic_conversion
    from akarins_gateway.core.config import get_tool_semantic_conversion_enabled
    apply_tool_semantic_conversion(body, enabled=get_tool_semantic_conversion_enabled())

    result = await route_request_with_fallback(
        endpoint="/chat/completions",
        method="POST",
        headers=headers,
        body=body,
        model=model,
        stream=stream,
        enable_cross_pool_fallback=client_info.enable_cross_pool_fallback if client_info else None,
    )

    # ================================================================
    # [SCID] Step 3: 构建响应并添加 SCID header
    # ================================================================
    response_headers = {}
    if scid:
        response_headers["X-AG-Conversation-Id"] = scid

    if stream and hasattr(result, "__anext__"):
        # ================================================================
        # [SCID] Step 4a: 流式响应 - 使用包装器在完成时回写
        # ================================================================
        if scid and state_manager and client_info and client_info.needs_sanitization:
            # 包装流式响应，在完成时回写状态
            result = wrap_stream_with_writeback(
                result, scid, state_manager, body.get("messages", [])
            )

        # ================================================================
        # [NEW 2026-03-04] Tool Semantic Reverse: 流式响应反向映射
        # 统一处理所有后端(ZG/Kiro/Public)的流式 tool_calls name 反向转换
        # ================================================================
        if get_tool_semantic_conversion_enabled():
            from akarins_gateway.converters.tool_semantic_converter import reverse_tool_name_in_sse_chunk
            _upstream = result

            async def _reverse_tool_names_stream():
                async for chunk in _upstream:
                    if isinstance(chunk, str):
                        yield reverse_tool_name_in_sse_chunk(chunk, enabled=True)
                    else:
                        yield chunk

            result = _reverse_tool_names_stream()

        return StreamingResponse(
            result,
            media_type="text/event-stream",
            headers={
                **STREAMING_HEADERS,
                **response_headers,
            }
        )

    # ================================================================
    # [SCID] Step 4b: 非流式响应 - 提取签名并回写状态
    # ================================================================
    if scid and state_manager and client_info and client_info.needs_sanitization:
        try:
            writeback_non_streaming_response(
                result, scid, state_manager, body.get("messages", [])
            )
        except Exception as wb_err:
            log.warning(f"[SCID] Non-streaming writeback failed (non-fatal): {wb_err}", tag="GATEWAY")

    # ================================================================
    # [NEW 2026-03-04] Tool Semantic Reverse: 非流式响应反向映射
    # ================================================================
    from akarins_gateway.converters.tool_semantic_converter import reverse_tool_semantic_in_response_body
    reverse_tool_semantic_in_response_body(result, enabled=get_tool_semantic_conversion_enabled())

    return JSONResponse(content=result, headers=response_headers)


# ==================== SSE 到 NDJSON 转换 ====================

async def convert_sse_to_augment_ndjson(sse_stream: AsyncGenerator) -> AsyncGenerator[str, None]:
    """
    将 SSE 格式流转换为 Augment Code 期望的 NDJSON 格式流

    OpenAI SSE 格式: data: {"choices":[{"delta":{"content":"你好"}}]}\n\n
    Augment NDJSON 格式: {"text":"你好"}\n

    Args:
        sse_stream: SSE 格式的异步生成器（可能返回 bytes 或 str）

    Yields:
        Augment NDJSON 格式的字符串（每行一个 {"text": "..."} 对象）
    """
    buffer = ""

    async for chunk in sse_stream:
        if not chunk:
            continue

        # 处理字节类型，转换为字符串
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="ignore")
        elif not isinstance(chunk, str):
            chunk = str(chunk)

        # 将 chunk 添加到缓冲区
        buffer += chunk

        # 按行处理缓冲区（SSE 格式以 \n\n 分隔事件）
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()

            # 跳过空行
            if not line:
                continue

            # 检查是否是 SSE 格式的 data: 行
            if line.startswith("data: "):
                # 提取 JSON 数据
                json_str = line[6:].strip()  # 移除 "data: " 前缀

                # 跳过 [DONE] 标记
                if json_str == "[DONE]":
                    continue

                # 验证是否是有效的 JSON
                try:
                    # 解析 OpenAI 格式的 JSON
                    json_obj = json.loads(json_str)

                    # 提取 content 字段转换为 Augment 格式
                    # OpenAI: {"choices":[{"delta":{"content":"xxx"}}]}
                    # Augment: {"text":"xxx"}
                    if "choices" in json_obj and len(json_obj["choices"]) > 0:
                        choice = json_obj["choices"][0]

                        # 处理流式响应的 delta
                        if "delta" in choice:
                            delta = choice["delta"]

                            # NOTE:
                            # When upstream chooses to call tools, OpenAI streaming returns `delta.tool_calls`
                            # (often with no `delta.content`). If we drop these deltas, the VSCode client will
                            # look like it "ended immediately" when a tool is attempted.
                            tool_calls = delta.get("tool_calls") if isinstance(delta, dict) else None
                            if isinstance(tool_calls, list) and tool_calls:
                                try:
                                    log.warning(
                                        f"[TOOL CALL] Upstream returned tool_calls (count={len(tool_calls)}), "
                                        f"first={json.dumps(tool_calls[0], ensure_ascii=False)[:500]}",
                                        tag="GATEWAY",
                                    )
                                except Exception:
                                    log.warning("[TOOL CALL] Upstream returned tool_calls (unable to dump)", tag="GATEWAY")

                                # Emit a visible message so the user isn't left with an empty response.
                                augment_obj = {
                                    "text": (
                                        "\n[Gateway] 上游模型触发了工具调用(tool_calls)，但当前网关尚未实现将 tool_calls "
                                        "转换/执行为 Augment 工具链的逻辑，因此工具步骤无法继续。"
                                    )
                                }
                                yield json.dumps(augment_obj, separators=(',', ':'), ensure_ascii=False) + "\n"

                            if "content" in delta and delta["content"] is not None:
                                augment_obj = {"text": delta["content"]}
                                yield json.dumps(augment_obj, separators=(',', ':'), ensure_ascii=False) + "\n"

                        # 处理完整响应的 message
                        elif "message" in choice:
                            message = choice["message"]
                            if "content" in message and message["content"] is not None:
                                augment_obj = {"text": message["content"]}
                                yield json.dumps(augment_obj, separators=(',', ':'), ensure_ascii=False) + "\n"

                        # 处理 finish_reason
                        if "finish_reason" in choice and choice["finish_reason"] is not None:
                            # Augment 不需要 finish_reason，跳过
                            if choice["finish_reason"] in ("tool_calls", "function_call"):
                                log.warning(f"[TOOL CALL] finish_reason={choice['finish_reason']}", tag="GATEWAY")
                            continue

                except json.JSONDecodeError:
                    # 如果不是有效的 JSON，记录警告但继续处理
                    log.warning(f"Invalid JSON in SSE stream: {json_str[:100]}")
                    continue
            elif line.startswith(":"):
                # SSE 注释行，跳过
                continue
            elif line.startswith("event:") or line.startswith("id:") or line.startswith("retry:"):
                # 其他 SSE 字段，跳过
                continue
