"""
Message Converter - Convert messages between OpenAI/Gemini and Antigravity formats
消息转换器 - 在 OpenAI/Gemini 和 Antigravity 格式之间转换消息
"""

import json
import re
from typing import Any, Dict, List, Optional

from akarins_gateway.core.log import log
from akarins_gateway.signature_cache import get_cached_signature
# [FIX 2026-01-11] 导入 gemini_fix 的清理函数
from .gemini_fix import clean_contents, ALLOWED_PART_KEYS
from .thoughtSignature_fix import decode_tool_id_and_signature

# [FIX 2026-02-17] 图片 MIME 类型规范化
# 参考 Antigravity-Manager v4.1.18
# 部分客户端（如 Cursor、Continue 等）发送的图片 MIME 类型可能存在大小写不一致、
# 别名格式或非标准后缀，直接透传会导致上游 API 返回 400 错误。
# 此映射表将常见的非标准 MIME 类型统一映射为标准格式。
MIME_NORMALIZE_MAP: Dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/jpe": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
    "image/svg": "image/svg+xml",
}

# 上游 API 支持的合法图片 MIME 类型集合
VALID_IMAGE_MIMES: set = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/svg+xml",
}


def normalize_image_mime(mime_type: str) -> str:
    """
    [FIX 2026-02-17] 规范化图片 MIME 类型

    将非标准或别名 MIME 类型映射为上游 API 接受的标准格式。
    处理大小写不一致、前后空白符、以及常见别名（如 image/jpg -> image/jpeg）。

    Args:
        mime_type: 原始 MIME 类型字符串

    Returns:
        规范化后的 MIME 类型字符串
    """
    if not mime_type:
        log.debug("[MIME] Empty MIME type received, defaulting to image/jpeg")
        return "image/jpeg"
    normalized = mime_type.lower().strip()
    normalized = MIME_NORMALIZE_MAP.get(normalized, normalized)
    # [FIX 2026-02-17] 验证规范化后的 MIME 类型是否在上游支持列表中
    if normalized not in VALID_IMAGE_MIMES:
        log.warning(f"[MIME] Unsupported MIME type: {normalized}, falling back to image/jpeg")
        return "image/jpeg"
    return normalized


def extract_images_from_content(content: Any) -> Dict[str, Any]:
    """
    从 OpenAI content 中提取文本和图片
    """
    result = {"text": "", "images": []}

    if isinstance(content, str):
        result["text"] = content
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    result["text"] += item.get("text", "")
                elif item.get("type") == "image_url":
                    image_url = item.get("image_url", {}).get("url", "")
                    # 解析 data:image/png;base64,xxx 格式
                    if image_url.startswith("data:image/"):
                        match = re.match(r"^data:image/([\w+\-.]+);base64,(.+)$", image_url)
                        if match:
                            mime_type = match.group(1)
                            base64_data = match.group(2)
                            # [FIX 2026-02-17] 规范化 MIME 类型，防止非标准后缀导致 400 错误
                            normalized_mime = normalize_image_mime(f"image/{mime_type}")
                            result["images"].append({
                                "inlineData": {
                                    "mimeType": normalized_mime,
                                    "data": base64_data
                                }
                            })

    return result


def strip_thinking_from_openai_messages(messages: List[Any]) -> List[Any]:
    """
    从 OpenAI 格式消息中移除 thinking 内容块。

    当 thinking 被禁用时，历史消息中的 thinking 内容块会导致 400 错误：
    "When thinking is disabled, an `assistant` message..."

    此函数会：
    1. 遍历所有消息
    2. 对于 assistant 消息，移除 content 中的 thinking 相关内容
    3. 处理字符串格式的 content（移除 <think>...</think> 或 <think>...</think> 标签）
    4. 处理数组格式的 content（移除 type="thinking" 的项）
    """
    if not messages:
        return messages

    cleaned_messages = []

    for msg in messages:
        # 处理 Pydantic 模型对象
        if hasattr(msg, "role") and hasattr(msg, "content"):
            role = getattr(msg, "role", None)
            content = getattr(msg, "content", None)

            # 只处理 assistant 消息
            if role == "assistant" and content:
                # 处理字符串格式的 content
                if isinstance(content, str):
                    # 移除各种 thinking 标签格式
                    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE)
                    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE)
                    # 清理多余的空白行
                    content = re.sub(r'\n\s*\n\s*\n', '\n\n', content)
                    # 如果内容为空，保留一个占位符
                    if not content.strip():
                        content = "..."
                    # 创建新消息对象，保留 tool_calls
                    # [FIX 2026-01-08] 必须保留 tool_calls 字段，否则会导致孤儿 tool_result
                    from akarins_gateway.models import OpenAIChatMessage
                    tool_calls = getattr(msg, "tool_calls", None)
                    cleaned_msg = OpenAIChatMessage(role=role, content=content, tool_calls=tool_calls)
                    cleaned_messages.append(cleaned_msg)
                    continue

                # 处理数组格式的 content
                elif isinstance(content, list):
                    cleaned_content = []
                    for item in content:
                        if isinstance(item, dict):
                            item_type = item.get("type")
                            # 跳过 thinking 类型的内容块
                            if item_type in ("thinking", "redacted_thinking"):
                                continue
                            # [FIX 2026-01-20] 清理非 thinking items 中的 thoughtSignature 字段
                            # 问题：Cursor 可能在历史消息的 text parts 中错误地保留 thoughtSignature
                            # 这会导致 Claude API 返回 400 错误："Invalid signature in thinking block"
                            # 解决：创建一个新的 dict，只保留必要字段，排除 thoughtSignature
                            if "thoughtSignature" in item or "signature" in item:
                                # 创建一个干净的副本，排除签名字段
                                cleaned_item = {k: v for k, v in item.items() if k not in ("thoughtSignature", "signature")}
                                cleaned_content.append(cleaned_item)
                            else:
                                cleaned_content.append(item)
                        else:
                            cleaned_content.append(item)

                    # 如果清理后为空，添加一个空文本块
                    if not cleaned_content:
                        cleaned_content = [{"type": "text", "text": "..."}]

                    # 创建新消息对象，保留 tool_calls
                    # [FIX 2026-01-08] 必须保留 tool_calls 字段，否则会导致孤儿 tool_result
                    from akarins_gateway.models import OpenAIChatMessage
                    tool_calls = getattr(msg, "tool_calls", None)
                    cleaned_msg = OpenAIChatMessage(role=role, content=cleaned_content, tool_calls=tool_calls)
                    cleaned_messages.append(cleaned_msg)
                    continue

        # 处理字典格式的消息
        elif isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content")

            # 只处理 assistant 消息
            if role == "assistant" and content:
                # 处理字符串格式的 content
                if isinstance(content, str):
                    # 移除各种 thinking 标签格式
                    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE)
                    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE)
                    # 清理多余的空白行
                    content = re.sub(r'\n\s*\n\s*\n', '\n\n', content)
                    # 如果内容为空，保留一个占位符
                    if not content.strip():
                        content = "..."
                    # 创建新消息对象，保留 tool_calls
                    # [FIX 2026-01-22] 必须保留 tool_calls 字段，否则会导致孤儿 tool_result
                    cleaned_msg = msg.copy()
                    cleaned_msg["content"] = content
                    # 确保 tool_calls 字段被保留（如果存在）
                    if "tool_calls" in msg:
                        cleaned_msg["tool_calls"] = msg["tool_calls"]
                    cleaned_messages.append(cleaned_msg)
                    continue

                # 处理数组格式的 content
                elif isinstance(content, list):
                    cleaned_content = []
                    for item in content:
                        if isinstance(item, dict):
                            item_type = item.get("type")
                            # 跳过 thinking 类型的内容块
                            if item_type in ("thinking", "redacted_thinking"):
                                continue
                            # [FIX 2026-01-20] 清理非 thinking items 中的 thoughtSignature 字段
                            # 问题：Cursor 可能在历史消息的 text parts 中错误地保留 thoughtSignature
                            # 这会导致 Claude API 返回 400 错误："Invalid signature in thinking block"
                            # 解决：创建一个新的 dict，只保留必要字段，排除 thoughtSignature
                            if "thoughtSignature" in item or "signature" in item:
                                # 创建一个干净的副本，排除签名字段
                                cleaned_item = {k: v for k, v in item.items() if k not in ("thoughtSignature", "signature")}
                                cleaned_content.append(cleaned_item)
                            else:
                                cleaned_content.append(item)
                        else:
                            cleaned_content.append(item)

                    # 如果清理后为空，添加一个空文本块
                    if not cleaned_content:
                        cleaned_content = [{"type": "text", "text": "..."}]

                    # 创建新消息对象，保留 tool_calls
                    # [FIX 2026-01-22] 必须保留 tool_calls 字段，否则会导致孤儿 tool_result
                    # 问题：当消息是字典格式时，strip_thinking_from_openai_messages 没有保留 tool_calls 字段
                    # 这会导致 openai_messages_to_antigravity_contents 无法建立 tool_call_id_to_name 映射
                    # 结果：tool_use 存在但 tool_result 找不到对应的 tool_use，导致 400 错误
                    cleaned_msg = msg.copy()
                    cleaned_msg["content"] = cleaned_content
                    # 确保 tool_calls 字段被保留（如果存在）
                    if "tool_calls" in msg:
                        cleaned_msg["tool_calls"] = msg["tool_calls"]
                    cleaned_messages.append(cleaned_msg)
                    continue

        # 其他情况直接保留
        cleaned_messages.append(msg)

    return cleaned_messages


# 工具格式提示常量
TOOL_FORMAT_REMINDER_TEMPLATE = """

[IMPORTANT - Tool Call Format Rules]
When calling tools, you MUST follow these rules strictly:
1. Always use the EXACT parameter names as defined in the current tool schema
2. Do NOT use parameter names from previous conversations - schemas may have changed
3. For terminal/command tools: the parameter name varies - check the tool definition
4. When in doubt: re-read the tool definition and use ONLY the parameters listed there

{tool_params_section}
"""

TOOL_FORMAT_REMINDER_AFTER_ERROR_TEMPLATE = """

[CRITICAL - Tool Call Error Detected]
Previous tool calls failed due to invalid arguments. You MUST:
1. STOP using parameter names from previous conversations
2. Use ONLY the exact parameter names shown below
3. Do NOT guess parameter names

{tool_params_section}

IMPORTANT: If a tool call fails, check the parameter names above and try again with the EXACT names listed.
"""

SEQUENTIAL_THINKING_PROMPT = """
[IMPORTANT: Thinking Capability Redirection]
Internal thinking/reasoning models are currently disabled or limited.
For complex tasks requiring step-by-step analysis, planning, or reasoning, you MUST use the 'sequentialthinking' (or 'sequential_thinking') tool.
Do NOT attempt to output <think> tags or raw reasoning text. Delegate all reasoning steps to the tool.
"""


def openai_messages_to_antigravity_contents(
    messages: List[Any],
    enable_thinking: bool = False,
    tools: Optional[List[Any]] = None,
    recommend_sequential_thinking: bool = False
) -> List[Dict[str, Any]]:
    """
    将 OpenAI 消息格式转换为 Antigravity contents 格式

    Args:
        messages: OpenAI 格式的消息列表
        enable_thinking: 是否启用 thinking（当启用时，最后一条 assistant 消息必须以 thinking block 开头）
        tools: 工具定义列表（用于提取参数摘要）
        recommend_sequential_thinking: 是否推荐使用 Sequential Thinking 工具
    """
    from .tool_converter import extract_tool_params_summary

    # Check for sequential thinking tool
    has_sequential_tool = False
    if recommend_sequential_thinking and tools:
        for tool in tools:
            name = ""
            if isinstance(tool, dict):
                if "function" in tool:
                    name = tool["function"].get("name", "")
                else:
                    name = tool.get("name", "")
            elif hasattr(tool, "function"):
                name = getattr(tool.function, "name", "")
            elif hasattr(tool, "name"):
                name = getattr(tool, "name", "")

            if name and "sequential" in name.lower() and "thinking" in name.lower():
                has_sequential_tool = True
                break

    contents = []
    system_messages = []

    has_tool_error = False
    has_tools = False  # 检测是否有工具调用

    # [FIX 2026-01-08] 建立 tool_call_id -> tool_name 的映射
    # 用于验证 tool 消息是否有对应的 tool_use，避免 Anthropic API 返回 400 错误：
    # "unexpected `tool_use_id` found in `tool_result` blocks"
    tool_call_id_to_name: dict = {}
    def _get_field(msg_obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(msg_obj, dict):
            return msg_obj.get(key, default)
        return getattr(msg_obj, key, default)

    def _get_tool_id_variants(tool_id: Any) -> List[str]:
        if not tool_id:
            return []
        raw_id = str(tool_id)
        variants = {raw_id}
        try:
            original_id, _ = decode_tool_id_and_signature(raw_id)
            if original_id:
                variants.add(str(original_id))
        except Exception as e:
            log.debug(f"[MESSAGE_CONVERTER] Failed to decode tool_id variants: {e}")
        return list(variants)

    def _extract_tool_call_id_and_name(tool_call: Any) -> tuple[Optional[str], str]:
        tc_id = None
        tc_function = None
        tc_name = ""
        if isinstance(tool_call, dict):
            tc_id = tool_call.get("id")
            tc_function = tool_call.get("function")
            tc_name = tool_call.get("name", "")
        else:
            tc_id = getattr(tool_call, "id", None)
            tc_function = getattr(tool_call, "function", None)
            tc_name = getattr(tool_call, "name", "")

        if tc_function:
            if isinstance(tc_function, dict):
                tc_name = tc_function.get("name", tc_name)
            else:
                tc_name = getattr(tc_function, "name", tc_name)

        return tc_id, tc_name or ""

    def _iter_tool_use_blocks(content: Any) -> List[Dict[str, Any]]:
        if not isinstance(content, list):
            return []
        return [
            item for item in content
            if isinstance(item, dict) and item.get("type") == "tool_use"
        ]

    def _extract_tool_result_output(raw_content: Any) -> Any:
        """
        从 OpenAI/Anthropic 风格的 tool_result.content 中提取可序列化输出。
        """
        if isinstance(raw_content, list):
            if not raw_content:
                return ""
            first = raw_content[0]
            if isinstance(first, dict) and first.get("type") == "text":
                return str(first.get("text", ""))
            try:
                return json.dumps(raw_content, ensure_ascii=False)
            except Exception:
                return str(raw_content)
        if raw_content is None:
            return ""
        if isinstance(raw_content, (str, int, float, bool)):
            return raw_content
        try:
            return json.dumps(raw_content, ensure_ascii=False)
        except Exception:
            return str(raw_content)

    for msg in messages:
        msg_tool_calls = _get_field(msg, "tool_calls")
        if msg_tool_calls:
            for tc in msg_tool_calls:
                tc_id, tc_name = _extract_tool_call_id_and_name(tc)
                if tc_id and tc_name:
                    for variant in _get_tool_id_variants(tc_id):
                        tool_call_id_to_name[variant] = tc_name

        msg_content = _get_field(msg, "content")
        for tool_use in _iter_tool_use_blocks(msg_content):
            tc_id = tool_use.get("id") or tool_use.get("tool_use_id") or tool_use.get("call_id")
            tc_name = tool_use.get("name", "")
            if tc_id and tc_name:
                for variant in _get_tool_id_variants(tc_id):
                    tool_call_id_to_name[variant] = tc_name

    # [FIX 2026-01-20] 建立 tool_result_ids 集合
    # 用于验证 tool_use 是否有对应的 tool_result，避免 Claude API 返回 400 错误：
    # "tool_use ids were found without tool_result blocks immediately after"
    # 场景: Cursor 重试时可能发送不完整的历史消息，tool_use 存在但 tool_result 缺失
    tool_result_ids: set = set()
    for msg in messages:
        msg_role = _get_field(msg, "role", "")
        if msg_role == "tool":
            tc_id = _get_field(msg, "tool_call_id", None)
            if tc_id:
                for variant in _get_tool_id_variants(tc_id):
                    tool_result_ids.add(variant)
        msg_content = _get_field(msg, "content", None)
        if isinstance(msg_content, list):
            for item in msg_content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_use_id = item.get("tool_use_id") or item.get("id") or item.get("call_id")
                    if tool_use_id:
                        for variant in _get_tool_id_variants(tool_use_id):
                            tool_result_ids.add(variant)
        msg_parts = _get_field(msg, "parts", None)
        if isinstance(msg_parts, list):
            for part in msg_parts:
                if isinstance(part, dict) and "functionResponse" in part:
                    fr = part.get("functionResponse", {})
                    fr_id = fr.get("id") or fr.get("callId") or fr.get("call_id") or fr.get("tool_use_id")
                    if fr_id:
                        for variant in _get_tool_id_variants(fr_id):
                            tool_result_ids.add(variant)

    def _has_matching_tool_result(tool_id: Any) -> bool:
        for variant in _get_tool_id_variants(tool_id):
            if variant in tool_result_ids:
                return True
        return False

    for msg in messages:
        msg_content = _get_field(msg, "content", "")
        msg_tool_calls = _get_field(msg, "tool_calls")

        # 检测是否有工具调用
        if msg_tool_calls or _iter_tool_use_blocks(msg_content):
            has_tools = True

        # 检测错误模式
        if msg_content and isinstance(msg_content, str):
            error_patterns = [
                "invalid arguments",
                "Invalid arguments",
                "invalid parameters",
                "Invalid parameters",
                "Unexpected parameters",
                "unexpected parameters",
                "model provided invalid",
                "Tool call arguments",
                "were invalid",
            ]
            for pattern in error_patterns:
                if pattern in msg_content:
                    has_tool_error = True
                    log.info(f"[ANTIGRAVITY] Detected tool error pattern in message: '{pattern}'")
                    break
        if has_tool_error:
            break

    for i, msg in enumerate(messages):
        role = _get_field(msg, "role", "user")
        content = _get_field(msg, "content", "")
        tool_calls = _get_field(msg, "tool_calls")
        tool_call_id = _get_field(msg, "tool_call_id")

        # 处理 system 消息 - 合并到第一条用户消息
        if role == "system":
            # Inject Sequential Thinking prompt if recommended and available
            if has_sequential_tool:
                content = content + SEQUENTIAL_THINKING_PROMPT
                log.info("[ANTIGRAVITY] Injected Sequential Thinking prompt into system message")

            # 在 system 消息末尾注入工具格式提示（包含动态参数）
            if has_tools:
                # 提取工具参数摘要（从传入的 tools 参数中提取）
                tool_params = extract_tool_params_summary(tools) if tools else ""

                if not tool_params:
                    tool_params_section = "Check the tool definitions in your context for exact parameter names."
                else:
                    tool_params_section = tool_params

                if has_tool_error:
                    # 检测到错误，注入强化提示
                    reminder = TOOL_FORMAT_REMINDER_AFTER_ERROR_TEMPLATE.format(tool_params_section=tool_params_section)
                    content = content + reminder
                    log.info(f"[ANTIGRAVITY] Injected TOOL_FORMAT_REMINDER_AFTER_ERROR with params into system message")
                else:
                    # 预防性注入基础提示
                    reminder = TOOL_FORMAT_REMINDER_TEMPLATE.format(tool_params_section=tool_params_section)
                    content = content + reminder
                    log.debug("[ANTIGRAVITY] Injected TOOL_FORMAT_REMINDER with params into system message")
            system_messages.append(content)
            continue

        # 处理 user 消息
        elif role == "user":
            parts = []

            # 如果有系统消息，添加到第一条用户消息
            if system_messages:
                for sys_msg in system_messages:
                    parts.append({"text": sys_msg})
                system_messages = []

            # [FIX 2026-02-07] 兼容 Anthropic 数组格式中的 tool_result，避免 functionCall/functionResponse 断链
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        if item:
                            parts.append({"text": str(item)})
                        continue

                    item_type = item.get("type")
                    if item_type == "text":
                        text = item.get("text", "")
                        if text:
                            parts.append({"text": text})
                    elif item_type == "image_url":
                        image_url = item.get("image_url", {}).get("url", "")
                        if image_url.startswith("data:image/"):
                            match = re.match(r"^data:image/([\w+\-.]+);base64,(.+)$", image_url)
                            if match:
                                mime_type = match.group(1)
                                base64_data = match.group(2)
                                # [FIX 2026-02-17] 规范化 MIME 类型，防止非标准后缀导致 400 错误
                                normalized_mime = normalize_image_mime(f"image/{mime_type}")
                                parts.append({
                                    "inlineData": {
                                        "mimeType": normalized_mime,
                                        "data": base64_data
                                    }
                                })
                    elif item_type == "tool_result":
                        tool_use_id = item.get("tool_use_id") or item.get("id") or item.get("call_id")
                        if not tool_use_id:
                            log.warning(f"[ANTIGRAVITY] User tool_result missing tool_use_id at index {i}, skipping")
                            continue

                        tool_id_variants = _get_tool_id_variants(tool_use_id)
                        tool_name = item.get("name", "")
                        if not tool_name or not str(tool_name).strip():
                            recovered_name = ""
                            for variant in tool_id_variants:
                                if variant in tool_call_id_to_name:
                                    recovered_name = tool_call_id_to_name[variant]
                                    break
                            if recovered_name:
                                tool_name = recovered_name
                            else:
                                tool_name = f"tool_{tool_use_id}"

                        output = _extract_tool_result_output(item.get("content"))
                        parts.append({
                            "functionResponse": {
                                "id": tool_use_id,
                                "name": tool_name,
                                "response": {"output": output}
                            }
                        })
                    else:
                        # 兜底：保留可读文本，避免直接丢内容
                        text = item.get("text")
                        if text:
                            parts.append({"text": str(text)})
            else:
                # 提取文本和图片
                extracted = extract_images_from_content(content)
                if extracted["text"]:
                    parts.append({"text": extracted["text"]})
                parts.extend(extracted["images"])

            if parts:
                contents.append({"role": "user", "parts": parts})

        # 处理 assistant 消息
        elif role == "assistant":
            # [DEBUG] 打印 assistant 消息的详细信息
            content_type = type(content).__name__
            content_len = len(str(content)) if content else 0
            log.info(f"[MESSAGE_CONVERTER DEBUG] Processing assistant message: content_type={content_type}, content_len={content_len}")
            if isinstance(content, str) and content:
                has_think_tag = "<think>" in content.lower() or "</think>" in content.lower()
                log.info(f"[MESSAGE_CONVERTER DEBUG] String content has_think_tag={has_think_tag}, first_100_chars='{content[:100]}...'")
            elif isinstance(content, list):
                log.info(f"[MESSAGE_CONVERTER DEBUG] List content with {len(content)} items, item_types={[type(i).__name__ for i in content[:3]]}")
            
            # 处理 content：可能是字符串或数组
            content_parts = []
            seen_tool_call_ids: set[str] = set()
            if content:
                if isinstance(content, str):
                    # 字符串格式：检查是否包含 thinking 标签
                    # 匹配 <think>...</think> 或 <think>...</think>
                    thinking_match = re.search(r'<(?:redacted_)?reasoning>.*?</(?:redacted_)?reasoning>', content, flags=re.DOTALL | re.IGNORECASE)
                    if not thinking_match:
                        thinking_match = re.search(r'<think>.*?</think>', content, flags=re.DOTALL | re.IGNORECASE)

                    if thinking_match:
                        # 提取 thinking 内容
                        thinking_text = thinking_match.group(0)
                        log.info(f"[MESSAGE_CONVERTER DEBUG] Found thinking_match: match_len={len(thinking_text)}")
                        # 移除标签，保留内容
                        thinking_content = re.sub(r'</?(?:redacted_)?reasoning>', '', thinking_text, flags=re.IGNORECASE)
                        thinking_content = re.sub(r'</?think>', '', thinking_content, flags=re.IGNORECASE)
                        thinking_content = thinking_content.strip()
                        log.info(f"[MESSAGE_CONVERTER DEBUG] Extracted thinking_content: len={len(thinking_content)}, first_50='{thinking_content[:50]}...'")

                        # [FIX 2026-01-21] 修正：不再无条件丢弃历史 thinking blocks
                        # 原来的注释说 "signature 是会话绑定的" 是错误理解
                        # 实际上：signature 是用于验证 thinking 内容完整性的，
                        # 只要 signature + thinking 内容匹配，任何请求都可以使用
                        #
                        # 字符串格式的 thinking（如 <think>...</think>）通常来自客户端截断，
                        # 不包含 signature。但我们不应该在这里丢弃它，
                        # 而是让上游（antigravity_router.py）从缓存恢复 signature
                        # 或者让 filter_thinking_for_target_model 根据目标模型决定是否保留
                        #
                        # 策略变更：保留 thinking 内容，让上游处理
                        log.info(f"[MESSAGE_CONVERTER] 保留历史 thinking block (字符串格式) 供上游处理: thinking_len={len(thinking_content)}")

                        # 移除 thinking 标签，但保留内容作为 thinking 块
                        # 移除原始的 thinking 标签
                        content = re.sub(r'<(?:redacted_)?reasoning>.*?</(?:redacted_)?reasoning>', '', content, flags=re.DOTALL | re.IGNORECASE)
                        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE)
                        content = content.strip()

                        # 将 thinking 内容作为 thinking 块添加（不带 signature，让上游恢复）
                        if thinking_content:
                            content_parts.append({
                                "text": thinking_content,
                                "thought": True,
                                # 注意：这里不设置 thoughtSignature，让上游从缓存恢复
                            })

                    extracted = extract_images_from_content(content)
                    if extracted["text"]:
                        content_parts.append({"text": extracted["text"]})
                    content_parts.extend(extracted["images"])
                elif isinstance(content, list):
                    # 数组格式：检查是否有 thinking 类型的内容块
                    for item in content:
                        if isinstance(item, dict):
                            item_type = item.get("type")
                            if item_type == "thinking":
                                # 提取 thinking 内容
                                thinking_text = item.get("thinking", "")
                                # [FIX 2026-01-20] 兼容两种签名字段名：signature 和 thoughtSignature
                                message_signature = item.get("signature") or item.get("thoughtSignature") or ""

                                # [FIX 2026-01-21] 修正：不再无条件丢弃历史 thinking blocks
                                # 如果有 signature，保留它；如果没有，也保留 thinking 内容让上游恢复
                                if thinking_text:
                                    if message_signature:
                                        log.info(f"[MESSAGE_CONVERTER] 保留历史 thinking block (数组格式，有签名): thinking_len={len(thinking_text)}, sig_len={len(message_signature)}")
                                        content_parts.append({
                                            "text": thinking_text,
                                            "thought": True,
                                            "thoughtSignature": message_signature
                                        })
                                    else:
                                        log.info(f"[MESSAGE_CONVERTER] 保留历史 thinking block (数组格式，无签名，待上游恢复): thinking_len={len(thinking_text)}")
                                        content_parts.append({
                                            "text": thinking_text,
                                            "thought": True,
                                            # 不设置 thoughtSignature，让上游从缓存恢复
                                        })
                            elif item_type == "redacted_thinking":
                                # [FIX 2026-01-21] redacted_thinking 也保留，让上游处理
                                thinking_text = item.get("thinking") or item.get("data", "")
                                message_signature = item.get("signature") or item.get("thoughtSignature") or ""
                                if thinking_text:
                                    if message_signature:
                                        log.info(f"[MESSAGE_CONVERTER] 保留历史 redacted_thinking block (有签名): thinking_len={len(thinking_text)}")
                                        content_parts.append({
                                            "text": thinking_text,
                                            "thought": True,
                                            "thoughtSignature": message_signature
                                        })
                                    else:
                                        log.info(f"[MESSAGE_CONVERTER] 保留历史 redacted_thinking block (无签名，待上游恢复): thinking_len={len(thinking_text)}")
                                        content_parts.append({
                                            "text": thinking_text,
                                            "thought": True,
                                        })
                            elif item_type == "text":
                                content_parts.append({"text": item.get("text", "")})
                            elif item_type == "image_url":
                                # 处理图片
                                image_url = item.get("image_url", {}).get("url", "")
                                if image_url.startswith("data:image/"):
                                    match = re.match(r"^data:image/([\w+\-.]+);base64,(.+)$", image_url)
                                    if match:
                                        mime_type = match.group(1)
                                        base64_data = match.group(2)
                                        # [FIX 2026-02-17] 规范化 MIME 类型，防止非标准后缀导致 400 错误
                                        normalized_mime = normalize_image_mime(f"image/{mime_type}")
                                        content_parts.append({
                                            "inlineData": {
                                                "mimeType": normalized_mime,
                                                "data": base64_data
                                            }
                                        })
                            elif item_type == "tool_use":
                                tool_use_id = item.get("id") or item.get("tool_use_id") or item.get("call_id")
                                tool_use_name = item.get("name", "")
                                tool_input = item.get("input", {})
                                
                                # [FIX 2026-01-25] 渐进式修复：如果tool_use没有ID，尝试恢复
                                if not tool_use_id:
                                    # 策略1: 尝试从tool_result_ids中通过name匹配恢复ID
                                    recovered_id = None
                                    for variant_id in tool_result_ids:
                                        # 检查是否有对应的tool_result（通过后续消息检查）
                                        # 注意：这里无法直接检查，因为tool_result在后续消息中
                                        # 所以暂时跳过，让reorganize_tool_messages处理
                                        pass
                                    
                                    # 策略2: 如果仍然没有ID，生成临时ID
                                    if not recovered_id and tool_use_name:
                                        import hashlib
                                        tool_args_str = str(tool_input) if tool_input else ""
                                        temp_id = f"temp_{hashlib.md5(f'{tool_use_name}_{tool_args_str}'.encode()).hexdigest()[:16]}"
                                        log.warning(
                                            f"[MESSAGE_CONVERTER] Generated temporary ID for tool_use without ID: "
                                            f"name={tool_use_name}, temp_id={temp_id}"
                                        )
                                        tool_use_id = temp_id
                                    
                                    # 策略3: 如果仍然没有ID，跳过这个tool_use（兜底）
                                    if not tool_use_id:
                                        log.warning(
                                            f"[MESSAGE_CONVERTER] Skipping tool_use without ID and name: "
                                            f"item={item}"
                                        )
                                        continue

                                if not _has_matching_tool_result(tool_use_id):
                                    log.warning(
                                        f"[ANTIGRAVITY] Skipping orphan tool_use in assistant content list: "
                                        f"tool_use_id={tool_use_id} has no corresponding tool_result."
                                    )
                                    continue
                                
                                tool_use_id_str = str(tool_use_id)
                                if tool_use_id_str in seen_tool_call_ids:
                                    continue
                                seen_tool_call_ids.add(tool_use_id_str)

                                if isinstance(tool_input, str):
                                    try:
                                        args_dict = json.loads(tool_input)
                                    except Exception:
                                        args_dict = {"query": tool_input}
                                else:
                                    args_dict = tool_input

                                content_parts.append({
                                    "functionCall": {
                                        "id": tool_use_id,
                                        "name": tool_use_name,
                                        "args": args_dict
                                    },
                                    # Gemini 3 要求 functionCall 必须包含 thoughtSignature
                                    "thoughtSignature": "skip_thought_signature_validator",
                                })
                        else:
                            # 非字典项，转换为文本
                            if item:
                                content_parts.append({"text": str(item)})
                else:
                    # 其他格式，尝试提取文本
                    extracted = extract_images_from_content(content)
                    if extracted["text"]:
                        content_parts.append({"text": extracted["text"]})
                    content_parts.extend(extracted["images"])

            # 添加工具调用
            if tool_calls:
                for tool_call in tool_calls:
                    tc_id, _ = _extract_tool_call_id_and_name(tool_call)
                    tc_function = getattr(tool_call, "function", None) if not isinstance(tool_call, dict) else tool_call.get("function")

                    # [FIX 2026-01-25] 渐进式修复：如果tool_call没有ID，尝试恢复
                    if not tc_id:
                        # 策略1: 尝试从tool_result_ids中通过name匹配恢复ID
                        tc_name = ""
                        if tc_function:
                            if isinstance(tc_function, dict):
                                tc_name = tc_function.get("name", "")
                            else:
                                tc_name = getattr(tc_function, "name", "")
                        
                        recovered_id = None
                        if tc_name:
                            # 检查后续消息中是否有匹配的tool_result
                            # 注意：这里无法直接检查，因为tool_result在后续消息中
                            # 所以暂时生成临时ID，让reorganize_tool_messages处理
                            import hashlib
                            func_args = ""
                            if tc_function:
                                if isinstance(tc_function, dict):
                                    func_args = str(tc_function.get("arguments", ""))
                                else:
                                    func_args = str(getattr(tc_function, "arguments", ""))
                            temp_id = f"temp_{hashlib.md5(f'{tc_name}_{func_args}'.encode()).hexdigest()[:16]}"
                            log.warning(
                                f"[MESSAGE_CONVERTER] Generated temporary ID for tool_call without ID: "
                                f"name={tc_name}, temp_id={temp_id}"
                            )
                            tc_id = temp_id
                        else:
                            # 策略2: 兜底 - 如果连name都没有，跳过
                            log.warning(
                                f"[MESSAGE_CONVERTER] Skipping tool_call without ID and name: "
                                f"tool_call={tool_call}"
                            )
                            continue

                    # [FIX 2026-01-20] 验证对应的 tool_result 是否存在
                    # 如果 tool_result 不存在，跳过这个 tool_use，避免 Claude API 返回 400 错误：
                    # "tool_use ids were found without tool_result blocks immediately after"
                    # 场景: Cursor 重试时可能发送不完整的历史消息，tool_use 存在但 tool_result 缺失
                    if tc_id:
                        if not _has_matching_tool_result(tc_id):
                            log.warning(f"[ANTIGRAVITY] Skipping orphan tool_use: "
                                       f"tool_call_id={tc_id} has no corresponding tool_result. "
                                       f"This may happen when conversation was interrupted during tool execution. "
                                       f"Filtering to avoid Claude API 400 error.")
                            continue

                        tc_id_str = str(tc_id)
                        if tc_id_str in seen_tool_call_ids:
                            continue
                        seen_tool_call_ids.add(tc_id_str)
                    else:
                        continue

                    if tc_function:
                        if isinstance(tc_function, dict):
                            func_name = tc_function.get("name", "")
                            func_args = tc_function.get("arguments", "{}")
                        else:
                            func_name = getattr(tc_function, "name", "")
                            func_args = getattr(tc_function, "arguments", "{}")

                        # 解析 arguments（可能是字符串）
                        if isinstance(func_args, str):
                            try:
                                args_dict = json.loads(func_args)
                            except Exception:
                                args_dict = {"query": func_args}
                        else:
                            args_dict = func_args

                        content_parts.append({
                            "functionCall": {
                                "id": tc_id,
                                "name": func_name,
                                "args": args_dict
                            },
                            # Gemini 3 要求 functionCall 必须包含 thoughtSignature
                            "thoughtSignature": "skip_thought_signature_validator",
                        })

            if content_parts:
                contents.append({"role": "model", "parts": content_parts})

        # 处理 tool 消息
        elif role == "tool":
            tool_call_id = _get_field(msg, "tool_call_id", None)
            tool_name = _get_field(msg, "name", "unknown")
            content = _get_field(msg, "content", "")

            # 验证必要字段
            if not tool_call_id:
                log.warning(f"[ANTIGRAVITY] Tool message missing tool_call_id at index {i}, skipping")
                continue  # 跳过无效的工具消息

            tool_call_id_variants = _get_tool_id_variants(tool_call_id)

            # [FIX 2026-01-20] 确保 tool_name 非空，避免 Gemini API 400 错误
            # 错误: "GenerateContentRequest.contents[4].parts[0].function_response.name: Name cannot be empty."
            # 场景: Cursor 重试时可能发送不完整的历史消息，tool 消息的 name 字段缺失
            if not tool_name or not str(tool_name).strip():
                # 尝试从映射中获取 name
                recovered_name = ""
                for variant in tool_call_id_variants:
                    if variant in tool_call_id_to_name:
                        recovered_name = tool_call_id_to_name[variant]
                        break
                if recovered_name:
                    tool_name = recovered_name
                    log.info(f"[ANTIGRAVITY] Recovered tool_name from mapping: {tool_name}")
                else:
                    # 最后的兜底: 使用 tool_call_id 作为 name
                    tool_name = f"tool_{tool_call_id}" if tool_call_id else "unknown_tool"
                    log.warning(f"[ANTIGRAVITY] Tool message missing name, using fallback: {tool_name}")

            # [FIX 2026-01-08] 验证对应的 tool_use 是否存在
            # 如果 tool_use 不存在，补建 tool_use，避免 Anthropic API 返回 400 错误：
            # "unexpected `tool_use_id` found in `tool_result` blocks"
            if not any(variant in tool_call_id_to_name for variant in tool_call_id_variants):
                synthetic_parts = [{
                    "functionCall": {
                        "id": tool_call_id,
                        "name": tool_name,
                        "args": {}
                    },
                    "thoughtSignature": "skip_thought_signature_validator",
                }]
                contents.append({"role": "model", "parts": synthetic_parts})
                for variant in tool_call_id_variants:
                    tool_call_id_to_name[variant] = tool_name
                log.warning(
                    f"[ANTIGRAVITY] Reconstructed missing tool_use for tool_result: "
                    f"tool_call_id={tool_call_id}, name={tool_name}, index={i}"
                )

            # 处理 content 为 None 的情况
            if content is None:
                content = ""
                log.debug(f"[ANTIGRAVITY] Tool message content is None, converting to empty string")

            # 记录工具消息信息（用于诊断）
            if not content:
                log.warning(f"[ANTIGRAVITY] Tool message has empty content: tool_call_id={tool_call_id}, name={tool_name}")
            else:
                content_preview = str(content)[:100] if content else ""
                log.debug(f"[ANTIGRAVITY] Tool message: tool_call_id={tool_call_id}, name={tool_name}, content_length={len(str(content))}, preview={content_preview}")

            # 确保 response.output 是有效的 JSON 可序列化值
            if not isinstance(content, (str, int, float, bool, type(None))):
                try:
                    content = json.dumps(content) if content else ""
                except Exception as e:
                    log.warning(f"[ANTIGRAVITY] Failed to serialize tool content: {e}, using str()")
                    content = str(content) if content else ""

            parts = [{
                "functionResponse": {
                    "id": tool_call_id,
                    "name": tool_name,
                    "response": {"output": content}
                }
            }]
            contents.append({"role": "user", "parts": parts})

    # [FIX 2026-01-11] 应用 ALLOWED_PART_KEYS 白名单过滤和尾随空格清理
    # 这是上游同步的关键修复，防止 cache_control 等不支持字段导致 400/429 错误
    contents = clean_contents(contents)

    # [FIX 2026-02-04] 在返回前应用 Gemini 格式工具链完整性检查
    # 解决 Cursor 中途中断后 400 tool_use_result_mismatch 错误
    contents = _ensure_gemini_tool_chain_integrity(contents)
    # [FIX 2026-02-08] 强制 functionCall/functionResponse 相邻配对
    # 解决上游 400: unexpected `tool_use_id` found in `tool_result` blocks（要求 previous message 对齐）
    contents = _ensure_gemini_tool_message_adjacency(contents)
    # 重排后再做一次完整性检查，确保不会遗留孤儿 functionCall
    contents = _ensure_gemini_tool_chain_integrity(contents)

    return contents


def _extract_function_id(obj: dict, is_call: bool = True) -> Optional[str]:
    """
    [FIX 2026-02-05] 从 functionCall 或 functionResponse 中提取 ID
    [FIX 2026-02-07] 增强：当没有标准 ID 字段时，使用 name 作为兜底标识

    支持多种 ID 字段名变体，确保不遗漏任何格式：
    - id: 标准 Gemini 格式
    - callId: 某些 API 变体
    - call_id: 下划线格式
    - tool_use_id: Anthropic 格式
    - name: 某些情况下用 name 作为 ID（兜底）

    Args:
        obj: functionCall 或 functionResponse 对象
        is_call: True 表示 functionCall，False 表示 functionResponse

    Returns:
        提取到的 ID，或 None
    """
    if not isinstance(obj, dict):
        return None

    # 按优先级检查各种可能的 ID 字段
    id_fields = ["id", "callId", "call_id", "tool_use_id"]

    for field in id_fields:
        val = obj.get(field)
        if val and str(val).strip():
            return str(val).strip()

    # [FIX 2026-02-07] 兜底：使用 name 字段作为标识
    # 问题场景：Cursor 中途中断时，某些工具调用可能只有 name 没有 id
    # 这会导致 _ensure_gemini_tool_chain_integrity 无法检测到孤儿工具调用
    # 结果：400 tool_use_result_mismatch 错误
    name = obj.get("name")
    if name and str(name).strip():
        # 添加前缀区分，避免与真实 ID 冲突
        return f"__name__{str(name).strip()}"

    return None


def _ensure_gemini_tool_chain_integrity(contents: List[Dict]) -> List[Dict]:
    """
    确保 Gemini 格式的工具链完整性

    检测并过滤孤儿 functionCall（没有对应的 functionResponse）

    [FIX 2026-02-04] 解决 Cursor 中途中断后 400 tool_use_result_mismatch 错误
    [FIX 2026-02-05] 增强：支持多种 ID 字段名变体，确保不遗漏孤儿检测

    问题场景:
    1. Cursor 用户手动强制停止对话时，tool_use 已发出但 tool_result 尚未返回
    2. 检查点机制没有保存工具调用信息，导致恢复时无法知道哪些工具调用需要匹配
    3. Gemini 格式转换后缺少工具链完整性检查，孤儿 functionCall 被发送给 Antigravity API
    4. API 检测到 tool_use 没有对应的 tool_result，返回 400 错误

    Args:
        contents: Gemini 格式的消息列表

    Returns:
        过滤后的消息列表，不包含孤儿 functionCall
    """
    import os

    # 环境变量控制开关（用于回滚）
    if os.environ.get("GEMINI_TOOL_CHAIN_CHECK", "true").lower() != "true":
        return contents

    if not contents:
        return contents

    # [FIX 2026-02-05] 收集所有 functionCall 和 functionResponse 的 ID
    # 使用增强的 ID 提取函数，支持多种字段名变体
    function_call_ids = set()
    function_call_id_to_index = {}  # 记录每个 ID 所在的 content 索引，用于调试
    function_response_ids = set()

    for idx, content in enumerate(contents):
        parts = content.get("parts", [])
        for part_idx, part in enumerate(parts):
            if isinstance(part, dict):
                if "functionCall" in part:
                    fc = part["functionCall"]
                    # [FIX 2026-02-05] 使用增强的 ID 提取
                    call_id = _extract_function_id(fc, is_call=True)
                    if call_id:
                        function_call_ids.add(call_id)
                        function_call_id_to_index[call_id] = (idx, part_idx)
                    else:
                        # 警告：发现没有 ID 的 functionCall
                        log.warning(
                            f"[MESSAGE_CONVERTER] Found functionCall without ID at content[{idx}].parts[{part_idx}]: "
                            f"name={fc.get('name', 'unknown')}, keys={list(fc.keys())}"
                        )
                if "functionResponse" in part:
                    fr = part["functionResponse"]
                    # [FIX 2026-02-05] 使用增强的 ID 提取
                    resp_id = _extract_function_id(fr, is_call=False)
                    if resp_id:
                        function_response_ids.add(resp_id)

    # 找出孤儿 functionCall
    orphan_calls = function_call_ids - function_response_ids

    if not orphan_calls:
        return contents

    log.warning(
        f"[MESSAGE_CONVERTER] Detected {len(orphan_calls)} orphan functionCalls in Gemini format, "
        f"filtering to avoid 400 error. IDs: {list(orphan_calls)[:3]}..."
    )

    # [FIX 2026-02-05] 增强日志：记录每个孤儿 ID 的位置信息
    for orphan_id in orphan_calls:
        if orphan_id in function_call_id_to_index:
            idx, part_idx = function_call_id_to_index[orphan_id]
            log.warning(
                f"[MESSAGE_CONVERTER] Orphan functionCall found: id={orphan_id}, "
                f"location=content[{idx}].parts[{part_idx}]"
            )

    # 过滤孤儿 functionCall
    cleaned_contents = []
    filtered_count = 0
    for content in contents:
        parts = content.get("parts", [])
        new_parts = []

        for part in parts:
            if isinstance(part, dict) and "functionCall" in part:
                fc = part["functionCall"]
                # [FIX 2026-02-05] 使用增强的 ID 提取函数，确保一致性
                call_id = _extract_function_id(fc, is_call=True)
                if call_id and call_id in orphan_calls:
                    log.info(f"[MESSAGE_CONVERTER] Filtering orphan functionCall: {call_id}")
                    filtered_count += 1
                    continue
            new_parts.append(part)

        if new_parts:
            new_content = content.copy()
            new_content["parts"] = new_parts
            cleaned_contents.append(new_content)
        elif parts:  # 原来有 parts 但过滤后为空
            # 添加占位符，保持消息结构
            cleaned_contents.append({
                "role": content.get("role", "model"),
                "parts": [{"text": "..."}]
            })

    log.info(
        f"[MESSAGE_CONVERTER] Tool chain integrity check complete: "
        f"filtered {len(orphan_calls)} orphan functionCalls, "
        f"contents count {len(contents)} -> {len(cleaned_contents)}"
    )

    return cleaned_contents


def _extract_function_id_strict(obj: Any) -> Optional[str]:
    """
    仅使用标准 ID 字段提取 functionCall/functionResponse ID。
    不使用 name 兜底，避免在顺序重排阶段误配对。
    """
    if not isinstance(obj, dict):
        return None
    for field in ("id", "callId", "call_id", "tool_use_id"):
        val = obj.get(field)
        if val and str(val).strip():
            return str(val).strip()
    return None


def _ensure_gemini_tool_message_adjacency(contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    强制 functionCall/functionResponse 相邻配对。

    后端约束：每个 tool_result(functionResponse) 必须紧跟其对应 tool_use(functionCall) 的下一条消息。
    仅做集合配对不足以满足该约束，因此需要在最终 contents 上进行重排。
    """
    import os

    if os.environ.get("GEMINI_TOOL_CHAIN_CHECK", "true").lower() != "true":
        return contents
    if not contents:
        return contents

    # 收集所有 functionResponse（按出现顺序排队）
    response_queues: Dict[str, List[Dict[str, Any]]] = {}
    flattened: List[Dict[str, Any]] = []
    for content in contents:
        role = content.get("role", "user")
        parts = content.get("parts", [])
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            flattened.append({"role": role, "part": part})
            if "functionResponse" in part:
                resp_id = _extract_function_id_strict(part.get("functionResponse", {}))
                if resp_id:
                    response_queues.setdefault(resp_id, []).append(part)

    reordered: List[Dict[str, Any]] = []
    paired_calls = 0
    skipped_calls = 0
    dropped_orphan_responses = 0

    for item in flattened:
        role = item["role"]
        part = item["part"]

        # functionResponse 由配对阶段插入，原位置统一跳过
        if "functionResponse" in part:
            continue

        if "functionCall" in part:
            call_id = _extract_function_id_strict(part.get("functionCall", {}))
            if call_id and response_queues.get(call_id):
                # 强制输出为相邻两条消息：model(functionCall) -> user(functionResponse)
                reordered.append({"role": "model", "parts": [part]})
                matched_response = response_queues[call_id].pop(0)
                reordered.append({"role": "user", "parts": [matched_response]})
                paired_calls += 1
            else:
                skipped_calls += 1
                log.warning(
                    f"[MESSAGE_CONVERTER] Skipping non-adjacent/orphan functionCall during adjacency enforcement: id={call_id}"
                )
            continue

        # 普通文本/图片等 part 原样保留（单 part 扁平化）
        reordered.append({"role": role, "parts": [part]})

    # 清理未被消费的 functionResponse（孤儿或无法建立紧邻关系）
    for resp_id, queue in response_queues.items():
        if queue:
            dropped_orphan_responses += len(queue)
            log.warning(
                f"[MESSAGE_CONVERTER] Dropping orphan/non-adjacent functionResponse(s): id={resp_id}, count={len(queue)}"
            )

    if paired_calls or skipped_calls or dropped_orphan_responses:
        log.info(
            f"[MESSAGE_CONVERTER] Adjacency enforcement complete: "
            f"paired_calls={paired_calls}, skipped_calls={skipped_calls}, "
            f"dropped_orphan_responses={dropped_orphan_responses}, "
            f"contents={len(contents)}->{len(reordered)}"
        )

    return reordered


def gemini_contents_to_antigravity_contents(gemini_contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将 Gemini 原生 contents 格式转换为 Antigravity contents 格式
    Gemini 和 Antigravity 的 contents 格式基本一致，只需要做少量调整
    """
    contents = []

    for content in gemini_contents:
        role = content.get("role", "user")
        parts = content.get("parts", [])

        contents.append({
            "role": role,
            "parts": parts
        })

    # [FIX 2026-01-11] 应用 ALLOWED_PART_KEYS 白名单过滤和尾随空格清理
    contents = clean_contents(contents)

    return contents


def antigravity_contents_to_openai_messages(contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将 Antigravity contents 格式转换为 OpenAI messages 格式

    [FIX 2026-01-26] 新增反向转换函数，用于 RetryCoordinator 调用其他后端时的格式转换

    Antigravity 格式:
    [
        {"role": "user", "parts": [{"text": "..."}]},
        {"role": "model", "parts": [{"text": "..."}, {"functionCall": {...}}]},
        {"role": "user", "parts": [{"functionResponse": {...}}]}
    ]

    OpenAI 格式:
    [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "...", "tool_calls": [...]},
        {"role": "tool", "content": "...", "tool_call_id": "..."}
    ]

    Args:
        contents: Antigravity 格式的消息列表

    Returns:
        OpenAI 格式的消息列表
    """
    # [FIX 2026-01-30] 移除错误的 from src.utils.logger import log
    # src.utils 是模块文件(utils.py)非包，src.utils.logger 不存在；使用模块级 log（第10行已导入）

    # [DEBUG] 记录输入
    log.debug(
        f"[CONVERSION] antigravity_contents_to_openai_messages called: "
        f"input_count={len(contents)}, "
        f"input_type={type(contents)}",
        tag="GATEWAY"
    )

    messages = []

    for idx, content in enumerate(contents):
        role = content.get("role", "user")
        parts = content.get("parts", [])

        # [DEBUG] 记录每个 content 的处理
        log.debug(
            f"[CONVERSION] Processing content[{idx}]: role={role}, parts_count={len(parts)}",
            tag="GATEWAY"
        )

        # 转换角色名称
        if role == "model":
            openai_role = "assistant"
        else:
            openai_role = role

        # 处理 parts
        text_parts = []
        tool_calls = []
        function_response = None

        for part_idx, part in enumerate(parts):
            if not isinstance(part, dict):
                log.debug(f"[CONVERSION] Skipping non-dict part[{part_idx}]: {type(part)}", tag="GATEWAY")
                continue

            # 文本内容
            if "text" in part:
                text_parts.append(part["text"])
                log.debug(f"[CONVERSION] Found text in part[{part_idx}]: length={len(part['text'])}", tag="GATEWAY")

            # 工具调用 (functionCall)
            elif "functionCall" in part:
                fc = part["functionCall"]
                tool_call = {
                    "id": fc.get("id", f"call_{len(tool_calls)}"),
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args", {}))
                    }
                }
                tool_calls.append(tool_call)
                log.debug(
                    f"[CONVERSION] Found functionCall in part[{part_idx}]: name={fc.get('name')}, id={fc.get('id')}",
                    tag="GATEWAY"
                )

            # 工具响应 (functionResponse)
            elif "functionResponse" in part:
                function_response = part["functionResponse"]
                log.debug(
                    f"[CONVERSION] Found functionResponse in part[{part_idx}]: id={function_response.get('id')}",
                    tag="GATEWAY"
                )

        # 构建 OpenAI 消息
        if function_response:
            # 工具响应消息
            response_output = function_response.get("response", {}).get("output", "")
            if isinstance(response_output, dict):
                response_output = json.dumps(response_output)

            message = {
                "role": "tool",
                "content": str(response_output),
                "tool_call_id": function_response.get("id", "")
            }
            messages.append(message)
            log.debug(f"[CONVERSION] Created tool message: tool_call_id={message['tool_call_id']}", tag="GATEWAY")
        else:
            # 普通消息
            message = {
                "role": openai_role,
                "content": "\n".join(text_parts) if text_parts else ""
            }

            # 添加工具调用
            if tool_calls:
                message["tool_calls"] = tool_calls
                log.debug(
                    f"[CONVERSION] Created {openai_role} message with {len(tool_calls)} tool_calls",
                    tag="GATEWAY"
                )
            else:
                log.debug(f"[CONVERSION] Created {openai_role} message with text only", tag="GATEWAY")

            messages.append(message)

    # [DEBUG] 记录输出
    log.debug(
        f"[CONVERSION] antigravity_contents_to_openai_messages completed: "
        f"output_count={len(messages)}",
        tag="GATEWAY"
    )

    return messages


def antigravity_tools_to_openai_tools(antigravity_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将 Antigravity tools 格式转换为 OpenAI tools 格式

    [FIX 2026-01-26] 新增反向转换函数，用于 RetryCoordinator 调用其他后端时的格式转换

    Antigravity 格式:
    [
        {
            "functionDeclarations": [
                {"name": "...", "description": "...", "parameters": {...}}
            ]
        }
    ]

    OpenAI 格式:
    [
        {
            "type": "function",
            "function": {"name": "...", "description": "...", "parameters": {...}}
        }
    ]

    Args:
        antigravity_tools: Antigravity 格式的工具列表

    Returns:
        OpenAI 格式的工具列表
    """
    openai_tools = []

    for tool in antigravity_tools:
        if not isinstance(tool, dict):
            continue

        # Antigravity 格式：{"functionDeclarations": [...]}
        function_declarations = tool.get("functionDeclarations", [])

        for func_decl in function_declarations:
            if not isinstance(func_decl, dict):
                continue

            openai_tool = {
                "type": "function",
                "function": {
                    "name": func_decl.get("name", ""),
                    "description": func_decl.get("description", ""),
                    "parameters": func_decl.get("parameters", {"type": "object", "properties": {}})
                }
            }
            openai_tools.append(openai_tool)

    return openai_tools
