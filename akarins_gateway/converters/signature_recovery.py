"""
Signature Recovery Module - 7层签名恢复策略

用于在各种场景下恢复 Claude Extended Thinking 模式的 signature。

恢复优先级（7层）：
1. Client (请求自带的 signature)
2. Context (上下文中的 last_thought_signature)
3. Thinking Hash Cache (基于 thinking 文本哈希的缓存) ← [FIX 2026-02-04]
4. Session Cache (会话级别缓存)
5. Tool Cache (工具ID级别缓存，仅用于 tool_use)
6. Last Signature (最近缓存的配对，fallback)
7. 使用占位符 skip_thought_signature_validator 或禁用 Thinking

注意：对于 tool_use 恢复，还有一个额外的 Encoded Tool ID 层（Layer 3）。

Author: Claude Opus 4.5 (浮浮酱)
Date: 2026-01-17
Updated: 2026-02-04 - 修正 source 标记问题
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple

log = logging.getLogger("gcli2api.signature_recovery")

# 最小有效签名长度
MIN_SIGNATURE_LENGTH = 10

# 占位符签名（用于绕过验证）
SKIP_SIGNATURE_VALIDATOR = "skip_thought_signature_validator"


class RecoverySource(Enum):
    """签名恢复来源枚举"""
    CLIENT = "client"           # 客户端提供
    CONTEXT = "context"         # 上下文中的签名
    ENCODED_TOOL_ID = "encoded_tool_id"  # 从编码的工具ID解码
    THINKING_HASH_CACHE = "thinking_hash_cache"  # [FIX 2026-02-04] 基于 thinking 文本哈希的缓存
    SESSION_CACHE = "session_cache"      # 会话级缓存
    TOOL_CACHE = "tool_cache"            # 工具ID缓存
    LAST_SIGNATURE = "last_signature"    # 最近缓存
    PLACEHOLDER = "placeholder"          # 占位符
    NONE = "none"                        # 未找到


@dataclass
class RecoveryResult:
    """签名恢复结果"""
    signature: Optional[str]
    source: RecoverySource
    thinking_text: Optional[str] = None  # 配对的 thinking 文本（如果有）

    @property
    def success(self) -> bool:
        """是否成功恢复"""
        return self.signature is not None and self.source != RecoverySource.NONE

    @property
    def is_placeholder(self) -> bool:
        """是否使用占位符"""
        return self.source == RecoverySource.PLACEHOLDER

    def __str__(self) -> str:
        sig_preview = self.signature[:20] + "..." if self.signature and len(self.signature) > 20 else self.signature
        return f"RecoveryResult(source={self.source.value}, signature={sig_preview})"


def is_valid_signature(signature: Optional[str]) -> bool:
    """
    验证签名是否有效

    Args:
        signature: 待验证的签名

    Returns:
        bool: 是否有效
    """
    if not signature or not isinstance(signature, str):
        return False

    if len(signature) < MIN_SIGNATURE_LENGTH:
        return False

    if signature == SKIP_SIGNATURE_VALIDATOR:
        return False

    return True


def recover_signature_for_thinking(
    thinking_text: str,
    client_signature: Optional[str] = None,
    context_signature: Optional[str] = None,
    session_id: Optional[str] = None,
    use_placeholder_fallback: bool = True,
    owner_id: Optional[str] = None,  # [FIX 2026-01-22] 新增 owner_id 参数，用于多客户端会话隔离（已逐步下线）
    conversation_id: Optional[str] = None
) -> RecoveryResult:
    """
    为 thinking 块恢复签名（6层策略）

    Args:
        thinking_text: thinking 块的文本内容
        client_signature: 客户端提供的签名
        context_signature: 上下文中的签名
        session_id: 会话ID（用于 Session Cache）
        use_placeholder_fallback: 是否在所有策略失败时使用占位符
        owner_id: 可选的所有者ID，用于多客户端会话隔离（已逐步下线）
        conversation_id: 可选的会话标识（优先使用）

    Returns:
        RecoveryResult: 恢复结果
    """
    # 延迟导入避免循环依赖
    from akarins_gateway.signature_cache import (
        get_cached_signature,
        get_session_signature_with_text,
        get_last_signature_with_text
    )
    effective_conversation_id = conversation_id or owner_id
    effective_owner_id = owner_id or conversation_id

    # 优先级 1: 客户端提供的签名
    if is_valid_signature(client_signature):
        log.info(f"[SIGNATURE_RECOVERY] Layer 1: Client signature found")
        return RecoveryResult(
            signature=client_signature,
            source=RecoverySource.CLIENT
        )

    # 优先级 2: 上下文中的签名
    if is_valid_signature(context_signature):
        log.info(f"[SIGNATURE_RECOVERY] Layer 2: Context signature found")
        return RecoveryResult(
            signature=context_signature,
            source=RecoverySource.CONTEXT
        )

    # 优先级 3: 从缓存恢复（基于 thinking 文本哈希）
    # [FIX 2026-01-22] 传递 owner_id 进行会话隔离
    # [FIX 2026-02-04] 修正 source 标记为 THINKING_HASH_CACHE
    if thinking_text:
        cached_sig = get_cached_signature(
            thinking_text,
            conversation_id=effective_conversation_id
        )
        if is_valid_signature(cached_sig):
            log.info(f"[SIGNATURE_RECOVERY] Layer 3: Cached signature found for thinking text")
            return RecoveryResult(
                signature=cached_sig,
                source=RecoverySource.THINKING_HASH_CACHE,
                thinking_text=thinking_text
            )

    # 优先级 4: Session Cache
    # [FIX 2026-01-22] 传递 owner_id 进行会话隔离
    if session_id:
        session_result = get_session_signature_with_text(session_id, effective_owner_id)
        if session_result:
            sig, text = session_result
            if is_valid_signature(sig):
                log.info(f"[SIGNATURE_RECOVERY] Layer 4: Session cache hit for session_id={session_id[:16]}...")
                return RecoveryResult(
                    signature=sig,
                    source=RecoverySource.SESSION_CACHE,
                    thinking_text=text
                )

    # 优先级 5: 最近缓存的签名（fallback）
    last_result = get_last_signature_with_text()
    if last_result:
        sig, text = last_result
        if is_valid_signature(sig):
            log.info(f"[SIGNATURE_RECOVERY] Layer 6: Using last cached signature (fallback)")
            return RecoveryResult(
                signature=sig,
                source=RecoverySource.LAST_SIGNATURE,
                thinking_text=text
            )

    # 所有策略都失败
    if use_placeholder_fallback:
        log.warning(f"[SIGNATURE_RECOVERY] All strategies failed, using placeholder")
        return RecoveryResult(
            signature=SKIP_SIGNATURE_VALIDATOR,
            source=RecoverySource.PLACEHOLDER
        )

    log.warning(f"[SIGNATURE_RECOVERY] All strategies failed, no signature available")
    return RecoveryResult(
        signature=None,
        source=RecoverySource.NONE
    )


def recover_signature_for_tool_use(
    tool_id: str,
    encoded_tool_id: str,
    client_signature: Optional[str] = None,
    context_signature: Optional[str] = None,
    session_id: Optional[str] = None,
    use_placeholder_fallback: bool = True,
    owner_id: Optional[str] = None,  # [FIX 2026-01-22] 新增 owner_id 参数，用于多客户端会话隔离（已逐步下线）
    conversation_id: Optional[str] = None
) -> RecoveryResult:
    """
    为工具调用恢复签名（6层策略）

    Args:
        tool_id: 原始工具调用ID
        encoded_tool_id: 编码的工具调用ID（可能包含签名）
        client_signature: 客户端提供的签名
        context_signature: 上下文中的签名
        session_id: 会话ID
        use_placeholder_fallback: 是否在所有策略失败时使用占位符
        owner_id: 可选的所有者ID，用于多客户端会话隔离（已逐步下线）
        conversation_id: 可选的会话标识（优先使用）

    Returns:
        RecoveryResult: 恢复结果
    """
    # 延迟导入避免循环依赖
    from akarins_gateway.signature_cache import (
        get_tool_signature,
        get_session_signature,
        get_last_signature
    )
    from akarins_gateway.converters.thoughtSignature_fix import decode_tool_id_and_signature
    effective_owner_id = owner_id or conversation_id

    # 优先级 1: 客户端提供的签名
    if is_valid_signature(client_signature):
        log.info(f"[SIGNATURE_RECOVERY] Tool Layer 1: Client signature found for tool_id={tool_id}")
        return RecoveryResult(
            signature=client_signature,
            source=RecoverySource.CLIENT
        )

    # 优先级 2: 上下文中的签名
    if is_valid_signature(context_signature):
        log.info(f"[SIGNATURE_RECOVERY] Tool Layer 2: Context signature found for tool_id={tool_id}")
        return RecoveryResult(
            signature=context_signature,
            source=RecoverySource.CONTEXT
        )

    # 优先级 3: 从编码的工具ID中解码（gcli2api 独有优势）
    _, decoded_sig = decode_tool_id_and_signature(encoded_tool_id)
    if is_valid_signature(decoded_sig):
        log.info(f"[SIGNATURE_RECOVERY] Tool Layer 3: Decoded from encoded tool_id={tool_id}")
        return RecoveryResult(
            signature=decoded_sig,
            source=RecoverySource.ENCODED_TOOL_ID
        )

    # 优先级 4: Session Cache
    # [FIX 2026-01-22] 传递 owner_id 进行会话隔离
    if session_id:
        session_sig = get_session_signature(session_id, effective_owner_id)
        if is_valid_signature(session_sig):
            log.info(f"[SIGNATURE_RECOVERY] Tool Layer 4: Session cache hit for tool_id={tool_id}")
            return RecoveryResult(
                signature=session_sig,
                source=RecoverySource.SESSION_CACHE
            )

    # 优先级 5: Tool Cache
    # [FIX 2026-01-22] 传递 owner_id 进行会话隔离
    tool_sig = get_tool_signature(tool_id, effective_owner_id)
    if is_valid_signature(tool_sig):
        log.info(f"[SIGNATURE_RECOVERY] Tool Layer 5: Tool cache hit for tool_id={tool_id}")
        return RecoveryResult(
            signature=tool_sig,
            source=RecoverySource.TOOL_CACHE
        )

    # 优先级 6: 最近缓存的签名（fallback）
    last_sig = get_last_signature()
    if is_valid_signature(last_sig):
        log.info(f"[SIGNATURE_RECOVERY] Tool Layer 6: Using last cached signature for tool_id={tool_id}")
        return RecoveryResult(
            signature=last_sig,
            source=RecoverySource.LAST_SIGNATURE
        )

    # 所有策略都失败
    if use_placeholder_fallback:
        log.warning(f"[SIGNATURE_RECOVERY] All strategies failed for tool_id={tool_id}, using placeholder")
        return RecoveryResult(
            signature=SKIP_SIGNATURE_VALIDATOR,
            source=RecoverySource.PLACEHOLDER
        )

    log.warning(f"[SIGNATURE_RECOVERY] All strategies failed for tool_id={tool_id}, no signature available")
    return RecoveryResult(
        signature=None,
        source=RecoverySource.NONE
    )


def get_recovery_stats() -> Dict[str, Any]:
    """
    获取签名恢复统计信息

    Returns:
        Dict: 统计信息
    """
    from akarins_gateway.signature_cache import get_cache_stats

    cache_stats = get_cache_stats()

    return {
        "cache_stats": cache_stats,
        "recovery_layers": [
            "1. Client signature",
            "2. Context signature",
            "3. Thinking Hash Cache (content-based)",
            "4. Session Cache",
            "5. Tool Cache",
            "6. Last Signature (fallback)",
            "7. Placeholder (skip_thought_signature_validator)"
        ],
        "recovery_layers_tool_use": [
            "1. Client signature",
            "2. Context signature",
            "3. Encoded Tool ID (gcli2api unique)",
            "4. Session Cache",
            "5. Tool Cache",
            "6. Last Signature (fallback)",
            "7. Placeholder (skip_thought_signature_validator)"
        ]
    }


# 导出公共接口
__all__ = [
    "RecoverySource",
    "RecoveryResult",
    "MIN_SIGNATURE_LENGTH",
    "SKIP_SIGNATURE_VALIDATOR",
    "is_valid_signature",
    "recover_signature_for_thinking",
    "recover_signature_for_tool_use",
    "get_recovery_stats"
]
