"""
Gateway Augment 端点

包含 Augment/Bugment 协议的 API 端点。

从 unified_gateway_router.py 抽取的 Augment 端点。

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-01-18
"""

from typing import Dict, Any
import os
import json
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse, JSONResponse

from akarins_gateway.streaming_constants import STREAMING_HEADERS
from .state import bugment_conversation_state_put, bugment_conversation_state_get
from .bridge import apply_bugment_state_to_raw_body, convert_bugment_to_gateway
from .nodes_bridge import (
    stream_openai_with_nodes_bridge,
    prepend_bugment_guidance_system_message,
    build_openai_messages_from_bugment,
)
from ..normalization import normalize_request_body
from ..scid import apply_scid_and_sanitization, wrap_stream_with_writeback

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
    # CRITICAL: auth module failed — all augment endpoints bypass authentication!
    log.critical("[SECURITY] core.auth import failed! All augment endpoints use dummy auth (no password check).")
    async def authenticate_bearer():
        return "dummy"
    async def authenticate_bearer_allow_local_dummy():
        return "dummy"

# 延迟导入 tool_loop (Loop 1.7 实现)
try:
    from ..tool_loop import stream_openai_with_tool_loop
except ImportError:
    stream_openai_with_tool_loop = None

router = APIRouter()

__all__ = [
    "router",
    "create_augment_router"  # [NEW 2026-01-24] 添加工厂函数
]


def create_augment_router() -> APIRouter:
    """
    创建 Augment 兼容路由器（工厂函数）
    
    用于适配器模式，确保与旧网关接口兼容。
    
    Returns:
        配置好的 APIRouter 实例（无前缀）
    
    作者: 浮浮酱 (Claude Sonnet 4.5)
    创建日期: 2026-01-24
    """
    return router


# 配置开关
BUGMENT_TOOL_RESULT_SHORTCIRCUIT_ENABLED = False  # 默认禁用
AUGMENT_COMPAT_AVAILABLE = True  # 使用 nodes_bridge
BUGMENT_DEFAULT_MODEL = os.getenv("BUGMENT_DEFAULT_MODEL", "gpt-4.1").strip()
# Phase 4: Bridge 集成 feature flag（可快速回滚）
USE_AUGMENT_BRIDGE = os.getenv("USE_AUGMENT_BRIDGE", "true").lower() == "true"


# ==================== Bugment 会话管理端点 ====================

@router.post("/bugment/conversation/set-model")
@router.post("/v1/bugment/conversation/set-model")
async def bugment_conversation_set_model(
    request: Request,
    token: str = Depends(authenticate_bearer_allow_local_dummy)
):
    """
    Bugment helper: proactively bind the currently selected model to a conversation.

    Why:
    - Before the first user message is sent, some internal requests (e.g. prompt enhancer)
      may be issued with `model: ""`.
    - We intentionally removed the hardcoded `gpt-4` fallback to avoid conflicts with the UI model.
    - This endpoint lets the extension sync the selected model immediately on model change,
      so follow-up requests with missing model can use conversation fallback deterministically.
    """
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload: expected JSON object")

    conversation_id = payload.get("conversation_id") or payload.get("conversationId")
    model = payload.get("model")
    if isinstance(model, str):
        model = model.strip()

    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise HTTPException(status_code=400, detail="Missing required field: conversation_id")
    if not isinstance(model, str) or not model:
        raise HTTPException(status_code=400, detail="Missing required field: model")

    bugment_conversation_state_put(conversation_id.strip(), model=model)
    log.info(
        f"Bugment conversation model updated: conversation_id={conversation_id.strip()} model={model}",
        tag="GATEWAY",
    )
    return {"success": True}


# ==================== Augment Chat Stream 端点 ====================

@router.post("/chat-stream")
async def chat_stream(
    request: Request,
    token: str = Depends(authenticate_bearer_allow_local_dummy)
):
    """
    Augment Code 兼容路由：统一聊天流式端点（NDJSON 格式）

    - 路径：/gateway/chat-stream（因为 router 前缀为 /gateway）
    - 功能：等价于 /chat/completions 且强制开启 stream 模式
    - 格式：返回 NDJSON 格式（每行一个 JSON 对象），而非 SSE 格式
    """
    log.info(f"Chat stream request received (Augment NDJSON format)", tag="GATEWAY")
    
    # 检测客户端类型（用于日志和后续处理）
    try:
        from akarins_gateway.ide_compat import ClientTypeDetector
        client_info = ClientTypeDetector.detect(dict(request.headers))
    except Exception as e:
        log.warning(f"Failed to detect client type: {e}", tag="GATEWAY")
        client_info = None

    # 读取原始字节
    raw_bytes = await request.body()
    log.debug(f"Raw bytes length: {len(raw_bytes)}", tag="GATEWAY")

    # 解析 JSON
    try:
        raw_body = json.loads(raw_bytes.decode('utf-8'))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # ------------------------------------------------------------------
    # Conversation-scoped state (model + chat history)
    # USE_AUGMENT_BRIDGE 时由 bridge 内部处理，否则委托 apply_bugment_state_to_raw_body
    # ------------------------------------------------------------------
    if not USE_AUGMENT_BRIDGE:
        try:
            apply_bugment_state_to_raw_body(raw_body)
        except Exception as e:
            log.warning(f"Failed to apply Bugment conversation state: {e}", tag="GATEWAY")

    # ------------------------------------------------------------------
    # Optional short-circuit: tool_result continuation (debug/mock)
    # ------------------------------------------------------------------
    if BUGMENT_TOOL_RESULT_SHORTCIRCUIT_ENABLED:
        try:
            nodes = raw_body.get("nodes") if isinstance(raw_body, dict) else None
            message = raw_body.get("message") if isinstance(raw_body, dict) else None
            if isinstance(nodes, list) and any(
                isinstance(n, dict)
                and n.get("type") == 1
                and isinstance(n.get("tool_result_node"), dict)
                for n in nodes
            ):
                if message is None or (isinstance(message, str) and message.strip() == ""):
                    tool_contents = []
                    for n in nodes:
                        trn = n.get("tool_result_node") if isinstance(n, dict) else None
                        if not isinstance(trn, dict):
                            continue
                        content = trn.get("content")
                        if isinstance(content, str) and content.strip():
                            tool_contents.append(content)

                    tool_text = tool_contents[0] if tool_contents else ""
                    if isinstance(tool_text, str) and tool_text.strip():
                        try:
                            parsed = json.loads(tool_text)
                            if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
                                tool_text = parsed["text"]
                        except Exception:
                            pass

                        async def _tool_result_shortcircuit():
                            yield json.dumps(
                                {"text": tool_text},
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ) + "\n"

                        log.info(
                            f"[TOOL_RESULT] short-circuit chat-stream (len={len(tool_text)})",
                            tag="GATEWAY",
                        )
                        return StreamingResponse(
                            _tool_result_shortcircuit(),
                            media_type="application/x-ndjson",
                        )
        except Exception as e:
            log.warning(f"[TOOL_RESULT] short-circuit failed: {e}", tag="GATEWAY")

    # ------------------------------------------------------------------
    # Short-circuit: Augment internal message-analysis/memory requests
    # ------------------------------------------------------------------
    try:
        msg = raw_body.get("message") if isinstance(raw_body, dict) else None
        mode = raw_body.get("mode") if isinstance(raw_body, dict) else None
        msg_str = msg if isinstance(msg, str) else ""
        mode_str = mode.strip().upper() if isinstance(mode, str) else ""
        is_message_analysis = (
            mode_str == "CHAT"
            and (
                ("ENTER MESSAGE ANALYSIS MODE" in msg_str)
                or ("ONLY JSON" in msg_str and "worthRemembering" in msg_str)
                or ("worthRemembering" in msg_str and "Return JSON" in msg_str)
            )
        )
        if is_message_analysis:
            analysis_result = {"explanation": "", "worthRemembering": False, "content": ""}
            json_text = json.dumps(analysis_result, ensure_ascii=False, separators=(",", ":"))
            ndjson_line = json.dumps({"text": json_text}, ensure_ascii=False, separators=(",", ":")) + "\n"
            return StreamingResponse(
                iter([ndjson_line]),
                media_type="application/x-ndjson",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )
    except Exception as e:
        log.warning(f"Failed to short-circuit message-analysis request: {e}", tag="GATEWAY")

    # 为下游后端（Antigravity / Kiro / AnyRouter / Copilot）统一使用网关自身的 API 密码，
    # 避免将 Augment 本地 Bearer token 直接转发到后端导致 401（尤其是 Kiro Gateway）。
    try:
        from akarins_gateway.core.config import get_api_password
        password = get_api_password()
    except ImportError:
        password = "dummy"
    
    headers = dict(request.headers)
    # 统一覆盖为网关 API 密码，后端只关心这一层认证，不需要感知 Augment 自己的 token
    headers["authorization"] = f"Bearer {password}"

    # ------------------------------------------------------------------
    # Phase 4: Bridge 集成 vs 旧路径
    # ------------------------------------------------------------------
    conversation_id = raw_body.get("conversation_id") if isinstance(raw_body, dict) else None

    if USE_AUGMENT_BRIDGE:
        # Bridge 路径：convert_bugment_to_gateway 输出完整 body + messages
        try:
            bridge_output = convert_bugment_to_gateway(raw_body, headers)
            body = bridge_output.body
            messages_for_bridge = bridge_output.messages
            model = bridge_output.model
        except Exception as e:
            log.error(f"[BRIDGE] convert_bugment_to_gateway failed: {e}", tag="GATEWAY")
            raise HTTPException(status_code=500, detail=f"Bridge conversion failed: {e}")

        # model 校验（Bridge 已做 fallback，此处仅兜底）
        if not model or (isinstance(model, str) and not model.strip()):
            if BUGMENT_DEFAULT_MODEL:
                model = BUGMENT_DEFAULT_MODEL
                body["model"] = model
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Missing required field: model (Bugment should send the currently selected model).",
                )
    else:
        # 旧路径：手动构建消息 + normalize
        messages_for_bridge = prepend_bugment_guidance_system_message(
            raw_body,
            build_openai_messages_from_bugment(raw_body),
        )
        if not messages_for_bridge:
            messages_for_bridge = raw_body.get("messages", []) if isinstance(raw_body, dict) else []
        body = normalize_request_body(raw_body, preserve_extra_fields=False)

        # model fallback（旧路径）
        model = body.get("model")
        if model is None or model == "" or (isinstance(model, str) and model.strip() == ""):
            state = bugment_conversation_state_get(conversation_id)
            fallback_model = state.get("model")
            if isinstance(fallback_model, str) and fallback_model.strip():
                model = fallback_model.strip()
                body["model"] = model
                log.warning(f"Model was None/empty; using conversation model fallback: {model}", tag="GATEWAY")
            elif BUGMENT_DEFAULT_MODEL:
                model = BUGMENT_DEFAULT_MODEL
                body["model"] = model
                bugment_conversation_state_put(conversation_id, model=model)
                log.warning(f"Model missing; using default model fallback: {model}", tag="GATEWAY")
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Missing required field: model (Bugment should send the currently selected model).",
                )

    # 应用 SCID 架构和消息净化
    scid, client_info, state_manager, messages_for_bridge = apply_scid_and_sanitization(
        headers=headers,
        raw_body=raw_body,
        body=body,
        messages=messages_for_bridge,
    )

    raw_mode = raw_body.get("mode") if isinstance(raw_body, dict) else None
    mode_str = raw_mode.strip().upper() if isinstance(raw_mode, str) else ""
    if mode_str == "CHAT":
        for thinking_key in ("thinking", "thinking_budget", "thinking_level", "thinking_config"):
            if thinking_key in body:
                body.pop(thinking_key, None)

    if scid:
        headers["x-ag-conversation-id"] = scid
        body["_scid"] = scid

    # Detect Augment/Bugment requests
    lower_header_keys = {k.lower() for k in headers.keys()}
    is_augment_request = (
        ("x-augment-client" in lower_header_keys)
        or ("x-bugment-client" in lower_header_keys)
        or ("x-augment-request" in lower_header_keys)
        or ("x-bugment-request" in lower_header_keys)
        or ("x-signature-version" in lower_header_keys)
        or ("x-signature-vector" in lower_header_keys)
        or ("x-signature-signature" in lower_header_keys)
    )
    if is_augment_request:
        if "x-augment-client" not in lower_header_keys and "x-bugment-client" not in lower_header_keys:
            headers.setdefault("x-augment-client", "augment")

        # Bugment uses CHAT-mode requests for internal JSON parsing workflows (prompt enhancer, message analysis).
        # Thinking output (<think>/signature-carrying blocks) can break those client-side parsers.
        # Only disable thinking/signature-cache for CHAT-mode; keep AGENT-mode thinking intact.
        if mode_str == "CHAT":
            headers.setdefault("x-disable-thinking-signature", "1")

    try:
        response_headers = {}
        if scid:
            response_headers["X-AG-Conversation-Id"] = scid

        # Prefer Augment-compatible client-side tool loop when available.
        if AUGMENT_COMPAT_AVAILABLE:
            ndjson_stream = stream_openai_with_nodes_bridge(
                headers=headers,
                raw_body=raw_body,
                model=model,
                messages_override=messages_for_bridge,
                scid=scid,
                state_manager=state_manager,
            )
        elif stream_openai_with_tool_loop is not None:
            # Legacy fallback (server-side tool loop; client will not see TOOL_USE nodes)
            ndjson_stream = stream_openai_with_tool_loop(headers=headers, body=body, model=model)
        else:
            raise HTTPException(status_code=500, detail="No streaming handler available")
        
        # 如果使用 stream_openai_with_nodes_bridge 且需要回写，包装流式响应
        if AUGMENT_COMPAT_AVAILABLE and scid and state_manager and client_info and client_info.needs_sanitization:
            ndjson_stream = wrap_stream_with_writeback(
                ndjson_stream, scid, state_manager, messages_for_bridge
            )

        return StreamingResponse(
            ndjson_stream,
            media_type="application/x-ndjson",
            headers=STREAMING_HEADERS,
        )
    except HTTPException as e:
        error_obj = {
            "error": {
                "message": str(e.detail) if e.detail else "Request failed",
                "type": "api_error",
                "code": e.status_code
            }
        }
        error_ndjson = json.dumps(error_obj, separators=(',', ':')) + "\n"
        return StreamingResponse(
            iter([error_ndjson]),
            media_type="application/x-ndjson",
            status_code=e.status_code,
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
    except Exception as e:
        log.error(f"Unexpected error in chat_stream: {e}", tag="GATEWAY")
        error_obj = {
            "error": {
                "message": str(e),
                "type": "internal_error",
                "code": 500
            }
        }
        error_ndjson = json.dumps(error_obj, separators=(',', ':')) + "\n"
        return StreamingResponse(
            iter([error_ndjson]),
            media_type="application/x-ndjson",
            status_code=500,
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )


# ==================== Augment Agent Tools 端点 ====================

@router.post("/agents/check-tool-safety")
async def agents_check_tool_safety(
    request: Request,
    token: str = Depends(authenticate_bearer_allow_local_dummy),
):
    """
    Minimal compatibility endpoint for Augment tool safety checks.

    Expected request:
      { "tool_id": <int>, "tool_input_json": "<json string>" }

    Expected response:
      { "is_safe": true/false }
    """
    try:
        await request.json()
    except Exception:
        # Even if payload parsing fails, default to safe to avoid hard failures in the client.
        return JSONResponse(content={"is_safe": True})

    return JSONResponse(content={"is_safe": True})


@router.post("/agents/run-remote-tool")
async def agents_run_remote_tool(
    request: Request,
    token: str = Depends(authenticate_bearer_allow_local_dummy),
):
    """
    Mock compatibility endpoint for Augment remote tool execution.

    Returns a mock SUCCESS response (status=1) to test if the Augment client
    can be tricked into thinking the tool executed successfully.

    Expected response keys (as used by the extension):
      - tool_output: The tool's output content
      - tool_result_message: Human-readable result message
      - status: 1 = success, other values = error
    """
    try:
        payload = await request.json()
    except Exception as e:
        return JSONResponse(
            content={
                "tool_output": "",
                "tool_result_message": f"Invalid JSON: {e}",
                "status": 0,  # Error status
            },
            status_code=400,
        )

    tool_name = payload.get("tool_name") or payload.get("toolName") or "unknown"
    tool_id = payload.get("tool_id") if "tool_id" in payload else payload.get("toolId")
    tool_input_json = payload.get("tool_input_json") or payload.get("toolInputJson") or ""

    # Parse tool input for logging
    tool_input = {}
    if isinstance(tool_input_json, str) and tool_input_json.strip():
        try:
            tool_input = json.loads(tool_input_json)
        except Exception:
            pass

    log.info(
        f"[AGENTS] run-remote-tool MOCK SUCCESS - "
        f"tool_name={tool_name}, tool_id={tool_id}, input_keys={list(tool_input.keys()) if isinstance(tool_input, dict) else 'N/A'}",
        tag="GATEWAY",
    )

    # Generate mock tool output based on tool name
    mock_output = _generate_mock_tool_output(tool_name, tool_input)

    return JSONResponse(
        content={
            "tool_output": mock_output,
            "tool_result_message": f"[MOCK] Tool '{tool_name}' executed successfully (Gateway mock response)",
            "status": 1,  # SUCCESS status!
        }
    )


def _generate_mock_tool_output(tool_name: str, tool_input: dict) -> str:
    """
    Generate mock tool output based on tool name and input.
    This helps test the protocol flow without real tool execution.
    """
    tool_name_lower = tool_name.lower()

    # File reading tools
    if "read" in tool_name_lower or "file" in tool_name_lower:
        path = tool_input.get("path") or tool_input.get("file_path") or "unknown_file"
        return f"[MOCK] Content of '{path}':\n# This is mock file content\n# Real file reading is not implemented in Gateway"

    # Search/grep tools
    if "search" in tool_name_lower or "grep" in tool_name_lower:
        pattern = tool_input.get("pattern") or tool_input.get("query") or "pattern"
        return f"[MOCK] Search results for '{pattern}':\nNo matches found (mock response)"

    # Shell/command tools
    if "shell" in tool_name_lower or "command" in tool_name_lower or "bash" in tool_name_lower:
        command = tool_input.get("command") or tool_input.get("cmd") or "unknown command"
        return f"[MOCK] Command output for '{command}':\n$ {command}\n(mock: command not actually executed)"

    # List/directory tools
    if "list" in tool_name_lower or "dir" in tool_name_lower:
        path = tool_input.get("path") or tool_input.get("directory") or "."
        return f"[MOCK] Directory listing for '{path}':\nfile1.txt\nfile2.py\nsubdir/\n(mock response)"

    # Write tools
    if "write" in tool_name_lower or "create" in tool_name_lower:
        path = tool_input.get("path") or tool_input.get("file_path") or "unknown_file"
        return f"[MOCK] Successfully wrote to '{path}' (mock - no actual file written)"

    # Default mock output
    return f"[MOCK] Tool '{tool_name}' executed with input: {json.dumps(tool_input, ensure_ascii=False)[:500]}"


# ==================== Augment Code 兼容端点 ====================

@router.post("/get-models")
@router.post("/v1/get-models")
async def augment_get_models_for_bugment(
    request: Request,
    token: str = Depends(authenticate_bearer_allow_local_dummy)
):
    """Bugment/VSCode: returns BackGetModelsResult (POST /get-models) without /gateway prefix."""
    log.debug(f"Bugment get-models request received from {request.url.path}", tag="GATEWAY")
    # 延迟导入避免循环依赖
    try:
        from ..endpoints.models import _build_bugment_get_models_result
        return _build_bugment_get_models_result()
    except ImportError:
        # 如果导入失败，返回默认结果（默认 claude-sonnet-4.5）
        return {
            "default_model": BUGMENT_DEFAULT_MODEL or "claude-sonnet-4.5",
            "models": [
                {
                    "name": "claude-sonnet-4.5",
                    "suggested_prefix_char_count": 8000,
                    "suggested_suffix_char_count": 2000,
                    "completion_timeout_ms": 120_000,
                    "internal_name": "claude-sonnet-4.5",
                },
            ],
            "feature_flags": {
                "enablePromptEnhancer": True,
                "enableAgentAutoMode": True,
            },
            "user_tier": "COMMUNITY_TIER",
            "user": {"id": "local"},
        }


@router.get("/usage/api/get-models")
async def augment_list_models(request: Request):
    """Augment Code 兼容路由：获取模型列表（不带 /gateway 前缀）- 返回对象数组"""
    # 延迟导入避免循环依赖
    try:
        from ..endpoints.models import list_models_for_augment
        return await list_models_for_augment(request)
    except ImportError:
        # 如果导入失败，返回默认模型列表
        return [
            {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
            {"id": "claude-opus-4-20250514", "name": "Claude Opus 4"},
            {"id": "gpt-4", "name": "GPT-4"},
        ]


@router.get("/usage/api/balance")
async def get_balance(request: Request):
    """Augment Code 兼容路由：获取账户余额信息"""
    log.debug(f"Balance request received from {request.url.path}", tag="GATEWAY")

    # 尝试从凭证管理器获取用户信息（如果有的话）
    user_email = "用户"
    try:
        from web import get_credential_manager
        cred_mgr = get_credential_manager()
        if cred_mgr:
            # 获取当前使用的凭证信息
            cred_result = await cred_mgr.get_valid_credential()
            if cred_result:
                _, credential_data = cred_result
                # 尝试从凭证数据中提取用户信息
                user_email = credential_data.get("email") or credential_data.get("user_email") or "用户"
    except Exception as e:
        log.warning(f"Failed to get credential info for balance: {e}")

    # 返回余额信息（模拟数据，实际应该从数据库或配置中读取）
    balance_data = {
        "success": True,
        "data": {
            "balance": 100.00,  # 默认余额
            "name": user_email.split("@")[0] if "@" in user_email else user_email,
            "plan_name": "标准套餐",
            "end_date": "2025-12-31"
        }
    }

    log.debug(f"Returning balance info: {balance_data}", tag="GATEWAY")
    return balance_data


@router.get("/usage/api/getLoginToken")
async def get_login_token(request: Request):
    """Augment Code 兼容路由：获取登录令牌"""
    import secrets
    import time as time_module

    log.debug(f"Login token request received from {request.url.path}", tag="GATEWAY")

    # 生成一个简单的令牌
    token = secrets.token_urlsafe(32)
    timestamp = int(time_module.time())

    # 返回令牌信息
    token_data = {
        "success": True,
        "data": {
            "token": token,
            "expires_in": 3600,  # 1小时过期
            "token_type": "Bearer",
            "timestamp": timestamp
        }
    }

    log.debug(f"Returning login token: {token_data}", tag="GATEWAY")
    return token_data
