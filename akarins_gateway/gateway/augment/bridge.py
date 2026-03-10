"""
Augment/Bugment → Gateway 中间转换层

职责：
1. 解析 Bugment 协议（chat_history、nodes、message、tool_definitions 等）
2. 统一 Bugment State 与 authoritative_history，得到有效历史
3. 输出符合 SCID/merge 假设的 OpenAI 格式消息
4. 转换 tool_definitions → OpenAI tools

设计原则：
- 耦合 Augment/Bugment 侧，不依赖 SCID/merge 内部实现
- 纯函数式转换，可单元测试
- 输出 GatewayReadyRequest，供 endpoints 直接交给 apply_scid_and_sanitization

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-01-30
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import json
import logging
import os

# 延迟导入，避免循环依赖
try:
    from akarins_gateway.core.log import log
except ImportError:
    log = logging.getLogger(__name__)

# 默认模型兜底（与 endpoints 一致）
BUGMENT_DEFAULT_MODEL = os.getenv("BUGMENT_DEFAULT_MODEL", "gpt-4.1").strip()


# ==================== 输出类型定义 ====================

@dataclass
class GatewayReadyRequest:
    """
    Bridge 转换后的网关就绪请求
    
    可直接交给 apply_scid_and_sanitization 使用
    """
    # 核心字段
    messages: List[Dict[str, Any]]          # OpenAI 格式消息，结构符合 merge 假设
    body: Dict[str, Any]                    # 完整请求体（含 messages、model、tools 等）
    model: str                              # 模型名称
    
    # 会话标识
    conversation_id: Optional[str] = None   # Bugment conversation_id
    scid_hint: Optional[str] = None         # 用于 SCID 提取的提示（通常等于 conversation_id）
    
    # Augment 特有
    is_augment: bool = True                 # 标记为 Augment 请求
    tools: Optional[List[Dict]] = None      # OpenAI 格式的 tools
    mode: Optional[str] = None              # CHAT / AGENT 模式
    disable_thinking: bool = False          # CHAT 模式需禁用 thinking
    
    # 原始数据（供流式回写使用）
    raw_body: Dict[str, Any] = field(default_factory=dict)


# ==================== 主入口函数 ====================

def convert_bugment_to_gateway(
    raw_body: Dict[str, Any],
    headers: Dict[str, str],
    *,
    state_manager: Optional[Any] = None,
    scid: Optional[str] = None,
    bugment_state_getter: Optional[callable] = None,
    authoritative_history_getter: Optional[callable] = None,
) -> GatewayReadyRequest:
    """
    将 Bugment 请求转换为网关可消费格式。

    Args:
        raw_body: Bugment 原始请求体
        headers: 请求头（用于 client 检测等）
        state_manager: 可选，ConversationStateManager 实例
        scid: 可选，用于 state 查询
        bugment_state_getter: 可选，获取 Bugment State 的函数 (conversation_id -> dict)
        authoritative_history_getter: 可选，获取权威历史的函数 (scid -> list)

    Returns:
        GatewayReadyRequest: 可直接交给 apply_scid_and_sanitization
    """
    if not isinstance(raw_body, dict):
        raw_body = {}
    
    # ========== Step 0: 保存 Bugment State（客户端发送时写入） ==========
    _save_bugment_state_to_storage(raw_body)
    
    # ========== Step 1: 提取基本字段 ==========
    conversation_id = raw_body.get("conversation_id")
    if isinstance(conversation_id, str):
        conversation_id = conversation_id.strip() or None
    
    model = _extract_model(raw_body)
    mode = _extract_mode(raw_body)
    disable_thinking = (mode == "CHAT")
    
    # ========== Step 2: 构建消息（调用 nodes_bridge 内部函数） ==========
    messages = _build_messages_from_bugment(raw_body)
    
    # ========== Step 3: Bugment State fallback ==========
    messages, model = _apply_bugment_state_fallback(
        messages=messages,
        model=model,
        conversation_id=conversation_id,
        raw_body=raw_body,
        bugment_state_getter=bugment_state_getter,
    )
    
    # ========== Step 4: authoritative_history 统一（Phase 3） ==========
    # 使用 scid 或 conversation_id 作为 authoritative 查询键（Augment 中二者常相同）
    lookup_key = scid or conversation_id
    messages = _apply_authoritative_history(
        messages=messages,
        lookup_key=lookup_key,
        raw_body=raw_body,
        state_manager=state_manager,
        authoritative_history_getter=authoritative_history_getter,
    )
    
    # ========== Step 5: 结构规范化（确保 merge 兼容） ==========
    messages = _normalize_message_structure(messages)
    
    # ========== Step 6: 转换 tool_definitions → OpenAI tools ==========
    tools = _convert_tool_definitions(raw_body)
    
    # ========== Step 7: 构建 body（Phase 4 完整输出） ==========
    body = _build_gateway_body(
        raw_body=raw_body,
        messages=messages,
        model=model,
        tools=tools,
        disable_thinking=disable_thinking,
        conversation_id=conversation_id,
    )
    
    # ========== Step 8: 返回结果 ==========
    return GatewayReadyRequest(
        messages=messages,
        body=body,
        model=model,
        conversation_id=conversation_id,
        scid_hint=conversation_id,  # Augment 使用 conversation_id 作为 SCID hint
        is_augment=True,
        tools=tools,
        mode=mode,
        disable_thinking=disable_thinking,
        raw_body=raw_body,
    )


# ==================== 内部实现函数 ====================

def _extract_model(raw_body: Dict[str, Any]) -> str:
    """
    提取模型名称，支持 third_party_override
    """
    model = raw_body.get("model")
    
    # 尝试从 third_party_override 获取
    third_party = raw_body.get("third_party_override")
    if (not model or (isinstance(model, str) and not model.strip())) and isinstance(third_party, dict):
        override_model = third_party.get("provider_model_name") or third_party.get("providerModelName")
        if override_model and isinstance(override_model, str):
            model = override_model
    
    # 清理模型名称
    if isinstance(model, str):
        # 移除常见前缀
        if "/" in model:
            model = model.split("/")[-1]
        model = model.strip()
    
    return model or ""


def _extract_mode(raw_body: Dict[str, Any]) -> Optional[str]:
    """
    提取 CHAT/AGENT 模式
    """
    mode = raw_body.get("mode")
    if isinstance(mode, str):
        return mode.strip().upper() or None
    return None


def _save_bugment_state_to_storage(raw_body: Dict[str, Any]) -> None:
    """
    将客户端发送的 chat_history / model 保存到 Bugment State
    
    与 endpoints L165-173 逻辑一致：当请求体包含有效数据时写入 state
    """
    try:
        from .state import bugment_conversation_state_put
    except ImportError:
        return
    
    conversation_id = raw_body.get("conversation_id")
    chat_history_field = raw_body.get("chat_history")
    model_field = raw_body.get("model")
    
    if isinstance(chat_history_field, list) and chat_history_field:
        bugment_conversation_state_put(conversation_id, chat_history=chat_history_field)
    
    if isinstance(model_field, str) and model_field.strip():
        bugment_conversation_state_put(conversation_id, model=model_field.strip())


def _build_messages_from_bugment(raw_body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    从 Bugment raw_body 构建 OpenAI 消息
    
    委托给 nodes_bridge 内部函数，保持行为一致
    """
    try:
        # 导入 nodes_bridge 的解析函数
        from .nodes_bridge import (
            augment_chat_history_to_messages,
            extract_tool_result_nodes,
            extract_tool_result_nodes_from_history,
            prepend_bugment_guidance_system_message,
        )
        from .state import bugment_tool_state_get
    except ImportError as e:
        log.warning(f"[BRIDGE] Failed to import nodes_bridge functions: {e}")
        # 降级：返回基本消息
        message = raw_body.get("message", "")
        if isinstance(message, str) and message.strip():
            return [{"role": "user", "content": message}]
        return [{"role": "user", "content": "Hello"}]
    
    conversation_id = raw_body.get("conversation_id")
    messages: List[Dict[str, Any]] = []
    
    # Step 1: 解析 chat_history
    chat_history = raw_body.get("chat_history")
    messages.extend(augment_chat_history_to_messages(chat_history))
    
    # Step 2: 解析 tool_result nodes
    tool_results = extract_tool_result_nodes(raw_body.get("nodes"))
    history_tool_results = extract_tool_result_nodes_from_history(chat_history, latest_only=True)
    
    # 合并 tool_results
    if history_tool_results:
        if tool_results:
            seen_ids = {
                tr.get("tool_use_id")
                for tr in tool_results
                if isinstance(tr, dict) and isinstance(tr.get("tool_use_id"), str)
            }
            for tr in history_tool_results:
                tool_use_id = tr.get("tool_use_id") if isinstance(tr, dict) else None
                if isinstance(tool_use_id, str) and tool_use_id not in seen_ids:
                    tool_results.append(tr)
                    seen_ids.add(tool_use_id)
        else:
            tool_results = history_tool_results
    
    # Step 3: 构建 tool 相关消息
    if tool_results:
        assistant_tool_calls = []
        tool_messages = []
        fallback_user_notes = []
        
        for tr in tool_results:
            tool_use_id = tr.get("tool_use_id")
            content = tr.get("content")
            is_error = tr.get("is_error")
            
            if not isinstance(tool_use_id, str) or not tool_use_id.strip():
                continue
            
            # 转换 content 为字符串
            if isinstance(content, str):
                text = content
            elif content is None:
                text = ""
            else:
                try:
                    text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    text = str(content)
            
            # 从 Bugment State 获取 tool 信息
            state = bugment_tool_state_get(conversation_id, tool_use_id)
            if isinstance(state, dict) and isinstance(state.get("tool_name"), str):
                assistant_tool_calls.append({
                    "id": tool_use_id,
                    "type": "function",
                    "function": {
                        "name": state["tool_name"],
                        "arguments": state.get("arguments_json") or "{}"
                    },
                })
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
    
    # Step 4: 添加当前 user message
    current_message = raw_body.get("message")
    if isinstance(current_message, str) and current_message.strip():
        messages.append({"role": "user", "content": current_message})
    
    # Step 5: 确保至少有一条消息
    if not messages:
        messages = [{"role": "user", "content": raw_body.get("message") or "Hello"}]
    
    # Step 6: Prepend guidance system message
    messages = prepend_bugment_guidance_system_message(raw_body, messages)
    
    return messages


def _apply_bugment_state_fallback(
    messages: List[Dict[str, Any]],
    model: str,
    conversation_id: Optional[str],
    raw_body: Dict[str, Any],
    bugment_state_getter: Optional[callable] = None,
) -> tuple:
    """
    应用 Bugment State fallback
    
    当 chat_history 或 model 为空时，从 Bugment State 补全
    """
    if bugment_state_getter is None:
        try:
            from .state import bugment_conversation_state_get
            bugment_state_getter = bugment_conversation_state_get
        except ImportError:
            return messages, model
    
    if not conversation_id:
        return messages, model
    
    state = bugment_state_getter(conversation_id)
    if not isinstance(state, dict):
        return messages, model
    
    # Model fallback
    if not model or (isinstance(model, str) and not model.strip()):
        fallback_model = state.get("model")
        if isinstance(fallback_model, str) and fallback_model.strip():
            model = fallback_model.strip()
            log.info(f"[BRIDGE] Model fallback from Bugment State: {model}")
        elif BUGMENT_DEFAULT_MODEL:
            model = BUGMENT_DEFAULT_MODEL
            try:
                from .state import bugment_conversation_state_put
                bugment_conversation_state_put(conversation_id, model=model)
            except ImportError:
                pass
            log.warning(f"[BRIDGE] Model missing; using default fallback: {model}")
    
    # chat_history fallback
    # 判断当前消息是否过短（仅 system + 1 条 user，或更少）
    non_system_count = sum(1 for m in messages if m.get("role") != "system")
    if non_system_count <= 1:
        fallback_history = state.get("chat_history")
        if isinstance(fallback_history, list) and fallback_history:
            try:
                from .nodes_bridge import augment_chat_history_to_messages, prepend_bugment_guidance_system_message
                
                # 重新构建消息
                history_messages = augment_chat_history_to_messages(fallback_history)
                
                # 提取当前 user message
                current_user = None
                for m in reversed(messages):
                    if m.get("role") == "user":
                        current_user = m
                        break
                
                if history_messages:
                    messages = history_messages
                    if current_user and current_user not in messages:
                        messages.append(current_user)
                    messages = prepend_bugment_guidance_system_message(raw_body, messages)
                    log.info(f"[BRIDGE] chat_history fallback from Bugment State: {len(history_messages)} messages")
            except Exception as e:
                log.warning(f"[BRIDGE] chat_history fallback failed: {e}")
    
    return messages, model


def _apply_authoritative_history(
    messages: List[Dict[str, Any]],
    lookup_key: Optional[str],
    raw_body: Dict[str, Any],
    state_manager: Optional[Any],
    authoritative_history_getter: Optional[callable],
) -> List[Dict[str, Any]]:
    """
    应用 authoritative_history 统一
    
    有效历史解析策略（Phase 3）：
    1. client 完整（non_system_count > 1）→ 用 client
    2. 否则 authoritative 优先：若 authoritative 存在，以 authoritative 为基础 + 追加当前 user
    3. Bugment State 已在 Step 3 兜底
    """
    if not lookup_key or not lookup_key.strip():
        return messages

    # 判断 client 消息是否「完整」（多轮对话）
    non_system_count = sum(1 for m in messages if m.get("role") != "system")
    if non_system_count > 1:
        return messages

    # 获取 authoritative_history
    authoritative = None
    if authoritative_history_getter is not None:
        try:
            authoritative = authoritative_history_getter(lookup_key)
        except Exception as e:
            log.warning(f"[BRIDGE] authoritative_history_getter failed: {e}")
    elif state_manager is not None and hasattr(state_manager, "get_authoritative_history"):
        try:
            authoritative = state_manager.get_authoritative_history(lookup_key)
        except Exception as e:
            log.warning(f"[BRIDGE] state_manager.get_authoritative_history failed: {e}")

    if not isinstance(authoritative, list) or not authoritative:
        return messages

    # 以 authoritative 为基础 + 追加当前 user message
    try:
        from .nodes_bridge import prepend_bugment_guidance_system_message
    except ImportError:
        prepend_bugment_guidance_system_message = None

    current_user = None
    for m in reversed(messages):
        if m.get("role") == "user":
            current_user = m
            break

    merged = list(authoritative)
    if current_user and (not merged or merged[-1] != current_user):
        merged.append(current_user)

    if prepend_bugment_guidance_system_message and raw_body:
        merged = prepend_bugment_guidance_system_message(raw_body, merged)

    log.info(f"[BRIDGE] authoritative_history applied: {len(authoritative)} base + current user")
    return merged


def _normalize_message_structure(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    规范化消息结构，确保与 merge 假设兼容（Phase 3）
    
    目标结构：
    - [system?, user, assistant, (tool)*, user, assistant, ..., user]
    - system 置顶；user/assistant/tool 保持原有顺序
    - 过滤无效 role
    """
    if not messages or not isinstance(messages, list):
        return [{"role": "user", "content": "Hello"}]

    valid_roles = ("system", "user", "assistant", "tool")
    filtered = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in valid_roles:
            continue
        filtered.append(msg)

    if not filtered:
        return [{"role": "user", "content": "Hello"}]

    # system 置顶（merge 假设 system 在首位）
    system_msgs = [m for m in filtered if m.get("role") == "system"]
    non_system = [m for m in filtered if m.get("role") != "system"]
    normalized = system_msgs + non_system

    return normalized


def _convert_tool_definitions(raw_body: Dict[str, Any]) -> Optional[List[Dict]]:
    """
    将 Augment tool_definitions 转换为 OpenAI tools 格式
    """
    tool_definitions = raw_body.get("tool_definitions")
    if not isinstance(tool_definitions, list) or not tool_definitions:
        return None
    
    try:
        from akarins_gateway.augment_compat.tools_bridge import (
            parse_tool_definitions_from_request,
            convert_tools_to_openai,
        )
        
        augment_tools = parse_tool_definitions_from_request(tool_definitions)
        if augment_tools:
            tools = convert_tools_to_openai(augment_tools)
            if tools:
                log.debug(f"[BRIDGE] Converted tool_definitions to OpenAI tools: count={len(tools)}")
                return tools
    except ImportError:
        log.warning("[BRIDGE] augment_compat.tools_bridge not available, skipping tool conversion")
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to convert tool_definitions: {e}")
    
    return None


def _build_gateway_body(
    raw_body: Dict[str, Any],
    messages: List[Dict[str, Any]],
    model: str,
    tools: Optional[List[Dict]],
    disable_thinking: bool,
    conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    构建网关请求体（Phase 4 完整 body，供 apply_scid 使用）
    """
    body: Dict[str, Any] = {
        "messages": messages,
        "stream": True,
    }
    
    if model:
        body["model"] = model
    
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    
    # conversation_id 供 SCID 提取（extract_or_generate_scid 使用 body.conversation_id）
    if conversation_id:
        body["conversation_id"] = conversation_id
    
    # CHAT 模式禁用 thinking
    if disable_thinking:
        # 不添加 thinking 配置
        pass
    else:
        # AGENT 模式可保留 thinking 配置（若 raw_body 中有）
        for key in ("thinking", "thinking_budget", "thinking_level", "thinking_config"):
            if key in raw_body:
                body[key] = raw_body[key]
    
    return body


# ==================== 供 endpoints 调用的兼容函数 ====================

def apply_bugment_state_to_raw_body(raw_body: Dict[str, Any]) -> None:
    """
    将 Bugment State 逻辑应用到 raw_body（原地修改）
    
    与 endpoints L165-189 逻辑等价：
    1. 当客户端发送 chat_history/model 时，保存到 state
    2. 当 chat_history 为空时，从 state 补全到 raw_body["chat_history"]
    3. 当 model 为空时，从 state 或 BUGMENT_DEFAULT_MODEL 补全到 raw_body["model"]
    
    用于 Phase 2.3：将 endpoints 中分散的 state 逻辑迁移到 bridge 后，endpoints 调用此函数简化代码。
    """
    if not isinstance(raw_body, dict):
        return
    try:
        from .state import bugment_conversation_state_put, bugment_conversation_state_get
    except ImportError:
        return
    
    conversation_id = raw_body.get("conversation_id")
    chat_history_field = raw_body.get("chat_history")
    model_field = raw_body.get("model")
    
    # 1. 保存到 state（客户端发送时）
    if isinstance(chat_history_field, list) and chat_history_field:
        bugment_conversation_state_put(conversation_id, chat_history=chat_history_field)
    
    if isinstance(model_field, str) and model_field.strip():
        bugment_conversation_state_put(conversation_id, model=model_field.strip())
    else:
        # 2. model fallback
        state = bugment_conversation_state_get(conversation_id)
        fallback_model = state.get("model") if isinstance(state, dict) else None
        if isinstance(fallback_model, str) and fallback_model.strip():
            raw_body["model"] = fallback_model.strip()
        elif BUGMENT_DEFAULT_MODEL:
            raw_body["model"] = BUGMENT_DEFAULT_MODEL
            bugment_conversation_state_put(conversation_id, model=BUGMENT_DEFAULT_MODEL)
            log.warning(f"[BRIDGE] Model missing; using default fallback: {BUGMENT_DEFAULT_MODEL}")
    
    if not isinstance(chat_history_field, list) or not chat_history_field:
        # 3. chat_history fallback
        state = bugment_conversation_state_get(conversation_id)
        fallback_history = state.get("chat_history") if isinstance(state, dict) else None
        if isinstance(fallback_history, list) and fallback_history:
            raw_body["chat_history"] = fallback_history


# ==================== 导出 ====================

__all__ = [
    "GatewayReadyRequest",
    "convert_bugment_to_gateway",
    "apply_bugment_state_to_raw_body",
]
