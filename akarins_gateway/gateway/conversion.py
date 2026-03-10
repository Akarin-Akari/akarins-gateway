"""
Anthropic/OpenAI 格式转换模块

从 unified_gateway_router.py 迁移的转换函数，用于 Kiro Gateway 和 AnyRouter 后端。

这些函数负责在 OpenAI 格式和 Anthropic 格式之间进行转换，以支持：
- Kiro Gateway: 使用 Anthropic Messages API 格式
- AnyRouter: 使用 Anthropic Messages API 格式

作者: 浮浮酱 (Claude Sonnet 4.5)
创建日期: 2026-01-23
迁移自: unified_gateway_router.py
"""

import json
import time
from typing import Dict, Any, List, AsyncIterator

# 延迟导入 log，避免循环依赖
try:
    from akarins_gateway.core.log import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

__all__ = [
    "_convert_openai_to_anthropic_body",
    "_convert_openai_content_to_anthropic",
    "_convert_openai_tools_to_anthropic",
    "_convert_anthropic_to_openai_response",
    "_convert_anthropic_stream_to_openai",
]


def _convert_openai_to_anthropic_body(openai_body: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 OpenAI 格式的请求体转换为 Anthropic 格式

    OpenAI 格式:
    {
        "model": "claude-sonnet-4.5",
        "messages": [
            {"role": "system", "content": "..."},
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "..."}
        ],
        "stream": true,
        "max_tokens": 4096,
        "temperature": 0.7
    }

    Anthropic 格式:
    {
        "model": "claude-sonnet-4-5-20250514",
        "system": "...",
        "messages": [
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "..."}
        ],
        "stream": true,
        "max_tokens": 4096,
        "temperature": 0.7
    }

    [FIX 2026-01-19] 为 Kiro Gateway 添加 OpenAI -> Anthropic 格式转换
    [FIX 2026-01-26] 修复工具调用时消息列表变为空的问题
    [FIX 2026-01-30] 防御性检测：若收到 Antigravity 嵌套格式（project/model/request），先转换为 OpenAI 格式
    """
    # [FIX 2026-01-30] 防御性格式检测：Antigravity 嵌套格式可能被误传入（如 RetryCoordinator 旧逻辑）
    # 若 messages 为空但存在 request.contents，先转换为 OpenAI 格式
    messages = openai_body.get("messages", [])
    if not messages:
        req_block = openai_body.get("request") if isinstance(openai_body.get("request"), dict) else None
        contents = openai_body.get("contents")
        if not contents and req_block:
            contents = req_block.get("contents")
        if contents:
            try:
                from akarins_gateway.converters.message_converter import antigravity_contents_to_openai_messages
                messages = antigravity_contents_to_openai_messages(contents)
                log.info(
                    f"[KIRO CONVERSION] Defensive: Converted Antigravity format to OpenAI (contents_count={len(contents)} -> messages_count={len(messages)})",
                    tag="GATEWAY"
                )
            except Exception as e:
                log.warning(f"[KIRO CONVERSION] Defensive conversion failed: {e}", tag="GATEWAY")

    model = openai_body.get("model", "claude-sonnet-4.5")
    stream = openai_body.get("stream", False)

    # [DEBUG] 记录输入消息数量
    log.debug(
        f"[KIRO CONVERSION] Input: messages_count={len(messages)}, "
        f"model={model}, stream={stream}",
        tag="GATEWAY"
    )

    # 提取 system 消息
    system_content = ""
    anthropic_messages = []
    # [FIX 2026-01-30] 收集连续的 tool 消息，合并为单条 user 消息（Anthropic API 要求）
    # 多个 tool_result 必须在同一 user 消息中，否则 Claude API 返回 400 "Improperly formed request"
    pending_tool_results: List[Dict[str, Any]] = []

    def _flush_tool_results() -> None:
        """将收集的 tool 消息合并为单条 user 消息并追加"""
        nonlocal pending_tool_results
        if not pending_tool_results:
            return
        content_blocks = []
        for tr in pending_tool_results:
            content_blocks.append({
                "type": "tool_result",
                "tool_use_id": tr["tool_call_id"],
                "content": tr["content"],
                **({"is_error": tr["is_error"]} if isinstance(tr.get("is_error"), bool) else {})
            })
        anthropic_messages.append({"role": "user", "content": content_blocks})
        total_len = sum(len(tr["content"]) for tr in pending_tool_results)
        log.debug(
            f"[KIRO CONVERSION] Added merged tool_result message: count={len(pending_tool_results)}, "
            f"total_content_len={total_len}",
            tag="GATEWAY"
        )
        pending_tool_results = []

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        # [DEBUG] 记录每条消息的处理
        log.debug(
            f"[KIRO CONVERSION] Processing message {i}: role={role}, "
            f"content_type={type(content).__name__}, "
            f"has_tool_calls={bool(msg.get('tool_calls'))}, "
            f"has_tool_call_id={bool(msg.get('tool_call_id'))}",
            tag="GATEWAY"
        )
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            _flush_tool_results()
            # 合并多个 system 消息
            if system_content:
                system_content += "\n\n"
            if isinstance(content, str):
                system_content += content
            elif isinstance(content, list):
                # 处理多部分内容
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        system_content += part.get("text", "")
                    elif isinstance(part, str):
                        system_content += part
        elif role in ("user", "assistant"):
            _flush_tool_results()
            # 转换 content 格式
            anthropic_content = _convert_openai_content_to_anthropic(content)

            # 处理 tool_calls (assistant 消息)
            tool_calls = msg.get("tool_calls", [])
            if tool_calls and role == "assistant":
                # 添加 tool_use 块
                if isinstance(anthropic_content, str):
                    anthropic_content = [{"type": "text", "text": anthropic_content}] if anthropic_content else []
                elif not isinstance(anthropic_content, list):
                    anthropic_content = []

                for tc in tool_calls:
                    tool_use_block = {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc.get("function", {}).get("name", ""),
                        "input": {}
                    }
                    # 解析 arguments
                    args_str = tc.get("function", {}).get("arguments", "{}")
                    try:
                        parsed_args = json.loads(args_str) if isinstance(args_str, str) else args_str
                        if not isinstance(parsed_args, dict):
                            parsed_args = {"raw": parsed_args}
                        tool_use_block["input"] = parsed_args
                    except json.JSONDecodeError:
                        tool_use_block["input"] = {"raw": args_str}
                    anthropic_content.append(tool_use_block)

            # [FIX 2026-01-26] 确保即使content为空也添加消息（Anthropic允许空content）
            # 但需要确保content是列表格式（Anthropic要求）
            if isinstance(anthropic_content, str) and not anthropic_content:
                # 空字符串转换为空列表
                anthropic_content = []
            elif not isinstance(anthropic_content, list):
                # 非列表格式转换为列表
                anthropic_content = [{"type": "text", "text": str(anthropic_content)}] if anthropic_content else []

            anthropic_messages.append({
                "role": role,
                "content": anthropic_content
            })
            
            log.debug(
                f"[KIRO CONVERSION] Added {role} message: content_type={type(anthropic_content).__name__}, "
                f"content_len={len(anthropic_content) if isinstance(anthropic_content, list) else 0}",
                tag="GATEWAY"
            )
        elif role == "tool":
            # [FIX 2026-01-30] 收集 tool 消息，稍后合并为单条 user 消息（而非每条单独追加）
            tool_call_id = msg.get("tool_call_id", "")
            if not tool_call_id:
                log.warning(
                    f"[KIRO CONVERSION] Tool message missing tool_call_id, skipping: {msg}",
                    tag="GATEWAY"
                )
                continue  # 跳过没有tool_call_id的tool消息
            
            if isinstance(content, str):
                tool_result_content = content
            else:
                try:
                    tool_result_content = json.dumps(content)
                except Exception:
                    tool_result_content = str(content)
            tool_is_error = msg.get("is_error")

            pending_tool_results.append({
                "tool_call_id": tool_call_id,
                "content": tool_result_content,
                "is_error": tool_is_error,
            })
            
            log.debug(
                f"[KIRO CONVERSION] Collected tool_result: tool_call_id={tool_call_id}, "
                f"content_len={len(tool_result_content)}",
                tag="GATEWAY"
            )
        else:
            _flush_tool_results()
            # [FIX 2026-01-26] 处理未知role的消息，记录警告但不跳过
            log.warning(
                f"[KIRO CONVERSION] Unknown role '{role}', skipping message: {msg}",
                tag="GATEWAY"
            )

    _flush_tool_results()  # 循环结束后刷出剩余的 tool 结果

    # 模型名称映射 (确保使用 Kiro Gateway 支持的格式)
    model_mapping = {
        # 标准格式（版本号在后）
        "claude-sonnet-4.5": "claude-sonnet-4-5-20250514",
        "claude-sonnet-4-5": "claude-sonnet-4-5-20250514",
        "claude-opus-4.5": "claude-opus-4-5-20250514",
        "claude-opus-4-5": "claude-opus-4-5-20250514",
        "claude-haiku-4.5": "claude-haiku-4-5-20250514",
        "claude-haiku-4-5": "claude-haiku-4-5-20250514",
        "claude-sonnet-4": "claude-sonnet-4-20250514",
        # ✅ [FIX 2026-02-17] Opus 4.6 Kiro Gateway 映射日期更新
        "claude-opus-4.6": "claude-opus-4-6-20260205",
        "claude-opus-4-6": "claude-opus-4-6-20260205",
        "claude-4.6-opus": "claude-opus-4-6-20260205",
        "claude-4-6-opus": "claude-opus-4-6-20260205",
        # [FIX 2026-01-23] Cursor 格式（版本号在前）
        "claude-4.5-sonnet": "claude-sonnet-4-5-20250514",
        "claude-4-5-sonnet": "claude-sonnet-4-5-20250514",
        "claude-4.5-opus": "claude-opus-4-5-20250514",
        "claude-4-5-opus": "claude-opus-4-5-20250514",
        "claude-4.5-haiku": "claude-haiku-4-5-20250514",
        "claude-4-5-haiku": "claude-haiku-4-5-20250514",
        "claude-4-sonnet": "claude-sonnet-4-20250514",
    }

    # 处理 thinking 变体
    # ✅ [FIX 2026-02-11] 增强 thinking 检测：Opus 模型天生支持 thinking
    model_lower_for_check = model.lower()
    is_thinking = "-thinking" in model_lower_for_check
    # Opus 4.5+ 天生就是 thinking 模型，无需 -thinking 后缀
    if not is_thinking and "opus" in model_lower_for_check:
        is_thinking = True
        log.info(
            f"[GATEWAY] 🎯 KIRO GATEWAY: Auto-detected Opus model as thinking-capable: {model}",
            tag="GATEWAY"
        )
    base_model = model_lower_for_check.replace("-thinking", "")
    mapped_model = model_mapping.get(base_model, model)

    # [FIX 2026-01-23] Thinking 模式需要在请求体中添加 thinking 参数
    # Anthropic API 通过 thinking 字段控制 thinking 模式，而不是模型名称
    # 
    # 检测 thinking 模式的三种方式（优先级从高到低）：
    # 1. body 中已有 thinking 配置（SCID 架构可能已添加）
    # 2. 模型名称包含 -thinking 后缀
    # 3. 消息中包含 thinking blocks
    thinking_config = None
    
    # 方式1: 检查 body 中是否已有 thinking 配置（SCID 架构可能已添加）
    if "thinking" in openai_body and isinstance(openai_body["thinking"], dict):
        thinking_config = openai_body["thinking"]
        log.info(
            f"[GATEWAY] 🎯 KIRO GATEWAY: Using existing thinking config from body: {thinking_config}",
            tag="GATEWAY"
        )
    elif is_thinking:
        # 方式2: 模型名称包含 -thinking 后缀
        # 添加默认的 thinking 配置
        try:
            from akarins_gateway.converters.anthropic_constants import DEFAULT_THINKING_BUDGET
            thinking_config = {
                "type": "enabled",
                "budget_tokens": DEFAULT_THINKING_BUDGET
            }
            log.info(
                f"[GATEWAY] 🎯 KIRO GATEWAY: Detected thinking model, adding thinking config (budget={DEFAULT_THINKING_BUDGET})",
                tag="GATEWAY"
            )
        except ImportError:
            log.warning("[GATEWAY] Failed to import DEFAULT_THINKING_BUDGET, skipping thinking config", tag="GATEWAY")
    else:
        # 方式3: 检查消息中是否有 thinking blocks（SCID 架构可能检测到但未添加到 body）
        has_thinking_blocks = False
        for msg in anthropic_messages:
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking"):
                        has_thinking_blocks = True
                        break
                if has_thinking_blocks:
                    break
            elif isinstance(content, str) and "<think>" in content.lower():
                has_thinking_blocks = True
                break
        
        if has_thinking_blocks:
            # 消息中有 thinking blocks，添加默认配置
            try:
                from akarins_gateway.converters.anthropic_constants import DEFAULT_THINKING_BUDGET
                thinking_config = {
                    "type": "enabled",
                    "budget_tokens": DEFAULT_THINKING_BUDGET
                }
                log.info(
                    f"[GATEWAY] 🎯 KIRO GATEWAY: Detected thinking blocks in messages, adding thinking config (budget={DEFAULT_THINKING_BUDGET})",
                    tag="GATEWAY"
                )
            except ImportError:
                log.warning("[GATEWAY] Failed to import DEFAULT_THINKING_BUDGET, skipping thinking config", tag="GATEWAY")

    # 构建 Anthropic 格式请求体
    # [FIX 2026-01-23] 使用双向限制策略计算 max_tokens
    # 导入双向限制策略常量
    try:
        from akarins_gateway.converters.anthropic_constants import MAX_ALLOWED_TOKENS, MIN_OUTPUT_TOKENS, DEFAULT_THINKING_BUDGET
    except ImportError:
        # 如果导入失败，使用默认值
        MAX_ALLOWED_TOKENS = 8192
        MIN_OUTPUT_TOKENS = 1024
        DEFAULT_THINKING_BUDGET = 4096
        log.warning("[GATEWAY] Failed to import anthropic_converter constants, using defaults", tag="GATEWAY")

    original_max_tokens = openai_body.get("max_tokens", 8192)

    # 计算需要的 max_tokens
    if thinking_config:
        # Thinking 模式：确保 max_tokens >= thinking_budget + MIN_OUTPUT_TOKENS
        thinking_budget = thinking_config.get("budget_tokens", DEFAULT_THINKING_BUDGET)
        required_tokens = thinking_budget + MIN_OUTPUT_TOKENS

        # 限制在 MAX_ALLOWED_TOKENS 范围内
        adjusted_max_tokens = min(required_tokens, MAX_ALLOWED_TOKENS)

        if adjusted_max_tokens > original_max_tokens:
            log.info(
                f"[GATEWAY] 🎯 KIRO GATEWAY: Adjusted max_tokens from {original_max_tokens} to {adjusted_max_tokens} "
                f"(thinking_budget={thinking_budget}, MIN_OUTPUT={MIN_OUTPUT_TOKENS})",
                tag="GATEWAY"
            )
    else:
        # 非 Thinking 模式：直接使用 MAX_ALLOWED_TOKENS，提供最大输出空间
        adjusted_max_tokens = MAX_ALLOWED_TOKENS

        if adjusted_max_tokens > original_max_tokens:
            log.info(
                f"[GATEWAY] 🎯 KIRO GATEWAY: Adjusted max_tokens from {original_max_tokens} to {adjusted_max_tokens} "
                f"(MAX_ALLOWED_TOKENS={MAX_ALLOWED_TOKENS})",
                tag="GATEWAY"
            )

    # [DEBUG] 记录输出消息数量
    log.debug(
        f"[KIRO CONVERSION] Output: messages_count={len(anthropic_messages)}, "
        f"model={mapped_model}, stream={stream}",
        tag="GATEWAY"
    )
    
    # [FIX 2026-01-26] 验证消息列表不为空
    if not anthropic_messages:
        log.error(
            f"[KIRO CONVERSION] ERROR: Converted messages list is empty! "
            f"Input had {len(messages)} messages. This will cause 400 error.",
            tag="GATEWAY"
        )
        # 如果消息列表为空，至少添加一个空的user消息，避免400错误
        # 但这种情况不应该发生，应该记录错误
        anthropic_messages = [{
            "role": "user",
            "content": [{"type": "text", "text": "Empty message list - conversion error"}]
        }]

    anthropic_body = {
        "model": mapped_model,
        "messages": anthropic_messages,
        "stream": stream,
        "max_tokens": adjusted_max_tokens,
    }

    # 添加 system 消息
    if system_content:
        anthropic_body["system"] = system_content

    # [FIX 2026-01-23] 添加 thinking 配置
    # 如果 body 中已有 thinking 配置，优先使用（可能来自 SCID 架构）
    if "thinking" in openai_body and isinstance(openai_body["thinking"], dict):
        anthropic_body["thinking"] = openai_body["thinking"]
        log.debug(
            f"[GATEWAY] 🎯 KIRO GATEWAY: Using thinking config from original body",
            tag="GATEWAY"
        )
    elif thinking_config:
        anthropic_body["thinking"] = thinking_config
        log.debug(
            f"[GATEWAY] 🎯 KIRO GATEWAY: Added thinking config from detection",
            tag="GATEWAY"
        )

    # 复制其他参数（注意 OpenAI ↔ Anthropic 字段名差异）
    # [FIX 2026-02-16] stop → stop_sequences (Codex debug 诊断: OpenAI 用 stop, Anthropic 用 stop_sequences)
    for key in ("temperature", "top_p"):
        if key in openai_body:
            anthropic_body[key] = openai_body[key]

    # OpenAI `stop` → Anthropic `stop_sequences`
    if "stop" in openai_body and openai_body["stop"] is not None:
        stop_val = openai_body["stop"]
        if isinstance(stop_val, str):
            anthropic_body["stop_sequences"] = [stop_val]
        elif isinstance(stop_val, list):
            anthropic_body["stop_sequences"] = stop_val
        log.debug(
            f"[KIRO CONVERSION] Mapped OpenAI 'stop' -> Anthropic 'stop_sequences': {anthropic_body.get('stop_sequences')}",
            tag="GATEWAY"
        )

    # metadata: 仅当符合 Anthropic 格式时传递 ({"user_id": "..."})
    if "metadata" in openai_body and isinstance(openai_body["metadata"], dict):
        anthropic_body["metadata"] = openai_body["metadata"]

    # 转换 tools
    if "tools" in openai_body:
        anthropic_tools = _convert_openai_tools_to_anthropic(openai_body["tools"])
        anthropic_body["tools"] = anthropic_tools

    return anthropic_body


def _convert_openai_content_to_anthropic(content: Any) -> Any:
    """
    将 OpenAI 格式的 content 转换为 Anthropic 格式

    OpenAI 格式可能是:
    - 字符串: "Hello"
    - 数组: [{"type": "text", "text": "..."}, {"type": "image_url", ...}]

    Anthropic 格式:
    - 字符串: "Hello"
    - 数组: [{"type": "text", "text": "..."}, {"type": "image", ...}]
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        anthropic_parts = []
        for part in content:
            if isinstance(part, dict):
                part_type = part.get("type", "")
                if part_type == "text":
                    anthropic_parts.append({"type": "text", "text": part.get("text", "")})
                elif part_type == "image_url":
                    # 转换图片格式
                    image_url = part.get("image_url", {}).get("url", "")
                    if image_url.startswith("data:"):
                        # 解析 base64 图片
                        import re
                        match = re.match(r"data:([^;]+);base64,(.+)", image_url)
                        if match:
                            anthropic_parts.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": match.group(1),
                                    "data": match.group(2)
                                }
                            })
                    else:
                        # URL 图片
                        anthropic_parts.append({
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": image_url
                            }
                        })
            elif isinstance(part, str):
                anthropic_parts.append({"type": "text", "text": part})
        return anthropic_parts if anthropic_parts else ""

    return content


def _convert_openai_tools_to_anthropic(openai_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将 OpenAI 格式的 tools 转换为 Anthropic 格式

    OpenAI 格式:
    [{"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}]

    Anthropic 格式:
    [{"name": "...", "description": "...", "input_schema": {...}}]
    """
    anthropic_tools = []
    for tool in openai_tools:
        if tool.get("type") == "function":
            func = tool.get("function", {})
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}})
            })
    return anthropic_tools


def _convert_anthropic_to_openai_response(anthropic_response: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 Anthropic 格式的响应转换为 OpenAI 格式

    Anthropic 格式:
    {
        "id": "msg_...",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "..."}],
        "model": "claude-sonnet-4-5-20250514",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 50}
    }

    OpenAI 格式:
    {
        "id": "chatcmpl-...",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "claude-sonnet-4.5",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "..."},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    }

    [FIX 2026-01-19] 为 Kiro Gateway 添加 Anthropic -> OpenAI 响应转换
    """
    # 提取内容
    content_blocks = anthropic_response.get("content", [])
    text_content = ""
    tool_calls = []

    for block in content_blocks:
        if isinstance(block, dict):
            block_type = block.get("type", "")
            if block_type == "text":
                text_content += block.get("text", "")
            elif block_type == "tool_use":
                # 转换 tool_use 为 OpenAI 的 tool_calls
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}))
                    }
                })
        elif isinstance(block, str):
            text_content += block

    # 构建 message
    message = {
        "role": "assistant",
        "content": text_content if text_content else None
    }

    if tool_calls:
        message["tool_calls"] = tool_calls

    # 转换 stop_reason
    stop_reason_mapping = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls"
    }
    finish_reason = stop_reason_mapping.get(
        anthropic_response.get("stop_reason", "end_turn"),
        "stop"
    )

    # 转换 usage
    anthropic_usage = anthropic_response.get("usage", {})
    openai_usage = {
        "prompt_tokens": anthropic_usage.get("input_tokens", 0),
        "completion_tokens": anthropic_usage.get("output_tokens", 0),
        "total_tokens": anthropic_usage.get("input_tokens", 0) + anthropic_usage.get("output_tokens", 0)
    }

    # 构建 OpenAI 响应
    openai_response = {
        "id": f"chatcmpl-{anthropic_response.get('id', 'unknown')}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": anthropic_response.get("model", "claude-sonnet-4.5"),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason
        }],
        "usage": openai_usage
    }

    return openai_response


async def _convert_anthropic_stream_to_openai(byte_iterator) -> AsyncIterator[str]:
    """
    将 Anthropic SSE 流式响应转换为 OpenAI SSE 格式

    Anthropic SSE 事件类型:
    - message_start: 消息开始，包含 message 对象
    - content_block_start: 内容块开始
    - content_block_delta: 内容块增量
    - content_block_stop: 内容块结束
    - message_delta: 消息增量（包含 stop_reason）
    - message_stop: 消息结束

    OpenAI SSE 格式:
    data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"..."}}]}

    [FIX 2026-01-19] 为 Kiro Gateway 添加流式响应转换
    """
    buffer = ""
    message_id = f"chatcmpl-kiro-{int(time.time())}"
    model = "claude-sonnet-4.5"
    current_tool_call_index = -1
    tool_call_id = ""
    tool_call_name = ""
    current_content_block_type = None

    async for chunk in byte_iterator:
        if not chunk:
            continue

        buffer += chunk.decode("utf-8", errors="ignore")

        # 解析 SSE 事件
        while "\n\n" in buffer:
            event_str, buffer = buffer.split("\n\n", 1)
            lines = event_str.strip().split("\n")

            event_type = None
            event_data = None

            for line in lines:
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_str = line[5:].strip()
                    if data_str:
                        try:
                            event_data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

            if not event_data:
                continue

            # [DEBUG] 记录收到的事件类型
            if event_type:
                log.debug(f"[KIRO STREAM] Received event: {event_type}", tag="GATEWAY")

            # 根据事件类型转换
            if event_type == "message_start":
                # 提取消息信息
                message = event_data.get("message", {})
                message_id = f"chatcmpl-{message.get('id', 'unknown')}"
                model = message.get("model", "claude-sonnet-4.5")

                # 发送初始 chunk
                openai_chunk = {
                    "id": message_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None
                    }]
                }
                yield f"data: {json.dumps(openai_chunk)}\n\n"

            elif event_type == "content_block_start":
                content_block = event_data.get("content_block", {})
                block_type = content_block.get("type", "")
                current_content_block_type = block_type

                if block_type == "tool_use":
                    # 工具调用开始
                    current_tool_call_index += 1
                    tool_call_id = content_block.get("id", "")
                    tool_call_name = content_block.get("name", "")

                    openai_chunk = {
                        "id": message_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": current_tool_call_index,
                                    "id": tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": tool_call_name,
                                        "arguments": ""
                                    }
                                }]
                            },
                            "finish_reason": None
                        }]
                    }
                    yield f"data: {json.dumps(openai_chunk)}\n\n"

            elif event_type == "content_block_delta":
                delta = event_data.get("delta", {})
                delta_type = delta.get("type", "")

                if delta_type == "text_delta":
                    # 文本增量
                    text = delta.get("text", "")
                    if text:
                        if current_content_block_type in ("thinking", "redacted_thinking"):
                            openai_chunk = {
                                "id": message_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"reasoning_content": text},
                                    "finish_reason": None
                                }]
                            }
                        else:
                            openai_chunk = {
                                "id": message_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": text},
                                    "finish_reason": None
                                }]
                            }
                        yield f"data: {json.dumps(openai_chunk)}\n\n"

                elif delta_type == "thinking_delta":
                    thinking_text = delta.get("thinking") or delta.get("text") or ""
                    if thinking_text:
                        openai_chunk = {
                            "id": message_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"reasoning_content": thinking_text},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(openai_chunk)}\n\n"

                elif delta_type == "input_json_delta":
                    # 工具调用参数增量
                    partial_json = delta.get("partial_json", "")
                    if partial_json:
                        openai_chunk = {
                            "id": message_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "tool_calls": [{
                                        "index": current_tool_call_index,
                                        "function": {
                                            "arguments": partial_json
                                        }
                                    }]
                                },
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(openai_chunk)}\n\n"

            elif event_type == "content_block_stop":
                current_content_block_type = None

            elif event_type == "message_delta":
                # 消息增量（包含 stop_reason）
                delta = event_data.get("delta", {})
                stop_reason = delta.get("stop_reason")

                # [DEBUG] 记录 stop_reason
                if stop_reason:
                    log.warning(f"[KIRO STREAM] Received stop_reason: {stop_reason} (will convert to OpenAI finish_reason)", tag="GATEWAY")

                if stop_reason:
                    # 转换 stop_reason
                    stop_reason_mapping = {
                        "end_turn": "stop",
                        "stop_sequence": "stop",
                        "max_tokens": "length",
                        "tool_use": "tool_calls"
                    }
                    finish_reason = stop_reason_mapping.get(stop_reason, "stop")

                    openai_chunk = {
                        "id": message_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": finish_reason
                        }]
                    }
                    yield f"data: {json.dumps(openai_chunk)}\n\n"

            elif event_type == "message_stop":
                # 消息结束
                yield "data: [DONE]\n\n"
