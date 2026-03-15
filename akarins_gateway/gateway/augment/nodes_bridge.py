"""
Gateway Augment Nodes Bridge

处理 OpenAI 响应到 Augment NDJSON 节点的转换。

从 unified_gateway_router.py 抽取的 Nodes Bridge 逻辑。

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-01-18
"""

from typing import Dict, Any, List, AsyncGenerator, Optional
import json
import re
import time

from .state import bugment_tool_state_put, bugment_tool_state_get

# 延迟导入 log，避免循环依赖
try:
    from akarins_gateway.core.log import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

# 延迟导入代理函数
try:
    from ..proxy import route_request_with_fallback
except ImportError:
    route_request_with_fallback = None

# 延迟导入工具转换函数（Augment tool_definitions → OpenAI tools）
# [FIX 2026-01-30] 从 augment_compat.tools_bridge 导入，而非 tool_converter（后者无此函数）
try:
    from akarins_gateway.augment_compat.tools_bridge import (
        parse_tool_definitions_from_request,
        convert_tools_to_openai,
    )
except ImportError:
    parse_tool_definitions_from_request = None
    convert_tools_to_openai = None

# 延迟导入 Kiro Gateway 检测和配置
try:
    from akarins_gateway.gateway.config_loader import is_backend_capable
    from akarins_gateway.gateway.config import BACKENDS
except ImportError:
    is_backend_capable = None
    BACKENDS = {}

# 延迟导入上下文截断（可选）
try:
    from akarins_gateway.context_truncation import (
        estimate_messages_tokens,
        truncate_context_for_api,
    )
    CONTEXT_TRUNCATION_AVAILABLE = True
except ImportError:
    CONTEXT_TRUNCATION_AVAILABLE = False
    estimate_messages_tokens = None
    truncate_context_for_api = None

__all__ = [
    "stream_openai_with_nodes_bridge",
    "augment_chat_history_to_messages",
    "extract_tool_result_nodes",
    "build_openai_messages_from_bugment",
    "prepend_bugment_guidance_system_message",
]


# ==================== 辅助函数 ====================

def augment_chat_history_to_messages(chat_history: Any) -> List[Dict[str, Any]]:
    """
    将 Augment 聊天历史转换为 OpenAI 消息格式

    Args:
        chat_history: Augment 聊天历史

    Returns:
        OpenAI 格式的消息列表
    """
    messages: List[Dict[str, Any]] = []
    if not isinstance(chat_history, list):
        return messages

    for item in chat_history:
        if not isinstance(item, dict):
            continue

        # Bugment log format: { request_message, response_text, ... }
        request_message = item.get("request_message") or item.get("user") or item.get("requestMessage")
        response_text = item.get("response_text") or item.get("assistant") or item.get("responseText")

        if isinstance(request_message, str) and request_message.strip():
            messages.append({"role": "user", "content": request_message})
        if isinstance(response_text, str) and response_text.strip():
            messages.append({"role": "assistant", "content": response_text})

        # Alternate Augment format: { role, content }
        role = item.get("role")
        content = item.get("content")
        if isinstance(role, str) and isinstance(content, str) and role in ("user", "assistant", "system") and content.strip():
            messages.append({"role": role, "content": content})

    return messages


def extract_tool_result_nodes(nodes: Any) -> List[Dict[str, Any]]:
    """
    从节点列表中提取工具结果

    Args:
        nodes: Augment 节点列表

    Returns:
        工具结果列表
    """
    if not isinstance(nodes, list):
        return []
    results: List[Dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if n.get("type") == 1 and isinstance(n.get("tool_result_node"), dict):
            results.append(n["tool_result_node"])
    return results


def extract_tool_result_nodes_from_history(
    chat_history: Any, *, latest_only: bool = False
) -> List[Dict[str, Any]]:
    """
    Extract tool_result nodes from chat_history entries.
    """
    if not isinstance(chat_history, list):
        return []
    results: List[Dict[str, Any]] = []
    items = reversed(chat_history) if latest_only else chat_history
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("request_nodes", "response_nodes", "nodes"):
            nodes = item.get(key)
            if not isinstance(nodes, list):
                continue
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                if n.get("type") == 1 and isinstance(n.get("tool_result_node"), dict):
                    results.append(n["tool_result_node"])
            if latest_only and results:
                return list(reversed(results))
    return results


def build_openai_messages_from_bugment(raw_body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Convert Bugment's request (message/chat_history/nodes) into OpenAI-compatible messages.
    Supports TOOL_RESULT continuation by replaying the original assistant tool_calls (from state)
    and appending tool messages.

    Args:
        raw_body: Bugment 请求体

    Returns:
        OpenAI 格式的消息列表
    """
    messages: List[Dict[str, Any]] = []
    conversation_id = raw_body.get("conversation_id") if isinstance(raw_body, dict) else None

    messages.extend(augment_chat_history_to_messages(raw_body.get("chat_history")))

    tool_results = extract_tool_result_nodes(raw_body.get("nodes"))
    history_tool_results = extract_tool_result_nodes_from_history(
        raw_body.get("chat_history"), latest_only=True
    )
    if history_tool_results:
        if tool_results:
            seen_ids = {
                tr.get("tool_use_id")
                for tr in tool_results
                if isinstance(tr, dict) and isinstance(tr.get("tool_use_id"), str)
            }
            for tr in history_tool_results:
                tool_use_id = tr.get("tool_use_id") if isinstance(tr, dict) else None
                if isinstance(tool_use_id, str) and tool_use_id in seen_ids:
                    continue
                tool_results.append(tr)
                if isinstance(tool_use_id, str):
                    seen_ids.add(tool_use_id)
        else:
            tool_results = history_tool_results
    if tool_results:
        assistant_tool_calls: List[Dict[str, Any]] = []
        tool_messages: List[Dict[str, Any]] = []
        fallback_user_notes: List[str] = []

        for tr in tool_results:
            tool_use_id = tr.get("tool_use_id")
            content = tr.get("content")
            is_error = tr.get("is_error")

            if not isinstance(tool_use_id, str) or not tool_use_id.strip():
                continue
            if isinstance(content, str):
                text = content
            elif content is None:
                text = ""
            else:
                try:
                    text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    text = str(content)

            state = bugment_tool_state_get(conversation_id, tool_use_id)
            if isinstance(state, dict) and isinstance(state.get("tool_name"), str):
                assistant_tool_calls.append(
                    {
                        "id": tool_use_id,
                        "type": "function",
                        "function": {"name": state["tool_name"], "arguments": state.get("arguments_json") or "{}"},
                    }
                )
                tool_message = {"role": "tool", "tool_call_id": tool_use_id, "content": text}
                if isinstance(is_error, bool):
                    tool_message["is_error"] = is_error
                tool_messages.append(tool_message)
            else:
                note = f"[Bugment] Tool result received but missing tool_call state: tool_use_id={tool_use_id}"
                if is_error:
                    note += " (is_error=true)"
                fallback_user_notes.append(note + "\n" + text)

        if assistant_tool_calls:
            messages.append({"role": "assistant", "content": "", "tool_calls": assistant_tool_calls})
            messages.extend(tool_messages)

        for note in fallback_user_notes:
            messages.append({"role": "user", "content": note})

    # Current user message (may be empty on tool_result continuations)
    current_message = raw_body.get("message")
    if isinstance(current_message, str) and current_message.strip():
        messages.append({"role": "user", "content": current_message})

    if not messages:
        messages = [{"role": "user", "content": raw_body.get("message") or "Hello"}]

    return messages


def prepend_bugment_guidance_system_message(raw_body: Dict[str, Any], messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Bugment/Augment sends guidance via out-of-band fields like:
    - user_guidelines / workspace_guidelines
    - rules (list)
    - agent_memories
    - persona_type

    Upstream OpenAI-compatible backends will ignore these unless we inject them into a system message.
    This is critical for preserving "agent" behavior and to avoid the model hallucinating local files
    like `.augment/` or `User Guidelines.md` inside the workspace.

    Args:
        raw_body: Bugment 请求体
        messages: 现有消息列表

    Returns:
        带有系统消息的消息列表
    """
    if not isinstance(raw_body, dict):
        return messages

    guidance_parts: List[str] = []

    # Stable prelude: keep it short to avoid fighting user-provided prompts.
    guidance_parts.append(
        "\n".join(
            [
                "# Runtime",
                "You are running inside a VSCode agent environment (Bugment/Augment-like).",
                "You can call provided tools to read/write workspace files and perform codebase retrieval.",
                "Do not assume there is a `.augment/` directory or `User Guidelines.md` file in the workspace unless you can list it.",
            ]
        )
    )

    ug = raw_body.get("user_guidelines")
    if isinstance(ug, str) and ug.strip():
        guidance_parts.append(f"# User Guidelines\n{ug.strip()}")

    wg = raw_body.get("workspace_guidelines")
    if isinstance(wg, str) and wg.strip():
        guidance_parts.append(f"# Workspace Guidelines\n{wg.strip()}")

    am = raw_body.get("agent_memories")
    if isinstance(am, str) and am.strip():
        guidance_parts.append(f"# Agent Memories\n{am.strip()}")

    rules = raw_body.get("rules")
    if isinstance(rules, list) and rules:
        guidance_parts.append(f"# Rules\n{json.dumps(rules, ensure_ascii=False)}")

    persona = raw_body.get("persona_type")
    if persona is not None and str(persona).strip():
        guidance_parts.append(f"# Persona Type\n{persona}")

    if not guidance_parts:
        return messages

    system_text = "\n\n".join(guidance_parts).strip()
    if not system_text:
        return messages

    return [{"role": "system", "content": system_text}] + list(messages)


# ==================== Kiro Compact 兼容层 ====================


def _convert_thinking_to_text(content: Any) -> tuple[Any, bool]:
    """
    将 thinking 块转换为普通文本以保留上下文，而非删除。
    
    Sequential Thinking MCP 等工具将推理结果放入 <think> 块，
    若直接删除会导致模型在数轮工具调用后丢失上下文、重复检索已知信息。
    
    Returns:
        (converted_content, had_thinking) - 转换后的内容及是否包含过 thinking
    """
    had_thinking = False
    if isinstance(content, str):
        # 提取 <think>/</think>、<thinking>、<reasoning> 等块并转为 [Context] 前缀文本
        # Sequential Thinking MCP、Claude reasoning 等将推理结果放入这些块
        def _replacer(m: re.Match[str]) -> str:
            nonlocal had_thinking
            had_thinking = True
            inner = m.group(1).strip()
            if not inner:
                return ""
            return f"\n[Context from reasoning]\n{inner}\n[/Context]\n"
        # 匹配 <think>、<thinking>、<reasoning>、<think> 等
        converted = re.sub(
            r'<(?:think(?:ing)?|reasoning|redacted_reasoning)>(.*?)</(?:think(?:ing)?|reasoning|redacted_reasoning)>',
            _replacer,
            content,
            flags=re.DOTALL | re.IGNORECASE
        )
        return converted.strip(), had_thinking
    if isinstance(content, list):
        # OpenAI parts 格式：提取 type=thinking 的 text，转为普通 text part；text part 内嵌 <think> 也转换
        new_parts: List[Dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                new_parts.append(part)
                continue
            ptype = part.get("type")
            if ptype in ("thinking", "redacted_thinking"):
                had_thinking = True
                # Anthropic 可能用 "thinking" 或 "thought" 存放内容，OpenAI 用 "text"
                text = part.get("text") or part.get("thinking") or part.get("thought") or ""
                if isinstance(text, str) and text.strip():
                    new_parts.append({"type": "text", "text": f"[Context from reasoning]\n{text.strip()}\n[/Context]"})
            elif ptype == "text":
                text = part.get("text", "")
                if isinstance(text, str):
                    conv_text, part_had = _convert_thinking_to_text(text)
                    if part_had:
                        had_thinking = True
                        new_parts.append({"type": "text", "text": conv_text})
                    else:
                        new_parts.append(part)
                else:
                    new_parts.append(part)
            else:
                new_parts.append(part)
        return new_parts, had_thinking
    # content 为 None、数字等非预期类型时透传，避免下游报错
    return content, False


def _prepare_kiro_compact_body(openai_body: Dict[str, Any]) -> Dict[str, Any]:
    """
    为 Kiro Gateway 准备"紧凑版" continuation 请求体
    
    核心策略（2026-01-31 修复上下文丢失）：
    1. 保留 _scid：Antigravity 需要从 body 读取，fallback 时才能恢复 thinking
    2. 将 thinking 内容转为文本保留：Sequential Thinking 等推理结果不再删除，避免模型丢失工具链上下文
    3. 裁剪消息历史：保留最近 N 轮 + 工具调用上下文
    4. 移除顶层 thinking 配置字段（Kiro 不需要）
    
    Args:
        openai_body: OpenAI 格式的请求体
        
    Returns:
        处理后的紧凑版请求体
    """
    if not isinstance(openai_body, dict):
        return openai_body
    
    compact_body = openai_body.copy()
    
    # 1. 仅移除顶层 thinking 配置（Kiro 不需要），保留 _scid 供 Antigravity fallback 使用
    for key in ("thinking", "thinking_budget", "thinking_level", "thinking_config"):
        compact_body.pop(key, None)
    
    messages = compact_body.get("messages", [])
    if not isinstance(messages, list):
        return compact_body
    
    # 2. 裁剪消息历史：保留最近 N 轮对话 + 工具调用上下文
    compact_messages = []
    tool_context_start_idx = -1
    
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                tool_context_start_idx = i
                break
    
    if tool_context_start_idx >= 0:
        context_start = max(0, tool_context_start_idx - 20)
        compact_messages = messages[context_start:]
        log.debug(
            f"[KIRO_COMPACT] Found tool_calls at index {tool_context_start_idx}, "
            f"keeping {len(compact_messages)} messages (from index {context_start})",
            tag="GATEWAY"
        )
    else:
        compact_messages = messages
        log.debug(
            f"[KIRO_COMPACT] No tool_calls found, keeping all {len(compact_messages)} messages",
            tag="GATEWAY"
        )
    
    # 3. 将 thinking 转为文本保留（不删除），避免 Sequential Thinking 等上下文丢失
    cleaned_messages = []
    thinking_preserved_count = 0
    for msg in compact_messages:
        if not isinstance(msg, dict):
            cleaned_messages.append(msg)
            continue
        
        cleaned_msg = msg.copy()
        content = cleaned_msg.get("content", "")
        if content is None:
            content = ""
        
        converted, had_thinking = _convert_thinking_to_text(content)
        cleaned_msg["content"] = converted
        if had_thinking:
            thinking_preserved_count += 1
        
        # 移除消息级别的 signature 字段（Kiro 不认），保留 _scid 若存在
        for key in ("thinking", "signature", "thoughtSignature"):
            cleaned_msg.pop(key, None)
        
        cleaned_messages.append(cleaned_msg)
    
    compact_body["messages"] = cleaned_messages
    
    log.info(
        f"[KIRO_COMPACT] Prepared compact body: {len(compact_body.get('messages', []))} messages, "
        f"thinking->text preserved={thinking_preserved_count}, _scid kept for fallback",
        tag="GATEWAY"
    )
    
    return compact_body


# ==================== 核心流式函数 ====================

async def stream_openai_with_nodes_bridge(
    *,
    headers: Dict[str, str],
    raw_body: Dict[str, Any],
    model: str,
    messages_override: Optional[List[Dict[str, Any]]] = None,
    scid: Optional[str] = None,
    state_manager: Optional[Any] = None,
) -> AsyncGenerator[str, None]:
    """
    Stream upstream /chat/completions and emit Bugment-compatible NDJSON objects.

    Bugment expects each NDJSON line to be a "BackChatResult"-like object with:
    - text: string (required)
    - nodes: optional list of nodes (e.g. type=5 tool_use)
    - stop_reason: optional string

    Tool loop contract (from vanilla extension):
    - Tool use node: { id, type: 5, tool_use: { tool_use_id, tool_name, input_json } }
    - Tool result node (client->gateway): { id, type: 1, tool_result_node: { tool_use_id, content, is_error } }

    Args:
        headers: 请求头
        raw_body: Bugment 请求体
        model: 模型名称

    Yields:
        NDJSON 格式的响应字符串
    """
    if route_request_with_fallback is None:
        yield json.dumps({"text": "[Gateway Error] route_request_with_fallback not available"}, ensure_ascii=False) + "\n"
        return

    conversation_id = raw_body.get("conversation_id") if isinstance(raw_body, dict) else None
    if messages_override is not None:
        # Phase 5: Bridge 路径始终提供 messages_override，避免重复解析
        messages = messages_override
    else:
        # 旧路径 fallback：当 Bridge 未启用时使用（USE_AUGMENT_BRIDGE=false）
        messages = prepend_bugment_guidance_system_message(raw_body, build_openai_messages_from_bugment(raw_body))

    tools = None
    try:
        raw_tool_defs = raw_body.get("tool_definitions")
        if isinstance(raw_tool_defs, list) and raw_tool_defs:
            if parse_tool_definitions_from_request is not None and convert_tools_to_openai is not None:
                augment_tools = parse_tool_definitions_from_request(raw_tool_defs)
                tools = convert_tools_to_openai(augment_tools) if augment_tools else None
                if tools:
                    log.debug(f"[NODES_BRIDGE] Converted tool_definitions to OpenAI tools: count={len(tools)}", tag="GATEWAY")
            else:
                log.warning(
                    "[NODES_BRIDGE] parse_tool_definitions_from_request/convert_tools_to_openai not available, "
                    "tools will not be sent to backend (model may output tool names as text)",
                    tag="GATEWAY"
                )
    except Exception as e:
        log.warning(f"Failed to convert tool_definitions to OpenAI tools: {e}", tag="GATEWAY")

    request_body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if tools:
        request_body["tools"] = tools
        request_body["tool_choice"] = "auto"
    if scid:
        request_body["_scid"] = scid

    # [FIX 2026-01-23] Kiro Compact 兼容层：对 Augment → Kiro 的请求应用紧凑格式
    # 这确保工具后的 continuation 请求对 Kiro 更友好，减少 502 错误
    if is_backend_capable and BACKENDS:
        try:
            if is_backend_capable("kiro-gateway", model) and BACKENDS.get("kiro-gateway", {}).get("enabled", True):
                request_body = _prepare_kiro_compact_body(request_body)
                log.info(
                    f"[KIRO_COMPACT] Applied compact format for Augment → Kiro (model={model})",
                    tag="GATEWAY"
                )
        except Exception as e:
            log.warning(f"[KIRO_COMPACT] Failed to prepare compact body: {e}", tag="GATEWAY")

    sse_stream = await route_request_with_fallback(
        endpoint="/chat/completions",
        method="POST",
        headers=headers,
        body=request_body,
        model=model,
        stream=True,
    )
    
    # [FIX 2026-01-20] 如果提供了 scid 和 state_manager，包装流式响应以支持状态回写
    # [FIX 2026-01-30] 为 Augment 传递 conversation_id 和 is_augment，启用 Bugment chat_history 回写
    if scid and state_manager and messages:
        from ..scid import wrap_stream_with_writeback
        aug_conv_id = raw_body.get("conversation_id") if isinstance(raw_body, dict) else None
        sse_stream = wrap_stream_with_writeback(
            sse_stream,
            scid,
            state_manager,
            messages,
            conversation_id=aug_conv_id,
            is_augment=True,
        )

    buffer = ""
    tool_calls_by_index: Dict[int, Dict[str, Any]] = {}
    saw_tool_calls = False
    saw_done = False
    thinking_chunks: List[str] = []
    in_thinking_block = False
    content_buffer = ""

    def _longest_suffix_prefix(text: str, token: str) -> int:
        text_lower = text.lower()
        token_lower = token.lower()
        max_len = min(len(text), len(token) - 1)
        for i in range(max_len, 0, -1):
            if text_lower.endswith(token_lower[:i]):
                return i
        return 0
    
    open_tokens = ("<think>", "<thinking>")
    close_tokens = ("</think>", "</thinking>")

    def _find_first_token(text: str, tokens) -> tuple[int, str]:
        text_lower = text.lower()
        best_idx = -1
        best_token = ""
        for token in tokens:
            idx = text_lower.find(token.lower())
            if idx != -1 and (best_idx == -1 or idx < best_idx):
                best_idx = idx
                best_token = token
        return best_idx, best_token

    async for chunk in sse_stream:
        if not chunk:
            continue
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="ignore")
        buffer += chunk

        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue

            json_str = line[6:].strip()
            if json_str == "[DONE]":
                buffer = ""
                saw_done = True
                break

            try:
                evt = json.loads(json_str)
            except Exception:
                continue

            choices = evt.get("choices") or []
            if not choices:
                continue
            choice0 = choices[0] if isinstance(choices[0], dict) else None
            if not choice0:
                continue

            # Text streaming
            delta = choice0.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                content_buffer += content
                while content_buffer:
                    if in_thinking_block:
                        close_idx, close_token = _find_first_token(content_buffer, close_tokens)
                        if close_idx == -1:
                            keep_len = max((_longest_suffix_prefix(content_buffer, token) for token in close_tokens), default=0)
                            if keep_len:
                                stable = content_buffer[:-keep_len]
                                if stable:
                                    thinking_chunks.append(stable)
                                content_buffer = content_buffer[-keep_len:]
                            else:
                                thinking_chunks.append(content_buffer)
                                content_buffer = ""
                            break
                        if close_idx:
                            thinking_chunks.append(content_buffer[:close_idx])
                        content_buffer = content_buffer[close_idx + len(close_token):]
                        in_thinking_block = False
                        continue
                    open_idx, open_token = _find_first_token(content_buffer, open_tokens)
                    if open_idx == -1:
                        keep_len = max((_longest_suffix_prefix(content_buffer, token) for token in open_tokens), default=0)
                        if keep_len:
                            stable = content_buffer[:-keep_len]
                            if stable:
                                yield json.dumps({"text": stable}, ensure_ascii=False, separators=(",", ":")) + "\n"
                            content_buffer = content_buffer[-keep_len:]
                        else:
                            yield json.dumps({"text": content_buffer}, ensure_ascii=False, separators=(",", ":")) + "\n"
                            content_buffer = ""
                        break
                    if open_idx:
                        stable = content_buffer[:open_idx]
                        if stable:
                            yield json.dumps({"text": stable}, ensure_ascii=False, separators=(",", ":")) + "\n"
                    content_buffer = content_buffer[open_idx + len(open_token):]
                    in_thinking_block = True

            reasoning_content = delta.get("reasoning_content")
            if isinstance(reasoning_content, str) and reasoning_content:
                thinking_chunks.append(reasoning_content)

            # Tool calls streaming (OpenAI-like)
            tool_calls = delta.get("tool_calls") or []
            if isinstance(tool_calls, list) and tool_calls:
                saw_tool_calls = True
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    idx = tc.get("index", 0)
                    if not isinstance(idx, int):
                        idx = 0
                    cur = tool_calls_by_index.setdefault(idx, {"id": None, "type": "function", "function": {"name": None, "arguments": ""}})
                    if isinstance(tc.get("id"), str):
                        cur["id"] = tc["id"]
                    if isinstance(tc.get("type"), str):
                        cur["type"] = tc["type"]
                    func = tc.get("function")
                    if isinstance(func, dict):
                        if isinstance(func.get("name"), str):
                            cur["function"]["name"] = func["name"]
                        if isinstance(func.get("arguments"), str):
                            cur["function"]["arguments"] += func["arguments"]

            finish_reason = choice0.get("finish_reason")
            if finish_reason in ("tool_calls", "function_call"):
                saw_tool_calls = True

        if saw_done:
            break

    if content_buffer:
        if in_thinking_block:
            thinking_chunks.append(content_buffer)
        else:
            yield json.dumps({"text": content_buffer}, ensure_ascii=False, separators=(",", ":")) + "\n"
        content_buffer = ""

    thinking_summary = "".join(thinking_chunks).strip()

    if saw_tool_calls and tool_calls_by_index:
        nodes: List[Dict[str, Any]] = []
        next_node_id = 0
        for idx in sorted(tool_calls_by_index.keys()):
            tc = tool_calls_by_index[idx]
            tool_use_id = tc.get("id") or f"call_{int(time.time())}_{idx}"
            func = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            tool_name = func.get("name") if isinstance(func, dict) else None
            arg_str = func.get("arguments") if isinstance(func, dict) else ""
            if not isinstance(tool_use_id, str) or not tool_use_id.strip():
                continue
            if not isinstance(tool_name, str) or not tool_name.strip():
                tool_name = "unknown"
            if not isinstance(arg_str, str):
                arg_str = ""
            arguments_json = arg_str.strip() or "{}"
            # Bugment will JSON.parse(input_json); ensure it's valid JSON.
            try:
                parsed_args = json.loads(arguments_json)
                # Compatibility: some upstreams call codebase-retrieval with `query`, while Bugment expects
                # `information_request` (per sidecar tool schema). Map when needed.
                if tool_name == "codebase-retrieval" and isinstance(parsed_args, dict):
                    if "information_request" not in parsed_args:
                        if isinstance(parsed_args.get("query"), str) and parsed_args.get("query"):
                            parsed_args["information_request"] = parsed_args["query"]
                        elif isinstance(parsed_args.get("informationRequest"), str) and parsed_args.get("informationRequest"):
                            parsed_args["information_request"] = parsed_args["informationRequest"]
                    arguments_json = json.dumps(parsed_args, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                arguments_json = json.dumps({"raw_arguments": arguments_json}, ensure_ascii=False, separators=(",", ":"))

            # Persist tool call mapping for TOOL_RESULT continuations.
            bugment_tool_state_put(conversation_id, tool_use_id, tool_name=tool_name, arguments_json=arguments_json)

            nodes.append(
                {
                    "id": next_node_id,
                    "type": 5,
                    "tool_use": {"tool_use_id": tool_use_id, "tool_name": tool_name, "input_json": arguments_json},
                }
            )
            next_node_id += 1

        if thinking_summary:
            nodes.append(
                {
                    "id": next_node_id,
                    "type": 8,
                    "thinking": {"summary": thinking_summary},
                }
            )
            next_node_id += 1

        if nodes:
            yield json.dumps({"text": "", "nodes": nodes, "stop_reason": "tool_use"}, ensure_ascii=False, separators=(",", ":")) + "\n"
        return

    if thinking_summary:
        nodes = [
            {
                "id": 0,
                "type": 8,
                "thinking": {"summary": thinking_summary},
            }
        ]
        yield json.dumps({"text": "", "nodes": nodes}, ensure_ascii=False, separators=(",", ":")) + "\n"

    # No tool calls: end of turn (send a deterministic terminal marker for clients that rely on it).
    yield json.dumps({"text": "", "stop_reason": "end_turn"}, ensure_ascii=False, separators=(",", ":")) + "\n"
