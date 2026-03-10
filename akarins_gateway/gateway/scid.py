"""
SCID 架构集成模块

提供会话ID（SCID）生成、消息净化和状态回写功能。

作者: 浮浮喵 (Claude Opus 4.5)
创建日期: 2026-01-23
迁移自: src/unified_gateway_router.py (4806-5350行)
"""

import json
import os
import re
import uuid
from collections import OrderedDict
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from akarins_gateway.core.log import log

__all__ = [
    "apply_scid_and_sanitization",
    "extract_signature_from_response",
    "writeback_non_streaming_response",
    "wrap_stream_with_writeback",
    "save_intermediate_state",  # [NEW 2026-02-02] 中间状态保存函数
    "update_checkpoint_signature",  # [NEW 2026-02-04] 实时更新检查点签名
    "get_checkpoint_interval",  # [NEW 2026-02-04] 动态检查点间隔计算
]


# ==================== [NEW 2026-02-02] 中间状态检查点机制 ====================
# 用于解决流中断时签名丢失导致 thinking 功能失效的问题
# 核心思路：实时增量缓存，而非流结束时批量回写
# ==========================================================================

# ==================== [FIX 2026-02-04] 动态检查点保存间隔 ====================
# 根据流的特性动态调整检查点保存频率：
# - 开始阶段更频繁保存（签名可能很快出现）
# - 后期可以降低频率（减少IO开销）
# ==========================================================================
CHECKPOINT_INTERVAL_INITIAL = 2   # [FIX 2026-02-04] 前50个chunk，每2个保存一次（更频繁以捕获工具调用）
CHECKPOINT_INTERVAL_NORMAL = 5    # 50-200个chunk，每5个保存一次
CHECKPOINT_INTERVAL_LATE = 10     # 200个chunk之后，每10个保存一次


def get_checkpoint_interval(chunk_count: int) -> int:
    """
    [FIX 2026-02-04] 根据chunk数量动态计算检查点保存间隔

    策略：
    - 前50个chunk：每5个保存一次（签名可能很快出现）
    - 50-200个chunk：每10个保存一次
    - 200个chunk之后：每20个保存一次（减少IO开销）

    Args:
        chunk_count: 当前chunk计数

    Returns:
        检查点保存间隔
    """
    if chunk_count < 50:
        return CHECKPOINT_INTERVAL_INITIAL
    elif chunk_count < 200:
        return CHECKPOINT_INTERVAL_NORMAL
    else:
        return CHECKPOINT_INTERVAL_LATE


# [FIX 2026-02-02 B4] 使用模块级单例避免重复创建实例
import threading

_state_manager_instance = None
_state_manager_lock = threading.Lock()

# [FIX 2026-02-02 C1/C2] 使用字典按会话存储签名，并添加线程锁
# [FIX 2026-02-04] 签名缓存Map，使用OrderedDict实现LRU
_signature_cache_map: OrderedDict[str, str] = OrderedDict()  # scid -> last_signature
_signature_lock = threading.Lock()
MAX_SIGNATURE_CACHE_SIZE = 1000  # 最大缓存条目数


def _get_state_manager():
    """
    获取 StateManager 单例实例

    [FIX 2026-02-03] 修复 Double-Checked Locking 反模式
    问题：原实现在第一层检查时无锁保护，高并发下可能导致多次初始化
    修复：使用 with 语句确保锁内完成所有检查和初始化
    """
    global _state_manager_instance

    # [FIX] 始终在锁内进行检查，避免竞态条件
    with _state_manager_lock:
        if _state_manager_instance is not None:
            return _state_manager_instance

        try:
            from akarins_gateway.ide_compat.state_manager import ConversationStateManager
            from akarins_gateway.cache.signature_database import SignatureDatabase
            try:
                db = SignatureDatabase()
                _state_manager_instance = ConversationStateManager(db)
            except Exception as db_err:
                log.warning(f"[SCID] SignatureDatabase init failed, using None: {db_err}", tag="GATEWAY")
                _state_manager_instance = ConversationStateManager(None)

            log.debug("[SCID] StateManager singleton initialized successfully", tag="GATEWAY")
            return _state_manager_instance
        except Exception as e:
            log.warning(f"[SCID] Failed to create StateManager: {e}", tag="GATEWAY")
            return None


def _extract_first_user_text(messages: List[Dict[str, Any]]) -> str:
    """提取第一条 user 文本（用于会话边界判定）。"""
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content.strip()
            if text:
                return text[:200]
            continue
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    t = str(item.get("text", "")).strip()
                    if t:
                        parts.append(t)
            if parts:
                return " ".join(parts)[:200]
    return ""


def _has_tool_context(messages: List[Dict[str, Any]]) -> bool:
    """检测消息列表中是否包含工具调用上下文。"""
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            return True
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and len(tool_calls) > 0:
            return True
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") in ("tool_use", "tool_result"):
                    return True
        parts = msg.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if "functionCall" in part or "functionResponse" in part:
                    return True
    return False


def _count_tool_events(messages: List[Dict[str, Any]]) -> int:
    """统计消息中的工具相关事件数，用于识别“重工具链旧状态”。"""
    if not isinstance(messages, list):
        return 0
    count = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool":
            count += 1
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            count += len(tool_calls)
        content = msg.get("content")
        if isinstance(content, list):
            count += sum(
                1 for item in content
                if isinstance(item, dict) and item.get("type") in ("tool_use", "tool_result")
            )
        parts = msg.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if "functionCall" in part:
                    count += 1
                if "functionResponse" in part:
                    count += 1
    return count


def _should_reset_stale_scid_state(
    state: Any,
    incoming_messages: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """
    判断是否应把当前 SCID 视为“新会话误命中旧缓存”并重置。

    场景：新开 Cursor 对话但首问相似，命中旧 SCID，导致旧工具链被合并。
    """
    enabled = os.getenv("SCID_STALE_BOUNDARY_RESET_ENABLED", "true").strip().lower() == "true"
    if not enabled:
        return False, ""

    if not state or not isinstance(incoming_messages, list):
        return False, ""

    incoming_count = len(incoming_messages)
    max_incoming = int(os.getenv("SCID_STALE_INCOMING_MAX_MESSAGES", "3"))
    # [FIX 2026-02-08] 默认阈值下调，避免“旧会话仅 7 条”时不触发隔离
    min_auth_messages = int(os.getenv("SCID_STALE_AUTH_MIN_MESSAGES", "4"))
    min_auth_tool_events = int(os.getenv("SCID_STALE_AUTH_MIN_TOOL_EVENTS", "2"))
    mismatch_min_auth_messages = int(os.getenv("SCID_STALE_MISMATCH_MIN_AUTH_MESSAGES", "2"))

    if incoming_count == 0 or incoming_count > max_incoming:
        return False, ""

    # 新会话起手通常没有工具上下文；若已带工具上下文则更可能是续聊，不重置
    if _has_tool_context(incoming_messages):
        return False, ""

    authoritative = getattr(state, "authoritative_history", None) or []
    auth_count = len(authoritative)

    auth_tool_events = _count_tool_events(authoritative)
    incoming_first = _extract_first_user_text(incoming_messages)
    auth_first = _extract_first_user_text(authoritative)
    incoming_roles = [str((m or {}).get("role", "")).lower() for m in incoming_messages if isinstance(m, dict)]
    incoming_all_user = bool(incoming_roles) and all(r == "user" for r in incoming_roles)

    # 首问明确不同：对于“短输入 + 纯用户起手”场景，优先判定为新会话边界
    if (
        auth_count >= mismatch_min_auth_messages
        and incoming_all_user
        and incoming_first
        and auth_first
        and incoming_first != auth_first
    ):
        return True, (
            f"first_user_mismatch incoming='{incoming_first[:80]}' auth='{auth_first[:80]}', "
            f"incoming_count={incoming_count}, auth_count={auth_count}, auth_tool_events={auth_tool_events}"
        )

    if auth_count < min_auth_messages:
        return False, ""

    # 即使首问相同/相似，只要“新请求极短 + 旧状态重工具链”也视为缓存误命中
    if auth_tool_events >= min_auth_tool_events:
        return True, (
            f"short_incoming_vs_heavy_auth incoming_count={incoming_count}, auth_count={auth_count}, "
            f"auth_tool_events={auth_tool_events}, first_user='{incoming_first[:80]}'"
        )

    return False, ""


def save_intermediate_state(
    scid: str,
    thinking_content: str = None,
    partial_response: str = None,
    signature: str = None,
    timestamp: float = None,
    tool_calls: list = None,  # [FIX 2026-02-04] 新增参数，保存流中断时的工具调用信息
    state_manager = None  # [FIX B4] 允许传入已有实例
) -> None:
    """
    保存中间状态检查点

    用于在流中断时保存已收到的内容，供下次请求恢复

    Args:
        scid: 会话 ID
        thinking_content: 已收到的 thinking 内容
        partial_response: 已收到的响应内容
        signature: 已收到的签名（如果有）
        timestamp: 保存时间戳
        tool_calls: [FIX 2026-02-04] 已收到的工具调用列表（用于中断恢复时识别孤儿 tool_use）
        state_manager: 可选的 StateManager 实例（避免重复创建）
    """
    import time as time_module

    if not scid:
        return

    try:
        # [FIX B4] 优先使用传入的实例，否则使用单例
        manager = state_manager or _get_state_manager()
        if manager is None:
            return

        checkpoint = {
            "thinking_content": thinking_content,
            "partial_response": partial_response,
            "signature": signature,
            "timestamp": timestamp or time_module.time(),
            "is_complete": False,  # 标记为未完成
            "tool_calls": tool_calls or []  # [FIX 2026-02-04] 保存工具调用信息
        }

        manager.save_checkpoint(scid, checkpoint)
        log.debug(
            f"[SCID] Intermediate state saved: scid={scid[:16]}..., "
            f"has_thinking={thinking_content is not None}, "
            f"has_signature={signature is not None}, "
            f"tool_calls_count={len(tool_calls) if tool_calls else 0}",
            tag="GATEWAY"
        )
    except Exception as e:
        log.warning(f"[SCID] Failed to save intermediate state: {e}", tag="GATEWAY")


def cache_signature_if_new(scid: str, signature: str) -> bool:
    """
    [FIX 2026-02-02 C1/C2] 仅在签名变化时缓存，避免重复写入

    修复：
    - C1: 添加线程锁保护
    - C2: 使用字典按会话存储，避免跨会话污染
    - [FIX 2026-02-04] 添加LRU策略防止内存泄漏

    Args:
        scid: 会话 ID
        signature: 签名

    Returns:
        True 如果缓存成功，False 如果跳过（重复签名）
    """
    if not scid or not signature:
        return False

    with _signature_lock:
        # 检查该会话是否已缓存相同签名
        if _signature_cache_map.get(scid) == signature:
            return False

        try:
            from akarins_gateway.signature_cache import cache_session_signature
            cache_session_signature(scid, signature, "")

            # [FIX 2026-02-04] LRU策略：如果已存在，先删除再添加（移到末尾）
            if scid in _signature_cache_map:
                del _signature_cache_map[scid]
            _signature_cache_map[scid] = signature

            # [FIX 2026-02-04] 容量限制：超过最大容量时清理最旧的条目
            while len(_signature_cache_map) > MAX_SIGNATURE_CACHE_SIZE:
                oldest_scid, _ = _signature_cache_map.popitem(last=False)
                log.debug(
                    f"[SCID] Signature cache evicted (LRU): scid={oldest_scid[:16]}...",
                    tag="GATEWAY"
                )

            log.debug(
                f"[SCID] Signature cached (new): scid={scid[:16]}..., sig_len={len(signature)}, "
                f"cache_size={len(_signature_cache_map)}",
                tag="GATEWAY"
            )
            return True
        except Exception as e:
            log.warning(f"[SCID] Signature cache failed: {e}", tag="GATEWAY")
            return False


def update_checkpoint_signature(scid: str, signature: str, state_manager=None) -> bool:
    """
    [FIX 2026-02-04] 实时更新检查点中的签名

    当从流中提取到签名时，立即更新已保存的检查点，
    解决签名与检查点时序不一致的问题。

    问题背景：
    - 检查点每10个chunk保存一次
    - 签名通常在thinking block结束时才出现
    - 早期检查点没有签名，流中断时无签名可恢复

    Args:
        scid: 会话 ID
        signature: 签名
        state_manager: 可选的 StateManager 实例

    Returns:
        True 如果更新成功
    """
    if not scid or not signature:
        return False

    try:
        manager = state_manager or _get_state_manager()
        if manager is None:
            return False

        # 检查是否有 get_checkpoint 方法
        if not hasattr(manager, 'get_checkpoint'):
            return False

        # 获取现有检查点
        checkpoint = manager.get_checkpoint(scid)
        if checkpoint is None:
            # 没有检查点，无需更新
            return False

        # 检查是否需要更新
        if checkpoint.get("signature") == signature:
            return False  # 签名相同，无需更新

        # 更新签名
        checkpoint["signature"] = signature
        manager.save_checkpoint(scid, checkpoint)

        log.debug(
            f"[SCID] Checkpoint signature updated: scid={scid[:16]}..., sig_len={len(signature)}",
            tag="GATEWAY"
        )
        return True

    except Exception as e:
        log.warning(f"[SCID] Failed to update checkpoint signature: {e}", tag="GATEWAY")
        return False


# ==================== [FIX 2026-02-02 M1] 拆分辅助函数 ====================
# 将 wrap_stream_with_writeback 的内部逻辑拆分为多个私有函数
# =========================================================================

def _parse_sse_chunk(chunk) -> Optional[dict]:
    """解析 SSE 格式的 chunk，返回 JSON 数据"""
    try:
        if isinstance(chunk, (str, bytes)):
            chunk_str = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for line in chunk_str.split("\n"):
                line = line.strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    return json.loads(line[6:])
    except Exception:
        pass
    return None


def _process_thinking_block(
    block: dict,
    scid: str,
    collected_thinking: list,
    last_signature: Optional[str],
    last_thinking_block: Optional[dict]
) -> Tuple[Optional[str], Optional[dict]]:
    """
    处理 thinking block，提取签名并缓存

    Returns:
        (updated_signature, updated_thinking_block)
    """
    # 填充 collected_thinking 列表
    thinking_text = block.get("thinking", "")
    if thinking_text:
        collected_thinking.append(thinking_text)

    sig = block.get("thoughtSignature") or block.get("signature")
    if sig and len(sig) > 50 and sig != "skip_thought_signature_validator":
        # 保存完整的thinking块
        updated_block = block.copy()
        # 归一化签名字段
        if "signature" in updated_block and "thoughtSignature" not in updated_block:
            updated_block["thoughtSignature"] = sig

        # 使用去重函数缓存签名
        if scid and sig:
            cache_signature_if_new(scid, sig)

        return sig, updated_block

    return last_signature, last_thinking_block


def _should_save_checkpoint(chunk_count: int) -> bool:
    """
    [FIX 2026-02-04] 判断是否应该保存检查点（动态间隔）

    使用动态间隔策略：
    - 前50个chunk：每5个保存一次
    - 50-200个chunk：每10个保存一次
    - 200个chunk之后：每20个保存一次

    Args:
        chunk_count: 当前chunk计数

    Returns:
        是否应该保存检查点
    """
    if chunk_count <= 0:
        return False
    interval = get_checkpoint_interval(chunk_count)
    return chunk_count % interval == 0


def extract_signature_from_response(result: Dict) -> Tuple[Optional[Dict], Optional[str]]:
    """
    从非流式响应中提取 assistant 消息和签名

    Args:
        result: OpenAI 格式的响应字典

    Returns:
        (assistant_message, signature) 元组
    """
    if not isinstance(result, dict):
        return None, None

    choices = result.get("choices", [])
    if not choices:
        return None, None

    message = choices[0].get("message", {})
    if not message or message.get("role") != "assistant":
        return None, None

    # 提取签名（从 content blocks 中查找 thinking block）
    signature = None
    content = message.get("content")

    if isinstance(content, list):
        for block in content:
            # [FIX 2026-01-25] 兼容两种thinking块格式
            # - Anthropic格式: {"type": "thinking", "thoughtSignature": "..."}
            # - Gemini格式: {"thought": true, "thoughtSignature": "..."}
            is_thinking_block = False
            if isinstance(block, dict):
                is_thinking_block = (
                    block.get("type") in ("thinking", "redacted_thinking") or  # Anthropic格式
                    block.get("thought") is True  # Gemini格式
                )
            
            if is_thinking_block:
                # 兼容两种字段名，统一存为 thoughtSignature
                sig = block.get("thoughtSignature") or block.get("signature")
                if sig and isinstance(sig, str) and len(sig) > 50:
                    if sig != "skip_thought_signature_validator":
                        signature = sig
                        # 归一化：确保 block 中使用 thoughtSignature
                        if "signature" in block and "thoughtSignature" not in block:
                            block["thoughtSignature"] = sig
                        break

    return message, signature


def writeback_non_streaming_response(
    result: Dict,
    scid: str,
    state_manager,
    request_messages: list
) -> None:
    """
    非流式响应回写：提取签名并更新权威历史

    只在成功完成一次 assistant 输出后写回，失败/中断不污染 last_signature

    Args:
        result: 上游响应
        scid: 会话 ID
        state_manager: ConversationStateManager 实例
        request_messages: 本次请求的消息列表
    """
    # 检查是否成功响应
    if not isinstance(result, dict):
        return

    # 检查是否有错误
    if "error" in result:
        log.debug("[SCID] Skipping writeback due to error response", tag="GATEWAY")
        return

    # 提取 assistant 消息和签名
    assistant_message, signature = extract_signature_from_response(result)

    if not assistant_message:
        log.debug("[SCID] No assistant message found in response, skipping writeback", tag="GATEWAY")
        return

    # 提取本轮新增的用户消息（最后一条 user 消息）
    new_user_messages = []
    for msg in reversed(request_messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            new_user_messages.insert(0, msg)
            break  # 只取最后一条用户消息

    # 更新权威历史
    state_manager.update_authoritative_history(
        scid=scid,
        new_messages=new_user_messages,
        response_message=assistant_message,
        signature=signature
    )

    # 同时缓存签名到 signature_cache（双写）
    if signature:
        try:
            from akarins_gateway.signature_cache import cache_session_signature
            cache_session_signature(scid, signature, "")
        except Exception as cache_err:
            log.debug(f"[SCID] Failed to cache signature: {cache_err}", tag="GATEWAY")

    log.info(
        f"[SCID] Non-streaming writeback complete: scid={scid[:20]}..., "
        f"has_signature={signature is not None}",
        tag="GATEWAY"
    )


async def wrap_stream_with_writeback(
    stream: AsyncGenerator,
    scid: str,
    state_manager,
    request_messages: list,
    *,
    conversation_id: Optional[str] = None,
    is_augment: bool = False,
) -> AsyncGenerator:
    """
    包装流式响应，在流完成时执行回写

    收集流中的 assistant 消息内容，在流结束时一次性写回

    Args:
        stream: 原始流式响应
        scid: 会话 ID
        state_manager: ConversationStateManager 实例
        request_messages: 本次请求的消息列表
        conversation_id: [Augment] 会话 ID，用于 Bugment State 回写
        is_augment: [Augment] 是否为 Augment 请求，启用 Bugment chat_history 回写

    Yields:
        原始流数据
    """
    collected_content = []  # 改为收集content blocks（保留结构）
    collected_tool_calls = {}  # [FIX 2026-02-06] 改为 dict，按 index 合并流式 tool_call delta
    last_signature = None
    last_thinking_block = None  # 保存最后一个thinking块
    stream_completed = False
    has_error = False
    has_text_content = False  # 标记是否有文本内容

    # [FIX 2026-02-04] 定期保存中间状态的变量
    # 使用动态检查点间隔（由 get_checkpoint_interval 函数计算）
    chunk_count = 0
    collected_thinking = []  # 收集 thinking 内容
    collected_text = []  # 收集 text 内容

    def _ordered_tool_calls() -> List[Dict[str, Any]]:
        """
        将按 index 聚合的 tool_calls(dict) 转换为稳定有序的 list。
        """
        if not collected_tool_calls:
            return []
        return [collected_tool_calls[idx] for idx in sorted(collected_tool_calls.keys())]

    try:
        async for chunk in stream:
            yield chunk

            # [NEW 2026-02-02] 增加 chunk 计数
            chunk_count += 1

            # 尝试解析 chunk 提取内容
            try:
                if isinstance(chunk, (str, bytes)):
                    chunk_str = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk

                    # 解析 SSE 格式
                    for line in chunk_str.split("\n"):
                        line = line.strip()
                        if line.startswith("data: ") and line != "data: [DONE]":
                            json_str = line[6:]
                            try:
                                data = json.loads(json_str)

                                # 检查错误
                                if "error" in data:
                                    has_error = True
                                    continue

                                choices = data.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})

                                    # 收集内容（保留block结构）
                                    if "content" in delta:
                                        content = delta["content"]
                                        if isinstance(content, str):
                                            # 字符串内容：创建text block
                                            if content:  # 只收集非空内容
                                                collected_content.append({
                                                    "type": "text",
                                                    "text": content
                                                })
                                                has_text_content = True
                                                # [FIX 2026-02-02 B2] 填充 collected_text 列表
                                                collected_text.append(content)
                                        elif isinstance(content, list):
                                            # 列表内容：直接收集blocks
                                            for block in content:
                                                if isinstance(block, dict):
                                                    # 收集block
                                                    collected_content.append(block)

                                                    # [FIX 2026-02-02 B1] 将 thinking block 检查移到 for 循环内部
                                                    # 兼容两种thinking块格式
                                                    # - Anthropic格式: {"type": "thinking", "thoughtSignature": "..."}
                                                    # - Gemini格式: {"thought": true, "thoughtSignature": "..."}
                                                    is_thinking_block = (
                                                        block.get("type") in ("thinking", "redacted_thinking") or
                                                        block.get("thought") is True
                                                    )

                                                    if is_thinking_block:
                                                        # [FIX 2026-02-02 B2] 填充 collected_thinking 列表
                                                        thinking_text = block.get("thinking", "")
                                                        if thinking_text:
                                                            collected_thinking.append(thinking_text)

                                                        sig = block.get("thoughtSignature") or block.get("signature")
                                                        if sig and len(sig) > 50 and sig != "skip_thought_signature_validator":
                                                            last_signature = sig
                                                            # 保存完整的thinking块
                                                            last_thinking_block = block.copy()
                                                            # 归一化签名字段
                                                            if "signature" in last_thinking_block and "thoughtSignature" not in last_thinking_block:
                                                                last_thinking_block["thoughtSignature"] = sig

                                                            # [FIX 2026-02-02 M2] 使用去重函数缓存签名
                                                            # 避免重复写入相同签名
                                                            if scid and sig:
                                                                cache_signature_if_new(scid, sig)
                                                                # [FIX 2026-02-04] 立即更新检查点中的签名
                                                                # 解决签名与检查点时序不一致问题
                                                                update_checkpoint_signature(scid, sig, state_manager)

                                                    # [FIX 2026-02-02 B2] 收集 text block 内容
                                                    elif block.get("type") == "text":
                                                        text_content = block.get("text", "")
                                                        if text_content:
                                                            collected_text.append(text_content)

                                    # [FIX 2026-02-06] 按 index 合并流式 tool_call delta
                                    # OpenAI 流式响应中 tool_call 分多个 delta 发送：
                                    # - 第一个 delta 包含 id, type, function.name
                                    # - 后续 delta 只包含 function.arguments 片段
                                    if "tool_calls" in delta:
                                        for tc in delta["tool_calls"]:
                                            idx = tc.get("index", 0)
                                            if idx not in collected_tool_calls:
                                                # 初始化新的 tool_call 结构
                                                collected_tool_calls[idx] = {
                                                    "index": idx,
                                                    "id": "",
                                                    "type": "function",
                                                    "function": {"name": "", "arguments": ""}
                                                }
                                            # 合并各字段
                                            if tc.get("id"):
                                                collected_tool_calls[idx]["id"] = tc["id"]
                                            if tc.get("type"):
                                                collected_tool_calls[idx]["type"] = tc["type"]
                                            if tc.get("function"):
                                                func = tc["function"]
                                                if func.get("name"):
                                                    collected_tool_calls[idx]["function"]["name"] = func["name"]
                                                if func.get("arguments"):
                                                    # arguments 是增量追加的
                                                    collected_tool_calls[idx]["function"]["arguments"] += func["arguments"]
                                        # [FIX 2026-02-04] 工具调用发起时立即保存检查点（不等待间隔）
                                        # 解决工具调用中断时状态丢失的问题
                                        if scid:
                                            try:
                                                import time as time_module
                                                save_intermediate_state(
                                                    scid=scid,
                                                    thinking_content="".join(collected_thinking) if collected_thinking else None,
                                                    partial_response="".join(collected_text) if collected_text else None,
                                                    signature=last_signature,
                                                    timestamp=time_module.time(),
                                                    tool_calls=_ordered_tool_calls() if collected_tool_calls else None
                                                )
                                                log.debug(
                                                    f"[SCID] Immediate checkpoint on tool_call: "
                                                    f"tool_calls={len(collected_tool_calls)}",
                                                    tag="GATEWAY"
                                                )
                                            except Exception as tc_checkpoint_err:
                                                log.debug(f"[SCID] Tool call checkpoint failed (non-fatal): {tc_checkpoint_err}", tag="GATEWAY")

                                    # 检查是否完成
                                    if choices[0].get("finish_reason"):
                                        stream_completed = True

                            except json.JSONDecodeError as json_err:
                                # [FIX 2026-02-03] 记录 JSON 解析失败，便于调试
                                log.debug(f"[SCID] JSON decode failed in stream chunk: {json_err}", tag="GATEWAY")
            except Exception as parse_err:
                # [FIX 2026-02-03] 记录解析异常，但不影响流传输
                log.debug(f"[SCID] Stream chunk parse failed (non-fatal): {parse_err}", tag="GATEWAY")

            # [FIX 2026-02-04] 定期保存中间状态检查点（动态间隔）
            # 使用动态间隔策略，前期更频繁保存以捕获签名
            if scid and _should_save_checkpoint(chunk_count):
                try:
                    import time as time_module
                    save_intermediate_state(
                        scid=scid,
                        thinking_content="".join(collected_thinking) if collected_thinking else None,
                        partial_response="".join(collected_text) if collected_text else None,
                        signature=last_signature,
                        timestamp=time_module.time(),
                        tool_calls=_ordered_tool_calls() if collected_tool_calls else None
                    )
                    log.debug(
                        f"[SCID] Checkpoint saved: chunk={chunk_count}, interval={get_checkpoint_interval(chunk_count)}, "
                        f"tool_calls={len(collected_tool_calls) if collected_tool_calls else 0}",
                        tag="GATEWAY"
                    )
                except Exception as checkpoint_err:
                    log.debug(f"[SCID] Checkpoint save failed (non-fatal): {checkpoint_err}", tag="GATEWAY")

    finally:
        # 流结束后执行回写（只在成功完成时）
        # [FIX 2026-01-30] 当 collected_content 和 collected_tool_calls 均为空时，跳过 state_manager 更新
        # 避免外层 wrap（如 Augment NDJSON 流）用空内容覆盖已正确写入的 authoritative_history
        has_collected_content = bool(collected_content or collected_tool_calls)
        if stream_completed and not has_error and scid and state_manager and has_collected_content:
            try:
                # 构建 assistant 消息（保留block结构）
                assistant_message = {
                    "role": "assistant"
                }

                # 设置content（优先使用block列表，兼容旧格式）
                if collected_content:
                    # 合并相邻的text blocks（优化）
                    merged_content = []
                    pending_text = []

                    for block in collected_content:
                        if block.get("type") == "text":
                            pending_text.append(block.get("text", ""))
                        else:
                            # 非text block：先flush pending text
                            if pending_text:
                                merged_content.append({
                                    "type": "text",
                                    "text": "".join(pending_text)
                                })
                                pending_text = []
                            # 添加非text block
                            merged_content.append(block)

                    # flush剩余的text
                    if pending_text:
                        merged_content.append({
                            "type": "text",
                            "text": "".join(pending_text)
                        })

                    # 设置content为block列表
                    assistant_message["content"] = merged_content
                else:
                    # 空内容
                    assistant_message["content"] = ""

                ordered_tool_calls = _ordered_tool_calls()
                if ordered_tool_calls:
                    assistant_message["tool_calls"] = ordered_tool_calls

                # 提取本轮新增的用户消息
                new_user_messages = []
                for msg in reversed(request_messages):
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        new_user_messages.insert(0, msg)
                        break

                # 更新权威历史
                state_manager.update_authoritative_history(
                    scid=scid,
                    new_messages=new_user_messages,
                    response_message=assistant_message,
                    signature=last_signature
                )

                # 缓存签名
                if last_signature:
                    try:
                        from akarins_gateway.signature_cache import cache_session_signature
                        cache_session_signature(scid, last_signature, "")
                    except Exception as sig_cache_err:
                        # [FIX 2026-02-03] 记录签名缓存失败，便于调试
                        log.debug(f"[SCID] Signature cache failed in stream writeback: {sig_cache_err}", tag="GATEWAY")

                # [NEW 2026-02-02] 流正常结束，清除检查点
                # 标记会话已完成，防止下次请求误判为中断
                if state_manager and hasattr(state_manager, 'clear_checkpoint'):
                    try:
                        state_manager.clear_checkpoint(scid)
                        log.debug(f"[SCID] Checkpoint cleared on stream completion: scid={scid[:16]}...", tag="GATEWAY")
                    except Exception as clear_err:
                        # [FIX 2026-02-03] 记录检查点清除失败，便于调试
                        log.debug(f"[SCID] Checkpoint clear failed (non-fatal): {clear_err}", tag="GATEWAY")

                # 计算内容长度（用于日志）
                content_len = 0
                merged_content = assistant_message.get("content", [])
                if isinstance(merged_content, list):
                    for block in merged_content:
                        if block.get("type") == "text":
                            content_len += len(block.get("text", ""))
                        elif block.get("type") in ("thinking", "redacted_thinking"):
                            content_len += len(block.get("thinking", ""))
                elif isinstance(merged_content, str):
                    content_len = len(merged_content)

                log.info(
                    f"[SCID] Streaming writeback complete: scid={scid[:20]}..., "
                    f"content_len={content_len}, "
                    f"content_blocks={len(merged_content) if collected_content else 0}, "
                    f"tool_calls={len(ordered_tool_calls) if ordered_tool_calls else 0}, "
                    f"has_thinking_block={last_thinking_block is not None}, "
                    f"has_signature={last_signature is not None}",
                    tag="GATEWAY"
                )

                # [FIX 2026-01-30] Augment Bugment State 响应回写：将 assistant 回复追加到 chat_history
                # 解决 Augment 每轮重复介绍模型编号的问题（上下文历史未维护）
                if is_augment and conversation_id and isinstance(conversation_id, str) and conversation_id.strip():
                    try:
                        from akarins_gateway.gateway.augment.state import bugment_conversation_state_get, bugment_conversation_state_put

                        # 提取 assistant 文本内容（排除 thinking 块，供 Bugment chat_history 使用）
                        response_text_parts = []
                        if isinstance(assistant_message.get("content"), list):
                            for blk in assistant_message["content"]:
                                if isinstance(blk, dict) and blk.get("type") == "text":
                                    response_text_parts.append(blk.get("text", "") or "")
                        elif isinstance(assistant_message.get("content"), str):
                            response_text_parts.append(assistant_message["content"])
                        response_text = "".join(response_text_parts).strip()

                        # 提取最后一条用户消息
                        last_user_content = ""
                        for msg in reversed(request_messages):
                            if isinstance(msg, dict) and msg.get("role") == "user":
                                content = msg.get("content", "")
                                if isinstance(content, str):
                                    last_user_content = content.strip()
                                elif isinstance(content, list):
                                    for blk in content:
                                        if isinstance(blk, dict) and blk.get("type") == "text":
                                            last_user_content = (blk.get("text", "") or "").strip()
                                            break
                                break

                        # 追加到 chat_history（Bugment 格式：{ request_message, response_text }）
                        cur_state = bugment_conversation_state_get(conversation_id.strip())
                        cur_history = cur_state.get("chat_history")
                        if not isinstance(cur_history, list):
                            cur_history = []
                        new_entry = {"request_message": last_user_content, "response_text": response_text}
                        cur_history.append(new_entry)
                        bugment_conversation_state_put(conversation_id.strip(), chat_history=cur_history)

                        log.info(
                            f"[SCID] Bugment chat_history writeback: conversation_id={conversation_id[:16]}..., "
                            f"history_len={len(cur_history)}, "
                            f"response_text_len={len(response_text)}",
                            tag="GATEWAY"
                        )
                    except Exception as bugment_err:
                        log.warning(f"[SCID] Bugment chat_history writeback failed: {bugment_err}", tag="GATEWAY")

            except Exception as wb_err:
                log.warning(f"[SCID] Streaming writeback failed: {wb_err}", tag="GATEWAY")

        # ==================== [NEW 2026-02-04] Claude Code 签名缓存 ====================
        # 即使 scid 为 None（Claude Code），也缓存签名到全局缓存
        # 这样下次请求可以通过 signature_cache 恢复签名
        # =============================================================================
        if last_signature and not scid and stream_completed and not has_error:
            try:
                from akarins_gateway.signature_cache import cache_signature
                # 使用 thinking 内容作为 key 缓存签名
                if collected_thinking:
                    thinking_text = "".join(collected_thinking)
                    if thinking_text.strip():
                        cache_signature(thinking_text, last_signature)
                        log.debug(
                            f"[SCID] Claude Code: cached signature from stream "
                            f"(sig_len={len(last_signature)}, thinking_len={len(thinking_text)})",
                            tag="GATEWAY"
                        )
            except Exception as sig_cache_err:
                log.debug(f"[SCID] Claude Code: failed to cache signature: {sig_cache_err}", tag="GATEWAY")
        # ==================== End of Claude Code 签名缓存 ====================


# ==================== [NEW 2026-02-04] 孤儿 Thinking Blocks 清理 ====================
def _strip_orphan_thinking_blocks(messages: List[Dict]) -> List[Dict]:
    """
    清理没有签名的孤儿 thinking blocks，同时保留 text 上下文

    [FIX 2026-02-04] 解决首次 thinking 中断时无法继续 thinking 的问题

    问题背景：
    - thoughtSignature 在 thinking block 末尾才发送
    - 如果在 thinking 过程中中断，签名还没收到
    - 此时没有任何签名可以恢复，但消息中可能有不完整的 thinking blocks
    - 将这些孤儿 thinking blocks 发送给 API 会导致 400 错误

    策略：
    - Thinking 层面：删除孤儿 thinking blocks，当作第一次思考
    - Text 层面：保留所有 text 内容，保持上下文连续性
    - 这两个策略不冲突，因为 thoughtSignature 只验证 thinking block 连续性

    支持的格式：
    1. 数组格式 content（Anthropic/OpenAI）: type="thinking" / type="redacted_thinking"
    2. 字符串格式 content: <think>...</think> 标签
    3. Gemini 格式: thought=True

    Args:
        messages: 消息列表

    Returns:
        清理后的消息列表（深拷贝）
    """
    import copy
    import re

    if not messages:
        return messages

    cleaned_messages = []
    stripped_count = 0

    for msg in messages:
        if not isinstance(msg, dict):
            cleaned_messages.append(msg)
            continue

        role = msg.get("role")

        # 只处理 assistant/model 消息（thinking blocks 只出现在这些消息中）
        if role not in ("assistant", "model"):
            cleaned_messages.append(msg)
            continue

        content = msg.get("content")

        # Case 1: 数组格式 content
        if isinstance(content, list):
            new_content = []
            for block in content:
                if not isinstance(block, dict):
                    new_content.append(block)
                    continue

                block_type = block.get("type")
                is_thinking = block_type in ("thinking", "redacted_thinking")
                is_gemini_thinking = block.get("thought") is True

                if is_thinking or is_gemini_thinking:
                    # 检查是否有有效签名
                    sig = block.get("thoughtSignature") or block.get("signature")
                    if sig and len(sig) > 50 and sig != "skip_thought_signature_validator":
                        # 有有效签名，保留
                        new_content.append(block)
                    else:
                        # 没有签名，是孤儿 thinking block，删除
                        stripped_count += 1
                        thinking_preview = ""
                        if block_type == "thinking":
                            thinking_preview = (block.get("thinking", ""))[:50]
                        elif is_gemini_thinking:
                            thinking_preview = (block.get("text", ""))[:50]
                        log.info(
                            f"[SCID] Stripping orphan thinking block (no signature): "
                            f"type={block_type or 'gemini_thought'}, preview='{thinking_preview}...'",
                            tag="GATEWAY"
                        )
                else:
                    # 非 thinking block，保留（包括 text、tool_use 等）
                    new_content.append(block)

            # 如果所有内容都被删除，添加占位符
            if not new_content and content:
                new_content = [{"type": "text", "text": "..."}]

            new_msg = copy.deepcopy(msg)
            new_msg["content"] = new_content
            cleaned_messages.append(new_msg)

        # Case 2: 字符串格式 content（包含 <think> 标签）
        elif isinstance(content, str):
            # 检测 <think>...</think> 标签
            think_pattern = re.compile(r'<think>(.*?)</think>', re.DOTALL | re.IGNORECASE)
            has_think_tags = bool(think_pattern.search(content))

            if has_think_tags:
                # 移除 <think>...</think> 标签，保留其他内容
                new_content = think_pattern.sub('', content)
                # 清理多余空白行
                new_content = re.sub(r'\n\s*\n\s*\n', '\n\n', new_content).strip()

                if not new_content:
                    new_content = "..."

                stripped_count += 1
                log.info(
                    f"[SCID] Stripping orphan thinking from string content: "
                    f"original_len={len(content)}, cleaned_len={len(new_content)}",
                    tag="GATEWAY"
                )

                new_msg = copy.deepcopy(msg)
                new_msg["content"] = new_content
                cleaned_messages.append(new_msg)
            else:
                # 没有 thinking 标签，直接保留
                cleaned_messages.append(msg)

        # Case 3: Gemini parts 格式
        elif "parts" in msg:
            parts = msg.get("parts", [])
            new_parts = []

            for part in parts:
                if not isinstance(part, dict):
                    new_parts.append(part)
                    continue

                is_gemini_thinking = part.get("thought") is True

                if is_gemini_thinking:
                    # 检查签名
                    sig = part.get("thoughtSignature")
                    if sig and len(sig) > 50 and sig != "skip_thought_signature_validator":
                        new_parts.append(part)
                    else:
                        stripped_count += 1
                        thinking_preview = (part.get("text", ""))[:50]
                        log.info(
                            f"[SCID] Stripping orphan Gemini thinking part: preview='{thinking_preview}...'",
                            tag="GATEWAY"
                        )
                else:
                    new_parts.append(part)

            if not new_parts and parts:
                new_parts = [{"text": "..."}]

            new_msg = copy.deepcopy(msg)
            new_msg["parts"] = new_parts
            cleaned_messages.append(new_msg)

        else:
            # 其他情况，直接保留
            cleaned_messages.append(msg)

    if stripped_count > 0:
        log.warning(
            f"[SCID] Stripped {stripped_count} orphan thinking blocks from messages. "
            f"Text context preserved. Thinking will restart fresh.",
            tag="GATEWAY"
        )

    return cleaned_messages
# ==================== End of 孤儿 Thinking Blocks 清理 ====================


# ==================== [NEW 2026-02-04] Claude Code 轻量级签名恢复 ====================
def _recover_signatures_for_claude_code(
    messages: List[Dict],
    session_id: Optional[str],
    body: Dict[str, Any]
) -> Tuple[List[Dict], Optional[str]]:
    """
    为 Claude Code 客户端执行轻量级签名恢复

    只扫描和恢复签名，不进行完整的 SCID 状态管理。

    问题背景（2026-02-04 诊断）：
    - Claude Code 被完全绕过 SCID 导致签名恢复失效
    - 产生 400 Corrupted thought signature 错误
    - 根因：toolu_vrtx_ 等跨后端 tool_id 无法恢复签名

    Args:
        messages: 消息列表
        session_id: 会话ID（Claude Code 通常为 None）
        body: 请求体（用于检测 thinking 配置）

    Returns:
        (processed_messages, last_signature)
    """
    from akarins_gateway.ide_compat.sanitizer import AnthropicSanitizer

    # 检测是否启用 thinking
    thinking_enabled = body.get("thinking") is not None

    # 扫描消息检测 thinking blocks
    has_thinking_blocks = False
    last_signature = None

    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in ("thinking", "redacted_thinking"):
                    has_thinking_blocks = True
                    sig = block.get("thoughtSignature") or block.get("signature")
                    if sig and len(sig) > 50 and sig != "skip_thought_signature_validator":
                        last_signature = sig

    # 如果有 thinking blocks 或启用了 thinking，执行签名恢复
    if has_thinking_blocks or thinking_enabled:
        try:
            sanitizer = AnthropicSanitizer()
            processed_messages, final_thinking_enabled = sanitizer.sanitize_messages(
                messages=messages,
                thinking_enabled=thinking_enabled or has_thinking_blocks,
                session_id=session_id,
                last_thought_signature=last_signature,
            )

            # 同步 thinking 配置
            if not final_thinking_enabled and "thinking" in body:
                body.pop("thinking", None)
                log.info("[SCID] Claude Code: thinking disabled due to signature validation failure", tag="GATEWAY")

            return processed_messages, last_signature
        except Exception as e:
            log.warning(f"[SCID] Claude Code: signature recovery failed: {e}", tag="GATEWAY")
            # 失败时返回原始消息
            return messages, last_signature

    # 无 thinking blocks，直接返回
    return messages, None
# ==================== End of Claude Code 轻量级签名恢复 ====================


def apply_scid_and_sanitization(
    *,
    headers: Dict[str, str],
    raw_body: Dict[str, Any],
    body: Dict[str, Any],
    messages: Optional[List[Dict[str, Any]]] = None,
):
    """
    应用 SCID 架构和消息净化

    功能：
    1. 提取或生成 SCID（会话ID）
    2. 使用 AnthropicSanitizer 净化消息
    3. 使用 ConversationStateManager 管理状态
    4. 支持 thinking blocks 检测和签名提取
    5. 支持 thinking 配置同步

    Args:
        headers: 请求头
        raw_body: 原始请求体
        body: 规范化后的请求体
        messages: 可选的消息列表（如果提供则使用，否则从 body 中提取）

    Returns:
        Tuple[scid, client_info, state_manager, messages_to_sanitize]
    """
    scid = None
    client_info = None
    state_manager = None
    last_signature = None

    try:
        from akarins_gateway.ide_compat import ClientTypeDetector

        client_info = ClientTypeDetector.detect(dict(headers))
    except Exception as e:
        log.warning(f"Failed to detect client type or extract SCID: {e}", tag="GATEWAY")
        client_info = None

    # ==================== [FIX 2026-02-04] 轻量级签名恢复模式 ====================
    # 某些客户端（如 Claude Code）需要签名恢复功能，但不需要完整的 SCID 状态管理
    #
    # 问题根因（2026-02-04 诊断）：
    # - 之前完全绕过 SCID 导致 Extended Thinking 签名无法恢复
    # - 产生 400 Corrupted thought signature 错误
    #
    # 启用的功能：
    # 1. ✅ 签名扫描和恢复（6层策略）
    # 2. ✅ 签名缓存写入
    # 3. ✅ 响应签名提取和缓存
    #
    # 跳过的功能：
    # 1. ✗ SCID 生成
    # 2. ✗ 权威历史管理
    # 3. ✗ 消息合并
    # 4. ✗ 检查点保存
    #
    # [FIX 2026-02-04] 使用 needs_signature_recovery_only 标志而非硬编码客户端类型
    # ===================================================================================
    if client_info and getattr(client_info, 'needs_signature_recovery_only', False):
        messages_to_process = messages if isinstance(messages, list) else body.get("messages", [])
        if not isinstance(messages_to_process, list):
            messages_to_process = []

        # [NEW] 执行轻量级签名恢复
        processed_messages, last_signature = _recover_signatures_for_claude_code(
            messages_to_process, session_id=None, body=body
        )

        body["messages"] = processed_messages

        log.info(
            f"[SCID] Lightweight signature recovery completed "
            f"(client={client_info.display_name}, version={client_info.version or 'unknown'}, "
            f"messages={len(processed_messages)}, has_signature={last_signature is not None})",
            tag="GATEWAY"
        )

        return None, client_info, None, processed_messages
    # ==================== End of 轻量级签名恢复模式 ====================

    # ================================================================
    # [FIX 2026-01-24] 使用新的 SCID 生成策略（对 checkpoint 友好）
    # 
    # 核心问题：
    # - 旧方案使用"前 3 条消息"生成 fingerprint
    # - checkpoint 回退时消息变化 → fingerprint 变化 → SCID 变化
    # - 导致找不到缓存
    # 
    # 新方案：
    # - 使用"第一条用户消息"生成 fingerprint
    # - checkpoint 回退时第一条消息不变 → SCID 不变
    # - ✅ 成功恢复缓存
    # ================================================================
    from akarins_gateway.gateway.scid_generator import extract_or_generate_scid, extract_client_ip
    
    # 提取客户端 IP（用于降低误匹配风险）
    client_ip = extract_client_ip(dict(headers))
    
    # 准备消息列表
    messages_for_scid = messages if isinstance(messages, list) else body.get("messages", [])
    if not isinstance(messages_for_scid, list):
        messages_for_scid = []
    
    # 使用新的混合策略生成 SCID
    scid = extract_or_generate_scid(
        headers=dict(headers),
        body=raw_body if isinstance(raw_body, dict) else (body if isinstance(body, dict) else {}),
        messages=messages_for_scid,
        client_ip=client_ip
    )
    
    # 记录客户端信息
    if client_info:
        log.info(
            f"[SCID] SCID generated for {client_info.display_name}: {scid[:30]}..., "
            f"client_ip={client_ip}",
            tag="GATEWAY"
        )

    # ================================================================
    # [SCID] Step 2: 消息净化（使用 AnthropicSanitizer）
    # ================================================================
    messages_to_sanitize = messages if isinstance(messages, list) else body.get("messages", [])
    if not isinstance(messages_to_sanitize, list):
        messages_to_sanitize = []

    if client_info and client_info.needs_sanitization:
        try:
            from akarins_gateway.ide_compat import AnthropicSanitizer, ConversationStateManager
            from akarins_gateway.cache.signature_database import SignatureDatabase

            # 获取状态管理器
            try:
                db = SignatureDatabase()
                state_manager = ConversationStateManager(db)
            except Exception as db_err:
                log.warning(f"[SCID] Failed to initialize SignatureDatabase: {db_err}, using memory-only state manager", tag="GATEWAY")
                state_manager = ConversationStateManager(None)

            # 如果有 SCID，尝试获取权威历史和最后签名
            if scid and state_manager:
                state = state_manager.get_or_create_state(scid, client_info.client_type.value)
                last_signature = state.last_signature

                # [FIX 2026-02-08] 新会话污染隔离：
                # 当短消息新会话误命中旧 SCID（重工具链历史）时，重置该 SCID 状态，避免合并旧缓存。
                should_reset, reset_reason = _should_reset_stale_scid_state(state, messages_to_sanitize)
                if should_reset:
                    reset_ok = state_manager.reset_state_for_new_chat(
                        scid,
                        client_type=client_info.client_type.value if client_info else None,
                        reason=reset_reason,
                    )
                    if reset_ok:
                        state = state_manager.get_or_create_state(scid, client_info.client_type.value)
                        last_signature = None
                        log.warning(
                            f"[SCID] Detected stale-state collision and reset session state: "
                            f"scid={scid[:20]}..., reason={reset_reason[:200]}",
                            tag="GATEWAY",
                        )
                    else:
                        log.warning(
                            f"[SCID] Stale-state collision detected but reset failed: "
                            f"scid={scid[:20]}..., reason={reset_reason[:200]}",
                            tag="GATEWAY",
                        )

                # [NEW 2026-02-02] 检查是否有未完成的会话，尝试恢复中断状态
                # 解决流中断时签名丢失导致 thinking 功能失效的问题
                if hasattr(state_manager, 'has_incomplete_session') and state_manager.has_incomplete_session(scid):
                    checkpoint = state_manager.get_checkpoint(scid)
                    if checkpoint:
                        log.info(
                            f"[SCID] Detected incomplete session, attempting recovery: scid={scid[:16]}..., "
                            f"checkpoint_time={checkpoint.get('timestamp')}",
                            tag="GATEWAY"
                        )

                        # 尝试从签名缓存恢复
                        cached_signature = checkpoint.get('signature')
                        if cached_signature and not last_signature:
                            last_signature = cached_signature
                            log.info(
                                f"[SCID] Recovered signature from checkpoint: scid={scid[:16]}..., "
                                f"sig_len={len(cached_signature)}",
                                tag="GATEWAY"
                            )

                        # 如果检查点中没有签名，尝试从 signature_cache 获取
                        if not last_signature:
                            try:
                                from akarins_gateway.signature_cache import get_session_signature
                                cached_sig = get_session_signature(scid)
                                if cached_sig:
                                    last_signature = cached_sig
                                    log.info(
                                        f"[SCID] Recovered signature from session cache: scid={scid[:16]}..., "
                                        f"sig_len={len(cached_sig)}",
                                        tag="GATEWAY"
                                    )
                            except Exception as cache_err:
                                log.warning(f"[SCID] Failed to recover signature from cache: {cache_err}", tag="GATEWAY")

                        # [FIX 2026-02-04] 如果仍然没有签名，尝试从 SQLite 获取最近的会话签名
                        # 解决 Cursor 中途中断后无法恢复 thinking 的问题
                        if not last_signature:
                            try:
                                from akarins_gateway.signature_cache import get_last_signature
                                last_sig = get_last_signature()
                                if last_sig:
                                    last_signature = last_sig
                                    log.info(
                                        f"[SCID] Recovered signature from last signature cache: scid={scid[:16]}..., "
                                        f"sig_len={len(last_sig)}",
                                        tag="GATEWAY"
                                    )
                            except Exception as last_sig_err:
                                log.warning(f"[SCID] Failed to recover last signature: {last_sig_err}", tag="GATEWAY")

                        # [FIX 2026-02-04] 如果还是没有签名，尝试从 SQLite 数据库直接获取
                        if not last_signature:
                            try:
                                from akarins_gateway.cache.signature_database import SignatureDatabase
                                db = SignatureDatabase()
                                if hasattr(db, 'get_last_session_signature'):
                                    db_result = db.get_last_session_signature()
                                    if db_result:
                                        last_signature = db_result[0]  # (signature, thinking_text)
                                        log.info(
                                            f"[SCID] Recovered signature from SQLite last session: scid={scid[:16]}..., "
                                            f"sig_len={len(last_signature)}",
                                            tag="GATEWAY"
                                        )
                            except Exception as db_err:
                                log.warning(f"[SCID] Failed to recover from SQLite: {db_err}", tag="GATEWAY")

                        # [FIX 2026-02-04] 最后的降级策略：如果无法恢复签名，清理孤儿 thinking blocks
                        # 策略：thinking 层面当成第一次思考，text 层面保留上下文
                        if not last_signature:
                            log.warning(
                                f"[SCID] Unable to recover signature for incomplete session: scid={scid[:16]}... "
                                f"Will strip orphan thinking blocks and start fresh thinking.",
                                tag="GATEWAY"
                            )

                            # [FIX 2026-02-04] 调用孤儿 thinking blocks 清理函数
                            # 这会删除没有签名的 thinking blocks，但保留所有 text 内容
                            original_msg_count = len(messages_to_sanitize)
                            messages_to_sanitize = _strip_orphan_thinking_blocks(messages_to_sanitize)
                            body["messages"] = messages_to_sanitize

                            # 统计清理效果
                            cleaned_count = sum(
                                1 for msg in messages_to_sanitize
                                if isinstance(msg, dict) and msg.get("role") == "assistant"
                                and isinstance(msg.get("content"), list)
                                and any(
                                    isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
                                    for b in msg.get("content", [])
                                )
                            )
                            log.info(
                                f"[SCID] Orphan thinking blocks stripped: "
                                f"messages={original_msg_count}, remaining_thinking_msgs={cleaned_count}",
                                tag="GATEWAY"
                            )

                # 使用权威历史合并客户端消息（Phase 5: Bridge 输出为 OpenAI 格式，merge 通用化无 Augment 分支）
                client_messages = messages_to_sanitize
                merged_messages = state_manager.merge_with_client_history(
                    scid,
                    client_messages,
                    client_info.client_type.value if client_info else None
                )

                if merged_messages != client_messages:
                    log.info(
                        f"[SCID] Merged messages with authoritative history: {len(client_messages)} -> {len(merged_messages)}",
                        tag="GATEWAY",
                    )
                    messages_to_sanitize = merged_messages
                    body["messages"] = merged_messages

            # 使用 AnthropicSanitizer 净化消息
            sanitizer = AnthropicSanitizer()
            messages_for_scan = messages_to_sanitize

            # 检测是否启用 thinking（OpenAI 格式可能没有 thinking 字段，但消息中可能有 thinking blocks）
            thinking_enabled = body.get("thinking") is not None

            # ================================================================
            # [FIX 2026-01-20] 检测消息中是否有 thinking blocks（用于判断 thinking_enabled）
            #
            # 注意：不再提取历史签名灌入缓存，因为：
            # 1. Thinking signature 是会话绑定的，历史签名在新请求中已失效
            # 2. sanitizer.py 已实现"直接删除历史 thinking blocks"的策略
            # 3. 只保留最新消息的 thinking blocks（由 sanitizer 处理签名恢复）
            # ================================================================
            thinking_blocks_found = 0
            last_extracted_signature = None

            # 用于从 <think> 标签提取内容的正则表达式
            think_tag_pattern = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)

            # 识别最后一条 assistant 消息的索引（只从最新消息提取签名）
            last_assistant_idx = None
            for i in range(len(messages_for_scan) - 1, -1, -1):
                if isinstance(messages_for_scan[i], dict) and messages_for_scan[i].get("role") == "assistant":
                    last_assistant_idx = i
                    break

            for msg_idx, msg in enumerate(messages_for_scan):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    content = msg.get("content")

                    # ================================================================
                    # 支持两种格式：
                    # 1. 数组格式: content: [{ type: "thinking", thinking: "...", signature: "..." }]
                    # 2. 字符串格式: content: "<think>...</think>正文"
                    # ================================================================

                    if isinstance(content, list):
                        # 数组格式
                        for block_idx, block in enumerate(content):
                            if isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking"):
                                thinking_blocks_found += 1

                                # 只从最新 assistant 消息提取签名（供 sanitizer 使用）
                                if msg_idx == last_assistant_idx:
                                    signature = block.get("signature") or block.get("thoughtSignature")
                                    if signature and isinstance(signature, str) and len(signature) > 50:
                                        if signature != "skip_thought_signature_validator":
                                            last_extracted_signature = signature
                                            log.debug(
                                                f"[SCID] Extracted signature from latest assistant message: "
                                                f"msg_idx={msg_idx}, sig_len={len(signature)}",
                                                tag="GATEWAY",
                                            )

                    elif isinstance(content, str) and "<think>" in content.lower():
                        # 字符串格式：包含 <think> 标签
                        think_matches = think_tag_pattern.findall(content)
                        for match_idx, thinking_text in enumerate(think_matches):
                            thinking_blocks_found += 1
                            thinking_text = thinking_text.strip()

                            # 只从最新 assistant 消息尝试从缓存获取签名
                            if msg_idx == last_assistant_idx:
                                from akarins_gateway.signature_cache import get_cached_signature

                                cached_sig = get_cached_signature(
                                    thinking_text,
                                    conversation_id=scid
                                )
                                if cached_sig:
                                    last_extracted_signature = cached_sig
                                    log.debug(
                                        f"[SCID] Found cached signature for latest string thinking: "
                                        f"msg_idx={msg_idx}, sig_len={len(cached_sig)}",
                                        tag="GATEWAY",
                                    )

            # [DEBUG] 记录扫描结果
            log.info(
                f"[SCID] Thinking blocks scan: found {thinking_blocks_found} thinking blocks in {len(messages_for_scan)} messages, "
                f"latest_signature={'extracted' if last_extracted_signature else 'none'}",
                tag="GATEWAY",
            )

            # 更新 last_signature 供后续 sanitizer 使用（仅最新消息的签名）
            if last_extracted_signature and not last_signature:
                last_signature = last_extracted_signature

            # 检查消息中是否有 thinking blocks（支持数组格式和字符串格式）
            has_thinking_blocks = thinking_blocks_found > 0
            if has_thinking_blocks:
                thinking_enabled = True

            if has_thinking_blocks or thinking_enabled:
                sanitized_messages, final_thinking_enabled = sanitizer.sanitize_messages(
                    messages=messages_for_scan,
                    thinking_enabled=thinking_enabled,
                    session_id=scid,
                    last_thought_signature=last_signature,
                )

                messages_to_sanitize = sanitized_messages
                body["messages"] = sanitized_messages

                # ================================================================
                # [FIX 2026-01-20] 增强 thinkingConfig 同步逻辑
                # 确保所有路径都正确同步 thinking 配置
                # ================================================================
                if not final_thinking_enabled:
                    # 1. 移除 body 中的 thinking 配置
                    if "thinking" in body:
                        log.info("[SCID] Removing thinking config due to sanitization", tag="GATEWAY")
                        body.pop("thinking", None)

                    # 2. 移除相关的 thinking 参数（如果有的话）
                    for thinking_key in ("thinking_budget", "thinking_level", "thinking_config"):
                        if thinking_key in body:
                            log.debug(f"[SCID] Removing {thinking_key} due to thinking disabled", tag="GATEWAY")
                            body.pop(thinking_key, None)
                else:
                    # [FIX 2026-01-23] 如果 thinking 已启用但 body 中没有配置，添加默认配置
                    # 这样 kiro-gateway 的转换函数就能正确识别并传递 thinking 模式
                    if "thinking" not in body:
                        from akarins_gateway.converters.anthropic_constants import DEFAULT_THINKING_BUDGET
                        body["thinking"] = {
                            "type": "enabled",
                            "budget_tokens": DEFAULT_THINKING_BUDGET
                        }
                        log.info(
                            f"[SCID] Added thinking config to body (budget={DEFAULT_THINKING_BUDGET}) "
                            f"for kiro-gateway compatibility",
                            tag="GATEWAY"
                        )

                log.info(
                    f"[SCID] Sanitized messages: "
                    f"client={client_info.display_name}, "
                    f"messages={len(messages_for_scan)}->{len(sanitized_messages)}, "
                    f"thinking={thinking_enabled}->{final_thinking_enabled}",
                    tag="GATEWAY",
                )

        except Exception as e:
            log.warning(f"[SCID] Message sanitization failed (non-fatal): {e}", tag="GATEWAY")
            # 净化失败不影响主流程
    else:
        body["messages"] = messages_to_sanitize

    return scid, client_info, state_manager, messages_to_sanitize
