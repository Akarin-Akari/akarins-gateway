"""
Conversation State Manager - 会话状态管理器

SCID 状态机的核心组件，负责维护每个会话的权威历史记录。

核心职责:
1. 维护每个 SCID 的权威历史消息列表
2. 在 IDE 回放变形消息时，使用权威历史替换
3. 管理签名的会话级缓存
4. 支持 SQLite 持久化
5. [NEW 2026-01-24] 智能上下文管理和压缩

设计原则:
- 网关是权威状态机，不信任 IDE 回放的历史
- 每次响应后更新权威历史
- 使用内存缓存 + SQLite 持久化的双层架构
- 线程安全
- [NEW] 自动压缩过长的权威历史，防止 Large payload 错误

Author: Claude Sonnet 4.5 (浮浮酱)
Date: 2026-01-17
Updated: 2026-01-24 (添加上下文压缩)
"""

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Set, Tuple

from akarins_gateway.cache.signature_database import SignatureDatabase

log = logging.getLogger("gcli2api.state_manager")


# ====================== 上下文管理配置 ======================

# 权威历史的最大消息数量（超过则触发压缩）
# [FIX 2026-02-07] 大幅提高阈值，避免误触发压缩破坏工具链
# 只有在收到上游明确的请求体过大错误时才会触发紧急压缩
MAX_HISTORY_MESSAGES = 200  # 提高到200条，基本不会自动触发

# 压缩后保留的消息数量
# [FIX 2026-02-07] 同步提高，确保压缩时保留足够多的消息
COMPRESSED_KEEP_MESSAGES = 150  # 压缩后仍保留150条

# 是否启用自动压缩
# [FIX 2026-02-07] ⚠️ 关键修复：禁用自动压缩！
# 自动压缩会破坏工具链完整性，导致 tool_use_result_mismatch 错误
# 只有在收到上游明确的请求体过大错误时才通过 trigger_emergency_compress() 触发
AUTO_COMPRESS_ENABLED = False  # 禁用自动压缩，保护工具链完整性

# [NEW 2026-02-03] 无状态模式客户端类型列表
# 这些客户端有自己的状态管理，不需要服务端 SCID 架构
# 与 client_detector.py 中的 STATELESS_CLIENTS 保持一致
#
# [FIX 2026-02-04] Claude Code 从此列表移除
# 问题：Claude Code 需要签名恢复和工具链缓存功能
# 解决：Claude Code 现在使用轻量级签名恢复模式（见 scid.py）
STATELESS_CLIENT_TYPES = {"cline", "aider", "continue_dev", "openai_api"}
# "claude_code" 已移除 - 需要签名恢复，移至 SIGNATURE_RECOVERY_ONLY_CLIENTS (client_detector.py)

# 工具结果内容压缩开关（默认关闭，避免长工具链信息被截断后触发重复调用）
TOOL_RESULT_COMPRESSION_ENABLED = (
    os.getenv("SCID_TOOL_RESULT_COMPRESSION_ENABLED", "false").strip().lower() == "true"
)

# 工具结果的最大字符数（当 TOOL_RESULT_COMPRESSION_ENABLED=true 时生效）
MAX_TOOL_RESULT_CHARS = int(os.getenv("SCID_MAX_TOOL_RESULT_CHARS", "5000"))


@dataclass
class ConversationState:
    """
    会话状态数据结构

    存储单个会话的完整状态信息，包括权威历史、签名等。

    [FIX 2026-02-07] 新增 tool_results_cache 字段
    用于缓存会话内所有工具调用结果，防止工具结果丢失导致：
    - tool_use_result_mismatch 错误
    - 系统提示词重复注入（每次工具调用都触发自我介绍）
    """
    scid: str                          # Server Conversation ID
    client_type: str                   # 客户端类型 ('cursor' | 'augment' | 'claude_code' | 'unknown')
    authoritative_history: List[Dict]  # 权威历史消息列表
    last_signature: Optional[str]      # 最后一个有效签名
    created_at: datetime               # 创建时间
    updated_at: datetime               # 最后更新时间
    access_count: int = 0              # 访问次数
    # [NEW 2026-02-07] 工具调用结果缓存
    # key: tool_use_id, value: tool_result 消息
    # 用于在工具结果丢失时恢复，防止工具链断裂
    tool_results_cache: Dict[str, Dict] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式（用于序列化）"""
        return {
            "scid": self.scid,
            "client_type": self.client_type,
            "authoritative_history": self.authoritative_history,
            "last_signature": self.last_signature,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "access_count": self.access_count,
            # [NEW 2026-02-07] 序列化工具结果缓存
            "tool_results_cache": self.tool_results_cache,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationState":
        """从字典创建实例（用于反序列化）"""
        return cls(
            scid=data["scid"],
            client_type=data["client_type"],
            authoritative_history=data["authoritative_history"],
            last_signature=data.get("last_signature"),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            access_count=data.get("access_count", 0),
            # [NEW 2026-02-07] 反序列化工具结果缓存
            tool_results_cache=data.get("tool_results_cache", {}),
        )


class ConversationStateManager:
    """
    会话状态管理器 - SCID 状态机核心

    职责:
    1. 维护每个 SCID 的权威历史
    2. 在 IDE 回放变形消息时，使用权威历史替换
    3. 管理签名的会话级缓存
    4. [NEW 2026-02-02] 支持中间状态检查点，用于中断恢复

    设计原则:
    - 网关是权威状态机，不信任 IDE 回放的历史
    - 每次响应后更新权威历史
    - 支持 SQLite 持久化

    Usage:
        manager = ConversationStateManager(db)

        # 获取或创建状态
        state = manager.get_or_create_state(scid, client_type)

        # 更新权威历史
        manager.update_authoritative_history(
            scid,
            new_messages=[{"role": "user", "content": "Hello"}],
            response_message={"role": "assistant", "content": "Hi!"},
            signature="sig_123"
        )

        # 合并客户端历史
        merged = manager.merge_with_client_history(scid, client_messages)

        # [NEW 2026-02-02] 检查点操作
        manager.save_checkpoint(scid, checkpoint_data)
        checkpoint = manager.get_checkpoint(scid)
        manager.clear_checkpoint(scid)
    """

    def __init__(self, db: Optional[SignatureDatabase] = None):
        """
        初始化状态管理器

        Args:
            db: SignatureDatabase 实例，用于持久化
        """
        self._memory_cache: Dict[str, ConversationState] = {}
        self._db = db
        self._lock = threading.Lock()

        # [NEW 2026-02-02] 中间状态检查点存储
        # 用于在流中断时保存已收到的内容，供下次请求恢复
        self._checkpoints: Dict[str, Dict] = {}
        self._checkpoint_lock = threading.Lock()

        log.info("[STATE_MANAGER] ConversationStateManager initialized")

    def get_or_create_state(self, scid: str, client_type: str) -> ConversationState:
        """
        获取或创建会话状态

        优先从内存缓存获取，其次从 SQLite，最后创建新状态

        [NEW 2026-02-03] 无状态模式客户端（CLI 工具）会跳过状态管理
        - 返回一个空的临时状态，不持久化
        - 避免与 CLI 自己的状态管理冲突

        Args:
            scid: Server Conversation ID
            client_type: 客户端类型

        Returns:
            ConversationState 实例
        """
        if not scid:
            raise ValueError("scid cannot be empty")

        # [NEW 2026-02-03] 无状态模式客户端绕过 SCID 架构
        if client_type in STATELESS_CLIENT_TYPES:
            log.info(
                f"[STATE_MANAGER] Stateless client detected: {client_type}, "
                f"bypassing SCID state management (scid={scid[:20]}...)"
            )
            # 返回一个临时的空状态，不缓存也不持久化
            now = datetime.now()
            return ConversationState(
                scid=scid,
                client_type=client_type,
                authoritative_history=[],
                last_signature=None,
                created_at=now,
                updated_at=now,
                access_count=0,
            )

        with self._lock:
            # 1. 尝试从内存缓存获取
            if scid in self._memory_cache:
                state = self._memory_cache[scid]
                state.access_count += 1
                log.debug(f"[STATE_MANAGER] Memory cache hit: scid={scid[:20]}...")
                return state

            # 2. 尝试从 SQLite 加载
            state = self._load_state(scid)
            if state:
                # 回填到内存缓存
                self._memory_cache[scid] = state
                state.access_count += 1
                log.info(f"[STATE_MANAGER] State loaded from SQLite: scid={scid[:20]}...")
                return state

            # 3. 创建新状态
            now = datetime.now()
            state = ConversationState(
                scid=scid,
                client_type=client_type,
                authoritative_history=[],
                last_signature=None,
                created_at=now,
                updated_at=now,
                access_count=1,
            )

            # 存储到内存缓存
            self._memory_cache[scid] = state

            # 持久化到 SQLite
            self._persist_state(state)

            log.info(f"[STATE_MANAGER] New state created: scid={scid[:20]}..., client_type={client_type}")
            return state

    def update_authoritative_history(
        self,
        scid: str,
        new_messages: List[Dict],
        response_message: Dict,
        signature: Optional[str] = None
    ) -> None:
        """
        更新权威历史

        在收到 Claude API 响应后调用，将响应消息追加到权威历史

        Args:
            scid: 会话 ID
            new_messages: 本轮新增的用户消息
            response_message: Claude 响应消息
            signature: 响应中的签名 (如果有)
        """
        if not scid:
            log.warning("[STATE_MANAGER] update_authoritative_history: scid is empty")
            return

        with self._lock:
            state = self._memory_cache.get(scid)
            if not state:
                log.warning(f"[STATE_MANAGER] State not found for scid={scid[:20]}..., cannot update history")
                return

            # 追加新消息到权威历史（同时压缩工具结果）
            for msg in new_messages:
                # 避免重复添加
                if not self._message_exists_in_history(msg, state.authoritative_history):
                    # [NEW 2026-01-24] 压缩工具结果后再添加
                    state.authoritative_history.append(self._compress_tool_result(msg))

            # 追加响应消息（同时压缩工具结果）
            if not self._message_exists_in_history(response_message, state.authoritative_history):
                # [NEW 2026-01-24] 压缩工具结果后再添加
                state.authoritative_history.append(self._compress_tool_result(response_message))

            # [NEW 2026-01-24] 自动压缩权威历史
            if AUTO_COMPRESS_ENABLED and len(state.authoritative_history) > MAX_HISTORY_MESSAGES:
                log.warning(
                    f"[STATE_MANAGER] History size exceeds limit: "
                    f"{len(state.authoritative_history)} > {MAX_HISTORY_MESSAGES}, "
                    f"triggering compression..."
                )
                state.authoritative_history = self._compress_authoritative_history(state.authoritative_history)

            # 更新签名
            if signature:
                state.last_signature = signature

            # 更新时间戳
            state.updated_at = datetime.now()

            # [NEW 2026-02-07] 缓存工具结果，用于后续恢复丢失的工具结果
            # 这可以防止系统提示词重复注入问题（每次工具调用都触发自我介绍）
            all_new_messages = new_messages + [response_message]
            cached_count = self._cache_tool_results_from_messages(state, all_new_messages)
            if cached_count > 0:
                log.debug(
                    f"[STATE_MANAGER] Cached {cached_count} tool results from new messages"
                )

            # 持久化到 SQLite
            self._persist_state(state)

            log.info(
                f"[STATE_MANAGER] History updated: scid={scid[:20]}..., "
                f"total_messages={len(state.authoritative_history)}, "
                f"has_signature={signature is not None}, "
                f"tool_cache_size={len(state.tool_results_cache)}"
            )

    def get_authoritative_history(self, scid: str) -> Optional[List[Dict]]:
        """
        获取权威历史

        用于替换 IDE 回放的可能变形的历史

        Args:
            scid: 会话 ID

        Returns:
            权威历史消息列表，如果不存在则返回 None
        """
        with self._lock:
            state = self._memory_cache.get(scid)
            if state:
                log.debug(f"[STATE_MANAGER] Authoritative history retrieved: scid={scid[:20]}..., messages={len(state.authoritative_history)}")
                return state.authoritative_history.copy()

            # 尝试从 SQLite 加载
            state = self._load_state(scid)
            if state:
                self._memory_cache[scid] = state
                log.info(f"[STATE_MANAGER] Authoritative history loaded from SQLite: scid={scid[:20]}...")
                return state.authoritative_history.copy()

            return None

    def reset_state_for_new_chat(
        self,
        scid: str,
        *,
        client_type: Optional[str] = None,
        reason: str = "",
    ) -> bool:
        """
        重置指定 SCID 的会话状态，用于“新会话误命中旧 SCID”场景。

        设计目标：
        - 清空权威历史与工具结果缓存，避免旧工具链污染新会话
        - 清空 last_signature，避免思维签名跨会话误恢复
        - 保留同一个 scid（不改 ID），减少对现有链路影响
        """
        if not scid:
            return False

        with self._lock:
            state = self._memory_cache.get(scid)
            if state is None:
                state = self._load_state(scid)

            if state is None:
                log.warning(f"[STATE_MANAGER] reset_state_for_new_chat: state not found (scid={scid[:20]}...)")
                return False

            old_history_len = len(state.authoritative_history)
            old_tool_cache_len = len(state.tool_results_cache)

            now = datetime.now()
            state.authoritative_history = []
            state.last_signature = None
            state.updated_at = now
            state.access_count = 0
            state.tool_results_cache = {}
            if client_type:
                state.client_type = client_type

            self._memory_cache[scid] = state
            self._persist_state(state)

        # 清理检查点（不在 _lock 内，避免锁顺序问题）
        try:
            self.clear_checkpoint(scid)
        except Exception as e:
            log.warning(f"[STATE_MANAGER] Failed to clear checkpoint during reset: {e}")

        log.warning(
            f"[STATE_MANAGER] Session state reset for new chat: scid={scid[:20]}..., "
            f"history={old_history_len}->0, tool_cache={old_tool_cache_len}->0, "
            f"reason={reason[:200] if reason else 'n/a'}"
        )
        return True

    def merge_with_client_history(
        self,
        scid: str,
        client_messages: List[Dict],
        client_type: str = None
    ) -> List[Dict]:
        """
        合并客户端历史与权威历史

        策略:
        1. [NEW 2026-02-03] 无状态模式客户端直接返回客户端消息（不合并）
        2. [NEW 2026-01-24] 智能工具链恢复：检测孤儿tool_use并从权威历史恢复tool_result
        3. 使用位置 + role 匹配，而不是纯内容 hash
        4. 对于权威历史范围内的消息，使用权威版本（防止 IDE 变形）
        5. 对于超出权威历史的新消息，追加到结果
        6. 返回合并后的消息列表
        7. 自动压缩工具结果

        这是核心方法，用于处理 IDE 变形问题

        Args:
            scid: 会话 ID
            client_messages: 客户端发送的消息列表
            client_type: [NEW] 客户端类型，用于判断是否为无状态模式

        Returns:
            合并后的消息列表
        """
        if not scid:
            log.warning("[STATE_MANAGER] merge_with_client_history: scid is empty")
            return client_messages

        # [NEW 2026-02-03] 无状态模式客户端直接返回客户端消息
        if client_type and client_type in STATELESS_CLIENT_TYPES:
            log.info(
                f"[STATE_MANAGER] Stateless client ({client_type}): "
                f"bypassing history merge, returning {len(client_messages)} client messages as-is"
            )
            # 仍然压缩工具结果以节省 tokens
            return [self._compress_tool_result(msg) for msg in client_messages]

        with self._lock:
            state = self._memory_cache.get(scid)

            # 如果没有权威历史，直接返回客户端消息（但压缩工具结果）
            if not state or not state.authoritative_history:
                log.debug(f"[STATE_MANAGER] No authoritative history for scid={scid[:20]}..., using client messages")
                # [NEW 2026-01-24] 即使没有权威历史，也压缩客户端的工具结果
                return [self._compress_tool_result(msg) for msg in client_messages]

            authoritative = state.authoritative_history
            auth_len = len(authoritative)

            # [NEW 2026-01-24] Step 1: 检测客户端消息中的tool_use，构建tool_call_id集合
            client_tool_calls = self._extract_tool_calls(client_messages)
            client_tool_results = self._extract_tool_results(client_messages)
            
            # 检测孤儿tool_use（有tool_call但没有对应result）
            orphan_tool_calls = client_tool_calls - client_tool_results
            
            if orphan_tool_calls:
                log.warning(
                    f"[STATE_MANAGER] Detected {len(orphan_tool_calls)} orphan tool_use in client messages, "
                    f"attempting recovery..."
                )

                # [NEW 2026-02-07] 优先从工具结果缓存恢复（更快、更准确）
                if state.tool_results_cache:
                    # 检查缓存中有多少可恢复的
                    recoverable_from_cache = orphan_tool_calls & set(state.tool_results_cache.keys())

                    if recoverable_from_cache:
                        log.info(
                            f"[STATE_MANAGER] Found {len(recoverable_from_cache)}/{len(orphan_tool_calls)} "
                            f"tool results in cache, attempting cache recovery first..."
                        )

                        # 从缓存恢复
                        merged = self._recover_tool_results_from_cache(
                            state, orphan_tool_calls, client_messages
                        )

                        # 检查是否全部恢复成功
                        merged_tool_results = self._extract_tool_results(merged)
                        remaining_orphans = orphan_tool_calls - merged_tool_results

                        if not remaining_orphans:
                            log.info(
                                f"[STATE_MANAGER] All orphan tool_use recovered from cache: "
                                f"scid={scid[:20]}..., "
                                f"client={len(client_messages)}, "
                                f"merged={len(merged)}, "
                                f"recovered_from_cache={len(recoverable_from_cache)}"
                            )
                            # 压缩工具结果后返回
                            return [self._compress_tool_result(msg) for msg in merged]
                        else:
                            log.warning(
                                f"[STATE_MANAGER] Cache recovery incomplete, "
                                f"{len(remaining_orphans)} orphans remaining, "
                                f"falling back to authoritative history..."
                            )
                            # 继续使用权威历史恢复剩余的
                            client_messages = merged
                            orphan_tool_calls = remaining_orphans

                # 从权威历史恢复（原有逻辑）
                merged = self._merge_with_tool_chain_recovery(
                    client_messages, authoritative, orphan_tool_calls
                )

                log.info(
                    f"[STATE_MANAGER] Tool chain recovery completed: "
                    f"scid={scid[:20]}..., "
                    f"client={len(client_messages)}, "
                    f"merged={len(merged)}, "
                    f"recovered_tools={len(orphan_tool_calls)}, "
                    f"cache_size={len(state.tool_results_cache)}"
                )

                return merged

            # 合并策略：位置匹配（原有逻辑）
            merged = []

            # 1. 对于权威历史范围内的消息，使用权威版本
            for i in range(min(auth_len, len(client_messages))):
                auth_msg = authoritative[i]
                client_msg = client_messages[i]

                # 如果 role 匹配，使用权威版本（即使内容不同）
                if auth_msg.get("role") == client_msg.get("role"):
                    # [NEW 2026-01-24] 压缩工具结果
                    merged.append(self._compress_tool_result(auth_msg))
                    log.debug(
                        f"[STATE_MANAGER] Position {i}: Using authoritative version "
                        f"(role={auth_msg.get('role')})"
                    )
                else:
                    # [FIX 2026-02-07] role 不匹配时，需要保留两边消息以维护工具链完整性
                    # 场景：auth=assistant(含tool_use), client=tool(含tool_result)
                    # 如果只用 client 版本，会丢失 tool_use，导致 tool_result 变成孤儿
                    auth_role = auth_msg.get("role")
                    client_role = client_msg.get("role")

                    # 检查是否是 tool chain 分裂场景
                    if auth_role == "assistant" and client_role == "tool":
                        # 权威历史有 assistant(tool_use)，客户端有 tool(tool_result)
                        # 需要保留两者！先插入 auth 的 assistant，再插入 client 的 tool
                        merged.append(self._compress_tool_result(auth_msg))
                        merged.append(self._compress_tool_result(client_msg))
                        log.warning(
                            f"[STATE_MANAGER] Position {i}: Tool chain split detected "
                            f"(auth={auth_role}, client={client_role}), "
                            f"preserving BOTH messages to maintain tool chain integrity"
                        )
                    elif auth_role == "tool" and client_role == "assistant":
                        # 反向场景：权威有 tool，客户端有 assistant
                        # 也需要保留两者
                        merged.append(self._compress_tool_result(auth_msg))
                        merged.append(self._compress_tool_result(client_msg))
                        log.warning(
                            f"[STATE_MANAGER] Position {i}: Reverse tool chain split "
                            f"(auth={auth_role}, client={client_role}), "
                            f"preserving BOTH messages"
                        )
                    else:
                        # 其他 role 不匹配情况，使用客户端版本（原有逻辑）
                        merged.append(self._compress_tool_result(client_msg))
                        log.warning(
                            f"[STATE_MANAGER] Position {i}: Role mismatch "
                            f"(auth={auth_role}, client={client_role}), "
                            f"using client version"
                        )

            # 2. 如果客户端有更多消息，追加新消息
            new_messages_count = 0
            if len(client_messages) > auth_len:
                for i in range(auth_len, len(client_messages)):
                    merged.append(self._compress_tool_result(client_messages[i]))
                    new_messages_count += 1
                    log.debug(
                        f"[STATE_MANAGER] Position {i}: New message "
                        f"(role={client_messages[i].get('role')})"
                    )

            # 3. 如果权威历史有更多消息（客户端历史被截断），保留权威版本
            if auth_len > len(client_messages):
                for i in range(len(client_messages), auth_len):
                    merged.append(self._compress_tool_result(authoritative[i]))
                    log.debug(
                        f"[STATE_MANAGER] Position {i}: Authoritative message "
                        f"(role={authoritative[i].get('role')})"
                    )

            log.info(
                f"[STATE_MANAGER] History merged: scid={scid[:20]}..., "
                f"authoritative={auth_len}, "
                f"client={len(client_messages)}, "
                f"merged={len(merged)}, "
                f"new={new_messages_count}"
            )

            # [FIX 2026-01-24] ⚠️ 关键修复！合并后必须检查并压缩历史
            # 注意：_compress_authoritative_history 会智能处理工具链完整性
            # - 如果≤20条：只压缩工具结果，不删除消息
            # - 如果>20条：删除旧消息，但确保工具链完整性
            if len(merged) > MAX_HISTORY_MESSAGES:
                log.warning(
                    f"[STATE_MANAGER] Merged history exceeds limit ({len(merged)} > {MAX_HISTORY_MESSAGES}), "
                    f"applying intelligent compression (tool chain aware)..."
                )
                merged = self._compress_authoritative_history(merged)
                log.info(
                    f"[STATE_MANAGER] Compressed merged history: "
                    f"{len(merged)} messages (tool chains preserved)"
                )

            return merged

    def get_last_signature(self, scid: str) -> Optional[str]:
        """
        获取会话的最后一个有效签名

        Args:
            scid: 会话 ID

        Returns:
            签名字符串，如果不存在则返回 None
        """
        with self._lock:
            state = self._memory_cache.get(scid)
            if state:
                return state.last_signature

            # 尝试从 SQLite 加载
            state = self._load_state(scid)
            if state:
                self._memory_cache[scid] = state
                return state.last_signature

            return None

    def cleanup_expired(self, max_age_hours: int = 24) -> int:
        """
        清理过期的会话状态

        Args:
            max_age_hours: 最大保留时间（小时）

        Returns:
            清理的会话数量
        """
        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)

        with self._lock:
            # 清理内存缓存
            expired_scids = [
                scid for scid, state in self._memory_cache.items()
                if state.updated_at < cutoff_time
            ]

            for scid in expired_scids:
                del self._memory_cache[scid]

            memory_cleaned = len(expired_scids)

            # 清理 SQLite
            db_cleaned = 0
            if self._db:
                db_cleaned = self._db.cleanup_expired_states()

            total_cleaned = memory_cleaned + db_cleaned

            if total_cleaned > 0:
                log.info(
                    f"[STATE_MANAGER] Cleaned up {total_cleaned} expired states "
                    f"(memory={memory_cleaned}, db={db_cleaned})"
                )

            return total_cleaned

    def _persist_state(self, state: ConversationState) -> None:
        """
        持久化状态到 SQLite

        Args:
            state: 要持久化的状态
        """
        if not self._db:
            return

        try:
            history_json = json.dumps(state.authoritative_history, ensure_ascii=False)

            success = self._db.store_conversation_state(
                scid=state.scid,
                client_type=state.client_type,
                history=history_json,
                signature=state.last_signature,
            )

            if success:
                log.debug(f"[STATE_MANAGER] State persisted: scid={state.scid[:20]}...")
            else:
                log.warning(f"[STATE_MANAGER] Failed to persist state: scid={state.scid[:20]}...")

        except Exception as e:
            log.error(f"[STATE_MANAGER] Error persisting state: {e}")

    def _load_state(self, scid: str) -> Optional[ConversationState]:
        """
        从 SQLite 加载状态

        Args:
            scid: 会话 ID

        Returns:
            ConversationState 实例，如果不存在则返回 None
        """
        if not self._db:
            return None

        try:
            data = self._db.get_conversation_state(scid)
            if not data:
                return None

            state = ConversationState(
                scid=data["scid"],
                client_type=data["client_type"],
                authoritative_history=data["authoritative_history"],
                last_signature=data.get("last_signature"),
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
                access_count=data.get("access_count", 0),
            )

            return state

        except Exception as e:
            log.error(f"[STATE_MANAGER] Error loading state: {e}")
            return None

    def _message_hash(self, message: Dict) -> str:
        """
        计算消息的内容 hash

        用于消息去重和匹配

        Args:
            message: 消息字典

        Returns:
            消息的 SHA256 hash
        """
        # 提取关键字段用于 hash
        key_fields = {
            "role": message.get("role"),
            "content": message.get("content"),
        }

        # 如果有 tool_calls，也包含进来（兼容 list/dict 两种历史格式）
        if "tool_calls" in message:
            key_fields["tool_calls"] = self._get_tool_calls_as_list(message)

        # 如果有 tool_call_id，也包含进来
        if "tool_call_id" in message:
            key_fields["tool_call_id"] = message["tool_call_id"]

        # 序列化并计算 hash
        content = json.dumps(key_fields, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _message_exists_in_history(self, message: Dict, history: List[Dict]) -> bool:
        """
        检查消息是否已存在于历史中

        Args:
            message: 要检查的消息
            history: 历史消息列表

        Returns:
            如果消息已存在则返回 True
        """
        msg_hash = self._message_hash(message)
        return any(self._message_hash(h) == msg_hash for h in history)

    def _normalize_tool_result_format(self, message: Dict) -> Dict:
        """
        规范化tool_result格式
        
        问题：权威历史可能包含Gemini格式的functionResponse，
        当恢复时直接发送给Claude API会导致400错误
        
        策略：
        1. 检测Gemini格式（parts + functionResponse）
        2. 检测functionResponse.name是否为tool_call_id（错误格式）
        3. 转换为Anthropic格式或修复name字段
        
        Args:
            message: 消息字典
            
        Returns:
            规范化后的消息
        """
        import copy
        
        normalized = copy.deepcopy(message)

        # [FIX 2026-02-08] 兼容历史脏数据：tool_calls 可能被错误写成 dict(index->tool_call)
        # 统一规范为 OpenAI 标准 list，避免后续工具链提取失败导致孤儿 tool_result。
        if isinstance(normalized.get("tool_calls"), dict):
            normalized["tool_calls"] = self._get_tool_calls_as_list(normalized)
        
        # 检查是否是Gemini格式
        parts = normalized.get("parts")
        if not isinstance(parts, list):
            return normalized
        
        # 检查是否有functionResponse
        has_function_response = False
        for part in parts:
            if isinstance(part, dict) and "functionResponse" in part:
                has_function_response = True
                fr = part["functionResponse"]
                fr_id = fr.get("id")
                fr_name = fr.get("name")
                
                # 检查name是否是tool_call_id（错误格式）
                if fr_name and (fr_name.startswith("tool_toolu") or fr_name.startswith("toolu")):
                    log.warning(
                        f"[STATE_MANAGER] Detected invalid functionResponse.name (is tool_call_id): "
                        f"id={fr_id[:20] if fr_id else 'None'}..., "
                        f"name={fr_name[:30]}..."
                    )
                    
                    # ❌ 移除错误的name字段
                    # Claude API会因为这种错误格式返回400
                    # Gemini API不需要functionResponse的name字段
                    if "name" in fr:
                        del fr["name"]
                        log.info(
                            f"[STATE_MANAGER] Removed invalid name from functionResponse: "
                            f"id={fr_id[:20] if fr_id else 'None'}..."
                        )
        
        if has_function_response:
            log.debug(f"[STATE_MANAGER] Normalized Gemini format message")
        
        return normalized

    def _get_tool_calls_as_list(self, message: Dict) -> List[Dict]:
        """
        读取并规范化 OpenAI assistant.tool_calls 字段。

        兼容两种形态：
        1. 标准 list[dict]
        2. 历史异常 dict[index -> dict]
        """
        if not isinstance(message, dict):
            return []

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            return [tc for tc in tool_calls if isinstance(tc, dict)]

        if isinstance(tool_calls, dict):
            normalized_calls: List[Dict] = []

            def _sort_key(raw_key: Any) -> tuple:
                try:
                    return (0, int(raw_key))
                except Exception:
                    return (1, str(raw_key))

            for key in sorted(tool_calls.keys(), key=_sort_key):
                call = tool_calls.get(key)
                if isinstance(call, dict):
                    normalized_calls.append(call)

            if normalized_calls:
                log.debug(
                    f"[STATE_MANAGER] Normalized legacy dict tool_calls -> list: "
                    f"count={len(normalized_calls)}"
                )

            return normalized_calls

        return []

    def _compress_tool_result(self, message: Dict, max_chars: int = MAX_TOOL_RESULT_CHARS) -> Dict:
        """
        压缩工具结果消息中的内容
        
        策略：
        1. [NEW 2026-01-24] 规范化消息格式（Gemini → Anthropic）
        2. 检测 tool_call_id 确认是工具结果
        3. 截断过长的 content
        4. 保留前N个字符 + "...[truncated]..."
        
        Args:
            message: 消息字典
            max_chars: 最大保留字符数
            
        Returns:
            压缩后的消息（深拷贝）
        """
        import copy
        
        # [NEW 2026-01-24] Step 1: 规范化格式
        normalized = self._normalize_tool_result_format(message)
        
        # 深拷贝避免修改原消息
        compressed = copy.deepcopy(normalized)
        
        # Step 2: 检查是否是工具结果消息
        # 工具结果可能有 tool_call_id 字段，或者 role='tool'
        is_tool_result = compressed.get("tool_call_id") or compressed.get("role") == "tool"
        
        # 也检查 Gemini 格式的 functionResponse
        if not is_tool_result and "parts" in compressed:
            parts = compressed.get("parts", [])
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and "functionResponse" in part:
                        is_tool_result = True
                        log.debug(f"[STATE_MANAGER] Detected Gemini functionResponse in parts, marking as tool result")
                        break
        
        if not is_tool_result:
            log.debug(f"[STATE_MANAGER] Not a tool result message, skipping compression (role={compressed.get('role')})")
            return compressed
        
        log.debug(f"[STATE_MANAGER] Processing tool result message for compression (role={compressed.get('role')})")

        # [FIX 2026-02-08] 默认关闭常规 tool_result 截断，避免长工具链下关键信息丢失
        # 说明：
        # 1) 关闭后仍保留格式规范化（_normalize_tool_result_format）
        # 2) 真正的“请求体过大”场景由 trigger_emergency_compress() 处理
        if not TOOL_RESULT_COMPRESSION_ENABLED:
            return compressed
        
        # Step 3: 压缩内容
        content = compressed.get("content", "")
        if isinstance(content, str) and len(content) > max_chars:
            # 截断并添加提示
            truncated_content = content[:max_chars] + f"\n\n...[SCID compressed {len(content) - max_chars} chars to save context]..."
            compressed["content"] = truncated_content
            
            log.info(
                f"[STATE_MANAGER] Tool result compressed: "
                f"{len(content)} -> {len(truncated_content)} chars "
                f"(saved {len(content) - len(truncated_content)} chars)"
            )
        
        # 压缩Gemini格式的output
        if "parts" in compressed:
            parts = compressed.get("parts", [])
            if isinstance(parts, list):
                for i, part in enumerate(parts):
                    if isinstance(part, dict) and "functionResponse" in part:
                        fr = part["functionResponse"]
                        response = fr.get("response", {})
                        output = response.get("output", "")
                        
                        log.debug(
                            f"[STATE_MANAGER] Found Gemini functionResponse in part {i}: "
                            f"output_type={type(output).__name__}, output_len={len(output) if isinstance(output, str) else 'N/A'}"
                        )
                        
                        if isinstance(output, str) and len(output) > max_chars:
                            truncated_output = output[:max_chars] + f"\n\n...[SCID compressed {len(output) - max_chars} chars]..."
                            
                            # [FIX 2026-01-24] 确保response字典存在
                            if "response" not in fr:
                                fr["response"] = {}
                            
                            fr["response"]["output"] = truncated_output
                            
                            log.info(
                                f"[STATE_MANAGER] ✅ Gemini functionResponse output compressed: "
                                f"{len(output)} -> {len(truncated_output)} chars "
                                f"(saved {len(output) - len(truncated_output)} chars)"
                            )
                        else:
                            if not isinstance(output, str):
                                log.warning(
                                    f"[STATE_MANAGER] ⚠️ functionResponse output is not a string: "
                                    f"type={type(output).__name__}"
                                )
                            elif len(output) <= max_chars:
                                log.debug(
                                    f"[STATE_MANAGER] functionResponse output within limit: "
                                    f"{len(output)} <= {max_chars}"
                                )
        
        return compressed

    def _extract_tool_calls(self, messages: List[Dict]) -> set:
        """
        从消息列表中提取所有 tool_call_id
        
        Args:
            messages: 消息列表
            
        Returns:
            tool_call_id 集合
        """
        tool_calls = set()
        
        for msg in messages:
            # 检查 tool_calls 字段（OpenAI格式）
            for call in self._get_tool_calls_as_list(msg):
                if call.get("id"):
                    tool_calls.add(call["id"])
            
            # 检查 content 中的 tool_use（数组格式）
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        if item.get("id"):
                            tool_calls.add(item["id"])
        
        return tool_calls

    def _extract_tool_results(self, messages: List[Dict]) -> set:
        """
        从消息列表中提取所有 tool_result 对应的 tool_call_id
        
        Args:
            messages: 消息列表
            
        Returns:
            已有result的tool_call_id集合
        """
        tool_results = set()
        
        for msg in messages:
            # 检查 tool_call_id 字段（Anthropic格式）
            if msg.get("tool_call_id"):
                tool_results.add(msg["tool_call_id"])
            
            # 检查 content 中的 tool_result（Anthropic数组格式）
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        if item.get("tool_use_id"):
                            tool_results.add(item["tool_use_id"])
            
            # [NEW 2026-01-24] 检查 parts 中的 functionResponse（Gemini格式）
            parts = msg.get("parts", [])
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and "functionResponse" in part:
                        fr = part["functionResponse"]
                        fr_id = fr.get("id") or fr.get("callId")
                        if fr_id:
                            tool_results.add(fr_id)

        return tool_results

    def _cache_tool_results_from_messages(
        self,
        state: ConversationState,
        messages: List[Dict]
    ) -> int:
        """
        [NEW 2026-02-07] 从消息列表中提取并缓存所有工具结果

        将每个 tool_result 按其 tool_use_id 缓存到 state.tool_results_cache 中，
        用于后续在工具结果丢失时恢复，防止：
        - tool_use_result_mismatch 错误
        - 系统提示词重复注入（每次工具调用都触发自我介绍）

        Args:
            state: 会话状态
            messages: 要提取工具结果的消息列表

        Returns:
            新缓存的工具结果数量
        """
        cached_count = 0

        for msg in messages:
            # 情况1: OpenAI格式 - role=tool, tool_call_id 字段
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                tool_call_id = msg["tool_call_id"]
                if tool_call_id not in state.tool_results_cache:
                    state.tool_results_cache[tool_call_id] = msg.copy()
                    cached_count += 1
                    log.debug(f"[TOOL_CACHE] Cached tool result: {tool_call_id[:20]}...")

            # 情况2: Anthropic格式 - role=user, content 数组中有 tool_result
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        tool_use_id = item.get("tool_use_id")
                        if tool_use_id and tool_use_id not in state.tool_results_cache:
                            # 缓存整个包含 tool_result 的消息
                            state.tool_results_cache[tool_use_id] = msg.copy()
                            cached_count += 1
                            log.debug(f"[TOOL_CACHE] Cached Anthropic tool result: {tool_use_id[:20]}...")

            # 情况3: Gemini格式 - parts 中有 functionResponse
            parts = msg.get("parts", [])
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and "functionResponse" in part:
                        fr = part["functionResponse"]
                        fr_id = fr.get("id") or fr.get("callId")
                        if fr_id and fr_id not in state.tool_results_cache:
                            state.tool_results_cache[fr_id] = msg.copy()
                            cached_count += 1
                            log.debug(f"[TOOL_CACHE] Cached Gemini function response: {fr_id[:20]}...")

        if cached_count > 0:
            log.info(f"[TOOL_CACHE] Cached {cached_count} new tool results, total cache size: {len(state.tool_results_cache)}")

        return cached_count

    def _recover_tool_results_from_cache(
        self,
        state: ConversationState,
        orphan_tool_calls: Set[str],
        messages: List[Dict]
    ) -> List[Dict]:
        """
        [NEW 2026-02-07] 从缓存中恢复丢失的工具结果

        当检测到孤儿 tool_use（有 tool_call 但没有对应 result）时，
        尝试从 tool_results_cache 中恢复这些工具结果。

        Args:
            state: 会话状态
            orphan_tool_calls: 孤儿 tool_call_id 集合
            messages: 原始消息列表

        Returns:
            包含恢复的工具结果的完整消息列表
        """
        if not orphan_tool_calls:
            return messages

        recovered_count = 0
        result_messages = []

        for msg in messages:
            result_messages.append(msg)

            # 检查这条消息是否包含孤儿 tool_use
            msg_tool_calls = set()
            
            # OpenAI格式 tool_calls
            for call in self._get_tool_calls_as_list(msg):
                if call.get("id"):
                    msg_tool_calls.add(call["id"])

            # Anthropic格式 content 中的 tool_use
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("id"):
                        msg_tool_calls.add(item["id"])

            # 对于这条消息中的每个孤儿 tool_call，尝试从缓存恢复
            orphans_in_msg = msg_tool_calls & orphan_tool_calls
            for tool_call_id in orphans_in_msg:
                if tool_call_id in state.tool_results_cache:
                    cached_result = state.tool_results_cache[tool_call_id]
                    result_messages.append(cached_result.copy())
                    recovered_count += 1
                    log.warning(
                        f"[TOOL_CACHE] Recovered tool result from cache: {tool_call_id[:20]}..."
                    )

        if recovered_count > 0:
            log.info(
                f"[TOOL_CACHE] Successfully recovered {recovered_count}/{len(orphan_tool_calls)} "
                f"tool results from cache"
            )

        return result_messages

    def _build_tool_chain_graph(self, history: List[Dict]) -> Dict[int, Optional[int]]:
        """
        构建工具链依赖图

        遍历历史消息，为每个 tool_use 找到对应的 tool_result，
        建立索引映射关系。

        Args:
            history: 历史消息列表

        Returns:
            Dict[tool_use_index, tool_result_index]
            - key: 包含 tool_use 的消息索引
            - value: 对应 tool_result 的消息索引，如果找不到则为 None
        """
        tool_chain_graph: Dict[int, Optional[int]] = {}

        for i, msg in enumerate(history):
            # 提取该消息中的所有 tool_call_id
            tool_call_ids = []

            # 检查 tool_calls 字段（OpenAI格式）
            for call in self._get_tool_calls_as_list(msg):
                if call.get("id"):
                    tool_call_ids.append(call["id"])

            # 检查 content 中的 tool_use（Anthropic数组格式）
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        if item.get("id"):
                            tool_call_ids.append(item["id"])

            # 检查 parts 中的 functionCall（Gemini格式）
            parts = msg.get("parts", [])
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and "functionCall" in part:
                        fc = part["functionCall"]
                        fc_id = fc.get("id") or fc.get("callId")
                        if fc_id:
                            tool_call_ids.append(fc_id)

            # 如果该消息包含 tool_use，查找对应的 tool_result
            if tool_call_ids:
                # 对于每个 tool_call_id，查找对应的 tool_result
                for tool_call_id in tool_call_ids:
                    result_idx = self._find_tool_result_by_id(history, tool_call_id, i + 1)
                    if result_idx is not None:
                        # 记录 tool_use 消息索引 -> tool_result 消息索引
                        tool_chain_graph[i] = result_idx
                        log.debug(
                            f"[STATE_MANAGER] Tool chain mapped: "
                            f"tool_use[{i}] -> tool_result[{result_idx}] "
                            f"(id={tool_call_id[:20]}...)"
                        )
                    else:
                        # 找不到对应的 tool_result
                        tool_chain_graph[i] = None
                        log.warning(
                            f"[STATE_MANAGER] Orphan tool_use at index {i}: "
                            f"no matching tool_result found (id={tool_call_id[:20]}...)"
                        )

        log.info(
            f"[STATE_MANAGER] Tool chain graph built: "
            f"{len(tool_chain_graph)} tool_use messages, "
            f"{sum(1 for v in tool_chain_graph.values() if v is not None)} with matching results"
        )

        return tool_chain_graph

    def _find_tool_result_by_id(
        self,
        history: List[Dict],
        tool_call_id: str,
        start_from: int
    ) -> Optional[int]:
        """
        从指定位置开始查找匹配的 tool_result

        Args:
            history: 历史消息列表
            tool_call_id: 要查找的 tool_call_id
            start_from: 开始搜索的索引位置

        Returns:
            匹配的 tool_result 消息索引，如果找不到则返回 None
        """
        for i in range(start_from, len(history)):
            msg = history[i]

            # 检查 Anthropic 格式的 tool_call_id
            if msg.get("tool_call_id") == tool_call_id:
                return i

            # 检查 Anthropic 数组格式的 tool_result
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        if item.get("tool_use_id") == tool_call_id:
                            return i

            # 检查 Gemini 格式的 functionResponse
            parts = msg.get("parts", [])
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and "functionResponse" in part:
                        fr = part["functionResponse"]
                        fr_id = fr.get("id") or fr.get("callId")
                        if fr_id == tool_call_id:
                            return i

        return None

    def _find_valid_thinking_blocks(self, history: List[Dict]) -> Set[int]:
        """
        识别包含有效 signature 的 thinking blocks

        有效性判断:
        1. block.type == "thinking"
        2. signature 存在且长度 >= 50

        Args:
            history: 历史消息列表

        Returns:
            包含有效 thinking blocks 的消息索引集合
        """
        valid_thinking_indices: Set[int] = set()

        for i, msg in enumerate(history):
            content = msg.get("content", [])

            # content 必须是列表才可能包含 thinking blocks
            if not isinstance(content, list):
                continue

            for item in content:
                if not isinstance(item, dict):
                    continue

                # 检查是否是 thinking block
                if item.get("type") == "thinking":
                    # 检查 signature 是否有效
                    signature = item.get("signature", "")
                    if signature and len(signature) >= 50:
                        valid_thinking_indices.add(i)
                        log.debug(
                            f"[STATE_MANAGER] Valid thinking block found at index {i}: "
                            f"signature_len={len(signature)}"
                        )
                        break  # 一条消息只需要标记一次

        log.info(
            f"[STATE_MANAGER] Found {len(valid_thinking_indices)} messages "
            f"with valid thinking blocks"
        )

        return valid_thinking_indices

    def _merge_with_tool_chain_recovery(
        self,
        client_messages: List[Dict],
        authoritative: List[Dict],
        orphan_tool_calls: set
    ) -> List[Dict]:
        """
        智能工具链恢复合并算法
        
        策略：
        1. 找到客户端中包含孤儿tool_use的assistant消息
        2. 从权威历史中找到对应的tool_result消息
        3. 在合并结果中插入缺失的tool_result
        4. 保持消息顺序正确
        
        Args:
            client_messages: 客户端消息
            authoritative: 权威历史
            orphan_tool_calls: 孤儿tool_call_id集合
            
        Returns:
            合并后的消息列表
        """
        merged = []
        
        # 从权威历史构建 tool_call_id -> tool_result 映射
        auth_tool_results = {}
        for msg in authoritative:
            # 检查 Anthropic 格式的 tool_call_id
            tool_call_id = msg.get("tool_call_id")
            if tool_call_id:
                auth_tool_results[tool_call_id] = msg
            
            # 检查 Anthropic 数组格式的 tool_result
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        tool_use_id = item.get("tool_use_id")
                        if tool_use_id:
                            auth_tool_results[tool_use_id] = msg
            
            # [NEW 2026-01-24] 检查 Gemini 格式的 functionResponse
            parts = msg.get("parts", [])
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and "functionResponse" in part:
                        fr = part["functionResponse"]
                        fr_id = fr.get("id") or fr.get("callId")
                        if fr_id:
                            auth_tool_results[fr_id] = msg
        
        recovered_count = 0
        
        for i, client_msg in enumerate(client_messages):
            # 添加客户端消息
            merged.append(self._compress_tool_result(client_msg))
            
            # 检查是否是包含孤儿tool_use的assistant消息
            msg_tool_calls = set()
            
            # 检查 tool_calls 字段
            for call in self._get_tool_calls_as_list(client_msg):
                if call.get("id"):
                    msg_tool_calls.add(call["id"])
            
            # 检查 content 中的 tool_use
            content = client_msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        if item.get("id"):
                            msg_tool_calls.add(item["id"])
            
            # 找出这条消息中的孤儿tool_use
            orphans_in_msg = msg_tool_calls & orphan_tool_calls
            
            if orphans_in_msg:
                # 从权威历史恢复对应的tool_result
                for tool_call_id in orphans_in_msg:
                    if tool_call_id in auth_tool_results:
                        result_msg = auth_tool_results[tool_call_id]
                        merged.append(self._compress_tool_result(result_msg))
                        recovered_count += 1

                        log.info(
                            f"[STATE_MANAGER] Recovered tool_result for orphan tool_use: "
                            f"tool_call_id={tool_call_id[:20]}..."
                        )

        # [FIX 2026-02-04] 降级策略：检查是否有未恢复的孤儿 tool_use
        # 如果权威历史中找不到对应的 tool_result，直接过滤孤儿 tool_use
        # 避免将不完整的工具链发送给 API，导致 400 tool_use_result_mismatch 错误
        recovered_tool_ids = set()
        for msg in merged:
            # 检查 OpenAI 格式的 tool_call_id
            tool_call_id = msg.get("tool_call_id")
            if tool_call_id:
                recovered_tool_ids.add(tool_call_id)

            # 检查 Anthropic 数组格式的 tool_result
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        tool_use_id = item.get("tool_use_id")
                        if tool_use_id:
                            recovered_tool_ids.add(tool_use_id)

            # 检查 Gemini 格式的 functionResponse
            parts = msg.get("parts", [])
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and "functionResponse" in part:
                        fr = part["functionResponse"]
                        fr_id = fr.get("id") or fr.get("callId")
                        if fr_id:
                            recovered_tool_ids.add(fr_id)

        unrecovered_orphans = orphan_tool_calls - recovered_tool_ids

        if unrecovered_orphans:
            # [FIX 2026-02-04] 增强日志：记录详细的孤儿工具信息，便于调试
            log.warning(
                f"[STATE_MANAGER] {len(unrecovered_orphans)} orphan tool_uses could not be recovered from "
                f"authoritative history. IDs: {list(unrecovered_orphans)[:5]}..."
            )

            # [FIX 2026-02-04] 尝试从检查点恢复孤儿工具调用
            # 如果检查点中有工具调用信息，尝试自动重试
            retry_success = self._attempt_orphan_tool_retry(unrecovered_orphans, merged)

            if retry_success:
                log.info(
                    f"[STATE_MANAGER] Successfully recovered {len(unrecovered_orphans)} orphan tool_uses via retry mechanism"
                )
            else:
                # 重试失败，使用降级策略：过滤孤儿 tool_use
                log.info(
                    f"[STATE_MANAGER] Orphan tool retry failed or skipped, applying graceful degradation: "
                    f"filtering {len(unrecovered_orphans)} unrecoverable orphan tool_uses to avoid 400 error"
                )
                # 过滤未恢复的孤儿 tool_use
                merged = self._filter_orphan_tool_uses(merged, unrecovered_orphans)

        log.info(
            f"[STATE_MANAGER] Tool chain recovery: "
            f"recovered {recovered_count} tool_results for {len(orphan_tool_calls)} orphan tool_uses, "
            f"filtered {len(unrecovered_orphans)} unrecoverable orphans"
        )

        return merged

    def _attempt_orphan_tool_retry(self, orphan_ids: set, merged_messages: List[Dict]) -> bool:
        """
        [FIX 2026-02-04] 尝试从检查点恢复孤儿工具调用

        当权威历史中找不到 tool_result 时，尝试以下策略：
        1. 检查检查点中是否有工具调用信息
        2. 如果有，生成占位符 tool_result（标记为中断）
        3. 这样可以保持工具链完整性，避免 400 错误

        注意：这是一个降级策略，不会真正重新执行工具调用
        因为我们无法在网关层执行工具，只能生成占位符

        Args:
            orphan_ids: 孤儿 tool_call_id 集合
            merged_messages: 合并后的消息列表（会被原地修改）

        Returns:
            True 如果成功生成了占位符，False 如果跳过
        """
        if not orphan_ids:
            return True

        try:
            # 记录详细的孤儿工具信息
            log.info(
                f"[STATE_MANAGER] Attempting orphan tool recovery: "
                f"orphan_count={len(orphan_ids)}, "
                f"ids={list(orphan_ids)[:3]}..."
            )

            # 生成占位符 tool_result
            placeholder_count = 0
            for orphan_id in orphan_ids:
                # 查找包含此 tool_use 的 assistant 消息
                for i, msg in enumerate(merged_messages):
                    if msg.get("role") != "assistant":
                        continue

                    # 检查 tool_calls 字段
                    tool_calls = self._get_tool_calls_as_list(msg)
                    has_orphan = False
                    tool_name = "unknown"

                    for call in tool_calls:
                        if isinstance(call, dict) and call.get("id") == orphan_id:
                            has_orphan = True
                            # 尝试获取工具名称
                            func = call.get("function", {})
                            tool_name = func.get("name", "unknown")
                            break

                    # 检查 content 中的 tool_use
                    if not has_orphan:
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "tool_use":
                                    if item.get("id") == orphan_id:
                                        has_orphan = True
                                        tool_name = item.get("name", "unknown")
                                        break

                    if has_orphan:
                        # 在此 assistant 消息之后插入占位符 tool_result
                        placeholder_result = {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": orphan_id,
                                    "content": f"[INTERRUPTED] Tool execution was interrupted. "
                                               f"Tool '{tool_name}' did not complete. "
                                               f"Please retry the operation if needed.",
                                    "is_error": True
                                }
                            ]
                        }

                        # 找到正确的插入位置（在 assistant 消息之后）
                        insert_pos = i + 1
                        # 确保不会插入到其他 tool_result 之前
                        while insert_pos < len(merged_messages):
                            next_msg = merged_messages[insert_pos]
                            # 如果下一条是 user 消息且包含 tool_result，继续往后找
                            if next_msg.get("role") == "user":
                                next_content = next_msg.get("content", [])
                                if isinstance(next_content, list):
                                    has_tool_result = any(
                                        isinstance(item, dict) and item.get("type") == "tool_result"
                                        for item in next_content
                                    )
                                    if has_tool_result:
                                        insert_pos += 1
                                        continue
                            break

                        merged_messages.insert(insert_pos, placeholder_result)
                        placeholder_count += 1

                        log.debug(
                            f"[STATE_MANAGER] Generated placeholder tool_result: "
                            f"tool_id={orphan_id[:20]}..., tool_name={tool_name}, "
                            f"insert_pos={insert_pos}"
                        )
                        break

            if placeholder_count > 0:
                log.info(
                    f"[STATE_MANAGER] Orphan tool recovery completed: "
                    f"generated {placeholder_count} placeholder tool_results"
                )
                return True
            else:
                log.warning(
                    f"[STATE_MANAGER] Orphan tool recovery failed: "
                    f"could not find matching assistant messages for {len(orphan_ids)} orphans"
                )
                return False

        except Exception as e:
            # [FIX 2026-02-04] 捕获所有异常，避免弹出不友好错误
            log.warning(
                f"[STATE_MANAGER] Orphan tool retry failed with exception (graceful degradation): {e}"
            )
            return False

    def _filter_orphan_tool_uses(self, messages: List[Dict], orphan_ids: set) -> List[Dict]:
        """
        [FIX 2026-02-04] 过滤孤儿 tool_use

        从消息列表中移除指定的孤儿 tool_use，保持消息结构完整

        Args:
            messages: 消息列表
            orphan_ids: 需要过滤的孤儿 tool_call_id 集合

        Returns:
            过滤后的消息列表
        """
        if not orphan_ids:
            return messages

        filtered = []
        for msg in messages:
            # 检查 OpenAI 格式的 tool_calls
            tool_calls = self._get_tool_calls_as_list(msg)
            if tool_calls:
                new_tool_calls = []
                for call in tool_calls:
                    if isinstance(call, dict):
                        call_id = call.get("id")
                        if call_id and call_id in orphan_ids:
                            log.info(f"[STATE_MANAGER] Filtering orphan tool_use: {call_id[:20]}...")
                            continue
                    new_tool_calls.append(call)

                if new_tool_calls:
                    new_msg = msg.copy()
                    new_msg["tool_calls"] = new_tool_calls
                    filtered.append(new_msg)
                elif msg.get("content"):
                    # 如果有内容但工具调用被完全过滤，保留消息但移除 tool_calls
                    new_msg = msg.copy()
                    new_msg.pop("tool_calls", None)
                    filtered.append(new_msg)
                # 如果消息只有工具调用且全被过滤，则跳过该消息
                continue

            # 检查 Anthropic 数组格式的 tool_use
            content = msg.get("content", [])
            if isinstance(content, list):
                new_content = []
                has_orphan = False
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        item_id = item.get("id")
                        if item_id and item_id in orphan_ids:
                            log.info(f"[STATE_MANAGER] Filtering orphan tool_use (Anthropic format): {item_id[:20]}...")
                            has_orphan = True
                            continue
                    new_content.append(item)

                if has_orphan:
                    if new_content:
                        new_msg = msg.copy()
                        new_msg["content"] = new_content
                        filtered.append(new_msg)
                    # 如果内容被完全过滤，跳过该消息
                    continue

            # 检查 Gemini 格式的 functionCall
            parts = msg.get("parts", [])
            if isinstance(parts, list):
                new_parts = []
                has_orphan = False
                for part in parts:
                    if isinstance(part, dict) and "functionCall" in part:
                        fc = part["functionCall"]
                        fc_id = fc.get("id")
                        if fc_id and fc_id in orphan_ids:
                            log.info(f"[STATE_MANAGER] Filtering orphan functionCall: {fc_id[:20]}...")
                            has_orphan = True
                            continue
                    new_parts.append(part)

                if has_orphan:
                    if new_parts:
                        new_msg = msg.copy()
                        new_msg["parts"] = new_parts
                        filtered.append(new_msg)
                    else:
                        # 添加占位符
                        filtered.append({
                            "role": msg.get("role", "model"),
                            "parts": [{"text": "..."}]
                        })
                    continue

            # 没有需要过滤的内容，直接添加
            filtered.append(msg)

        return filtered

    def _compress_authoritative_history(self, history: List[Dict]) -> List[Dict]:
        """
        智能压缩权威历史（工具链友好）

        [FIX 2026-02-07] ⚠️ 重大修改：永不删除消息！

        策略（极度保守）：
        1. 永不删除任何消息 - 只压缩工具结果内容
        2. 保护工具链完整性是最高优先级
        3. 即使超过 MAX_HISTORY_MESSAGES 也只压缩内容，不删除消息
        4. 只有通过 trigger_emergency_compress() 显式调用时才考虑删除

        工具链完整性：
        - 每个 tool_use 必须有对应的 tool_result
        - 删除任何消息都可能破坏工具链
        - 400 tool_use_result_mismatch 错误的根本原因就是工具链断裂

        Args:
            history: 原始历史消息列表

        Returns:
            压缩后的历史消息列表（只压缩内容，保留所有消息）
        """
        # [FIX 2026-02-07] ⚠️ 关键修复！
        # 永不删除消息！只压缩工具结果的内容
        # 这是保护工具链完整性的唯一可靠方式

        if len(history) > MAX_HISTORY_MESSAGES:
            log.warning(
                f"[STATE_MANAGER] ⚠️ History exceeds soft limit: {len(history)} > {MAX_HISTORY_MESSAGES}, "
                f"but NOT deleting messages to preserve tool chain integrity! "
                f"Only compressing tool result content."
            )
        else:
            log.debug(
                f"[STATE_MANAGER] History within limit ({len(history)} <= {MAX_HISTORY_MESSAGES}), "
                f"compressing tool results content only"
            )

        # 只压缩内容，永不删除消息
        compressed = [self._compress_tool_result(msg) for msg in history]

        log.info(
            f"[STATE_MANAGER] History content compressed: "
            f"{len(history)} messages (all preserved, only content truncated)"
        )

        return compressed

    def trigger_emergency_compress(self, scid: str, error_msg: str = "") -> bool:
        """
        紧急压缩 - 仅在上游明确返回请求体过大错误时调用

        [FIX 2026-02-07] 新增方法

        触发条件（必须同时满足）：
        1. 上游返回 413 Request Entity Too Large
        2. 或上游返回 400 且错误信息包含 "too large" / "token limit" / "context length"

        压缩策略（仍然保守）：
        1. 首先尝试只压缩工具结果内容（更激进的截断）
        2. 如果仍然太大，才考虑删除最早的非工具链消息
        3. 永远不删除工具链的任何部分（tool_use + tool_result 配对）

        Args:
            scid: 会话 ID
            error_msg: 上游返回的错误信息

        Returns:
            是否成功压缩
        """
        log.warning(
            f"[STATE_MANAGER] 🚨 Emergency compress triggered for SCID {scid[:16]}... "
            f"Error: {error_msg[:200] if error_msg else 'Unknown'}"
        )

        with self._lock:
            state = self._memory_cache.get(scid)
            if not state:
                log.error(f"[STATE_MANAGER] SCID {scid[:16]}... not found for emergency compress")
                return False

            history = state.authoritative_history
            original_count = len(history)

            if original_count == 0:
                log.warning(f"[STATE_MANAGER] SCID {scid[:16]}... has no history to compress")
                return False

            # 第一步：更激进地压缩工具结果内容（截断到 1000 字符）
            compressed = []
            for msg in history:
                compressed_msg = self._emergency_compress_tool_result(msg)
                compressed.append(compressed_msg)

            # 更新状态
            state.authoritative_history = compressed
            self._memory_cache[scid] = state

            log.info(
                f"[STATE_MANAGER] ✅ Emergency compress completed for SCID {scid[:16]}...: "
                f"{original_count} messages (content aggressively truncated, all messages preserved)"
            )

            return True

    def _emergency_compress_tool_result(self, msg: Dict) -> Dict:
        """
        紧急压缩单条消息的工具结果（更激进的截断）

        [FIX 2026-02-07] 新增方法

        与 _compress_tool_result 的区别：
        - 截断到 1000 字符（而不是 5000）
        - 更激进地处理大型内容

        Args:
            msg: 原始消息

        Returns:
            压缩后的消息
        """
        EMERGENCY_MAX_CHARS = 1000  # 紧急模式下更激进的截断

        if msg.get("role") != "user":
            return msg

        # 处理 Gemini 格式
        if "parts" in msg:
            parts = msg.get("parts", [])
            if not isinstance(parts, list):
                return msg

            new_parts = []
            for part in parts:
                if isinstance(part, dict) and "functionResponse" in part:
                    response = part.get("functionResponse", {})
                    result = response.get("response", {})

                    if isinstance(result, dict):
                        result_str = str(result)
                        if len(result_str) > EMERGENCY_MAX_CHARS:
                            new_parts.append({
                                "functionResponse": {
                                    "name": response.get("name", "unknown"),
                                    "response": {
                                        "_emergency_truncated": True,
                                        "_original_length": len(result_str),
                                        "content": result_str[:EMERGENCY_MAX_CHARS] + "...[EMERGENCY TRUNCATED]"
                                    }
                                }
                            })
                            continue
                    elif isinstance(result, str) and len(result) > EMERGENCY_MAX_CHARS:
                        new_parts.append({
                            "functionResponse": {
                                "name": response.get("name", "unknown"),
                                "response": result[:EMERGENCY_MAX_CHARS] + "...[EMERGENCY TRUNCATED]"
                            }
                        })
                        continue

                new_parts.append(part)

            return {**msg, "parts": new_parts}

        # 处理 OpenAI 格式
        content = msg.get("content")
        if isinstance(content, str) and len(content) > EMERGENCY_MAX_CHARS:
            return {
                **msg,
                "content": content[:EMERGENCY_MAX_CHARS] + "...[EMERGENCY TRUNCATED]",
                "_emergency_truncated": True,
                "_original_length": len(content)
            }

        return msg

    def get_stats(self) -> Dict[str, Any]:
        """
        获取状态管理器统计信息

        Returns:
            统计信息字典
        """
        with self._lock:
            total_states = len(self._memory_cache)
            total_messages = sum(
                len(state.authoritative_history)
                for state in self._memory_cache.values()
            )

            return {
                "total_states": total_states,
                "total_messages": total_messages,
                "average_messages_per_state": total_messages / total_states if total_states > 0 else 0,
            }
    
    def cleanup_old_scids(self, max_age_hours: int = 24) -> Dict[str, int]:
        """
        清理超过指定时间的 SCID
        
        目的：
        - 防止数据库无限膨胀
        - 避免基于第一条消息的 SCID 误匹配
        - 释放内存和磁盘空间
        
        策略：
        - 清理超过 max_age_hours 的 SCID
        - 同时清理内存缓存和数据库
        
        Args:
            max_age_hours: 最大保留时间（小时），默认 24 小时
        
        Returns:
            清理统计：{
                "memory_cleaned": 内存中清理的 SCID 数量,
                "db_cleaned": 数据库中清理的 SCID 数量
            }
        """
        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
        
        memory_cleaned = 0
        db_cleaned = 0
        
        # 1. 清理内存缓存
        with self._lock:
            to_remove = []
            for scid, state in self._memory_cache.items():
                if state.updated_at < cutoff_time:  # [FIX C3] 修复：使用正确的属性名 updated_at
                    to_remove.append(scid)

            for scid in to_remove:
                del self._memory_cache[scid]
                memory_cleaned += 1
                log.info(f"[STATE_MANAGER] Cleaned up old SCID from memory: {scid[:30]}...")

        # 2. 清理 SQLite 数据库（如果有）
        if self._db:  # [FIX C3] 修复：使用正确的属性名 self._db
            try:
                # 计算 cutoff 时间戳
                cutoff_timestamp = cutoff_time.timestamp()                # 执行删除 - 使用 SignatureDatabase 的公共接口
                # 注意：SignatureDatabase 可能没有 _get_connection 方法，需要检查接口
                if hasattr(self._db, 'cleanup_expired_states'):
                    db_cleaned = self._db.cleanup_expired_states(cutoff_timestamp)
                elif hasattr(self._db, '_get_connection'):
                    with self._db._get_connection() as conn:
                        cursor = conn.execute(
                            """
                            DELETE FROM conversation_state
                            WHERE last_updated < ?
                            """,
                            (cutoff_timestamp,)
                        )
                        db_cleaned = cursor.rowcount
                        conn.commit()
                else:
                    log.warning("[STATE_MANAGER] Database doesn't support cleanup operation")
                
                log.info(
                    f"[STATE_MANAGER] Cleaned up {db_cleaned} old sessions from database "
                    f"(older than {max_age_hours} hours)"
                )
            except Exception as e:
                log.error(f"[STATE_MANAGER] Failed to cleanup database: {e}")
        
        # 返回统计
        stats = {
            "memory_cleaned": memory_cleaned,
            "db_cleaned": db_cleaned,
            "total_cleaned": memory_cleaned + db_cleaned
        }
        
        log.info(
            f"[STATE_MANAGER] Cleanup completed: "
            f"memory={memory_cleaned}, db={db_cleaned}, total={stats['total_cleaned']}"
        )
        
        return stats
    
    def cleanup_by_scid_prefix(self, prefix: str = "scid_first_") -> Dict[str, int]:
        """
        根据 SCID 前缀清理数据
        
        用途：
        - 清理特定类型的 SCID（如基于第一条消息的）
        - 用于迁移或重置
        
        Args:
            prefix: SCID 前缀
        
        Returns:
            清理统计
        """
        memory_cleaned = 0
        db_cleaned = 0
        
        # 1. 清理内存缓存
        with self._lock:
            to_remove = []
            for scid in self._memory_cache.keys():
                if scid.startswith(prefix):
                    to_remove.append(scid)
            
            for scid in to_remove:
                del self._memory_cache[scid]
                memory_cleaned += 1
        
        # 2. 清理数据库
        if self._db:  # [FIX C4] 修复：使用正确的属性名 self._db
            try:
                if hasattr(self._db, '_get_connection'):
                    with self._db._get_connection() as conn:
                        cursor = conn.execute(
                            """
                            DELETE FROM conversation_state
                            WHERE scid LIKE ?
                            """,
                            (f"{prefix}%",)
                        )
                        db_cleaned = cursor.rowcount
                        conn.commit()
                else:
                    log.warning("[STATE_MANAGER] Database doesn't support cleanup by prefix")
            except Exception as e:
                log.error(f"[STATE_MANAGER] Failed to cleanup by prefix: {e}")
        
        log.info(
            f"[STATE_MANAGER] Cleaned up SCIDs with prefix '{prefix}': "
            f"memory={memory_cleaned}, db={db_cleaned}"
        )
        
        return {
            "memory_cleaned": memory_cleaned,
            "db_cleaned": db_cleaned,
            "total_cleaned": memory_cleaned + db_cleaned
        }

    # ==================== [NEW 2026-02-02] 检查点机制 ====================
    # 用于解决流中断时签名丢失的问题
    # ===================================================================

    # [FIX W4] 检查点最大容量限制，防止内存泄漏
    MAX_CHECKPOINTS = 1000

    def save_checkpoint(self, scid: str, checkpoint: Dict) -> None:
        """
        保存中间状态检查点

        用于在流中断时保存已收到的内容，供下次请求恢复

        Args:
            scid: 会话 ID
            checkpoint: 检查点数据，包含：
                - thinking_content: 已收到的 thinking 内容
                - partial_response: 已收到的响应内容
                - timestamp: 保存时间戳
                - is_complete: 是否已完成（False 表示中断）
        """
        if not scid:
            return

        with self._checkpoint_lock:
            # [FIX W4] 如果超过容量限制，清理旧检查点
            if len(self._checkpoints) >= self.MAX_CHECKPOINTS:
                self._cleanup_oldest_checkpoints_unlocked(count=100)

            # [FIX 2026-02-04] 添加 last_accessed 字段（LRU策略）
            import time
            checkpoint["last_accessed"] = checkpoint.get("timestamp", time.time())

            self._checkpoints[scid] = checkpoint

            # [FIX 2026-02-04] 同时持久化到SQLite
            if self._db and hasattr(self._db, 'save_checkpoint'):
                try:
                    self._db.save_checkpoint(scid, checkpoint)
                except Exception as e:
                    log.warning(f"[STATE_MANAGER] Failed to persist checkpoint to SQLite: {e}")

            log.debug(
                f"[STATE_MANAGER] Checkpoint saved: scid={scid[:20]}..., "
                f"is_complete={checkpoint.get('is_complete', False)}"
            )

    def _cleanup_oldest_checkpoints_unlocked(self, count: int = 100) -> int:
        """
        清理最久未访问的检查点（LRU策略）

        [FIX 2026-02-04] 改为按 last_accessed 排序，而非 timestamp
        这样可以保留活跃使用的检查点，清理不活跃的

        Args:
            count: 要清理的数量

        Returns:
            实际清理的数量
        """
        if not self._checkpoints:
            return 0

        # [FIX 2026-02-04] 按 last_accessed 排序（LRU），而非 timestamp
        # 向后兼容：如果没有 last_accessed，使用 timestamp 作为 fallback
        sorted_items = sorted(
            self._checkpoints.items(),
            key=lambda x: x[1].get("last_accessed", x[1].get("timestamp", 0))
        )

        to_remove = [scid for scid, _ in sorted_items[:count]]
        for scid in to_remove:
            del self._checkpoints[scid]

        if to_remove:
            log.info(f"[STATE_MANAGER] Cleaned up {len(to_remove)} least recently used checkpoints (LRU)")

        return len(to_remove)

    def get_checkpoint(self, scid: str) -> Optional[Dict]:
        """
        获取中间状态检查点

        Args:
            scid: 会话 ID

        Returns:
            检查点数据字典，如果不存在则返回 None
        """
        if not scid:
            return None

        with self._checkpoint_lock:
            # 先从内存获取
            checkpoint = self._checkpoints.get(scid)
            if checkpoint:
                # [FIX 2026-02-04] 更新访问时间（LRU策略）
                import time
                checkpoint["last_accessed"] = time.time()

                log.debug(
                    f"[STATE_MANAGER] Checkpoint retrieved: scid={scid[:20]}..., "
                    f"is_complete={checkpoint.get('is_complete', False)}"
                )
                return checkpoint

            # [FIX 2026-02-04] 内存没有则从SQLite恢复
            if self._db and hasattr(self._db, 'get_checkpoint'):
                try:
                    checkpoint = self._db.get_checkpoint(scid)
                    if checkpoint:
                        # 回填到内存缓存
                        self._checkpoints[scid] = checkpoint
                        log.info(f"[STATE_MANAGER] Checkpoint recovered from SQLite: scid={scid[:20]}...")
                        return checkpoint
                except Exception as e:
                    log.warning(f"[STATE_MANAGER] Failed to get checkpoint from SQLite: {e}")

            return None

    def clear_checkpoint(self, scid: str) -> None:
        """
        清除检查点（流正常结束时调用）

        Args:
            scid: 会话 ID
        """
        if not scid:
            return

        with self._checkpoint_lock:
            if scid in self._checkpoints:
                del self._checkpoints[scid]

            # [FIX 2026-02-04] 同时从SQLite删除
            if self._db and hasattr(self._db, 'delete_checkpoint'):
                try:
                    self._db.delete_checkpoint(scid)
                except Exception as e:
                    log.warning(f"[STATE_MANAGER] Failed to delete checkpoint from SQLite: {e}")

            log.debug(f"[STATE_MANAGER] Checkpoint cleared: scid={scid[:20]}...")

    def has_incomplete_session(self, scid: str) -> bool:
        """
        检查是否有未完成的会话

        用于在请求前检测是否需要恢复中断的会话

        Args:
            scid: 会话 ID

        Returns:
            如果有未完成的检查点则返回 True
        """
        if not scid:
            return False

        with self._checkpoint_lock:
            # 先检查内存
            checkpoint = self._checkpoints.get(scid)
            if checkpoint is not None and not checkpoint.get("is_complete", True):
                log.info(
                    f"[STATE_MANAGER] Incomplete session detected: scid={scid[:20]}..., "
                    f"timestamp={checkpoint.get('timestamp')}"
                )
                return True

            # [FIX 2026-02-04] 内存没有则检查SQLite
            if self._db and hasattr(self._db, 'get_checkpoint'):
                try:
                    checkpoint = self._db.get_checkpoint(scid)
                    if checkpoint and not checkpoint.get("is_complete", True):
                        # 回填到内存
                        self._checkpoints[scid] = checkpoint
                        log.info(f"[STATE_MANAGER] Incomplete session recovered from SQLite: scid={scid[:20]}...")
                        return True
                except Exception as e:
                    log.warning(f"[STATE_MANAGER] Failed to check checkpoint in SQLite: {e}")

            return False

    def cleanup_old_checkpoints(self, max_age_seconds: int = 3600) -> int:
        """
        清理过期的检查点

        Args:
            max_age_seconds: 最大保留时间（秒），默认 1 小时

        Returns:
            清理的检查点数量
        """
        import time
        now = time.time()
        cleaned = 0

        with self._checkpoint_lock:
            expired_scids = [
                scid for scid, checkpoint in self._checkpoints.items()
                if now - checkpoint.get("timestamp", 0) > max_age_seconds
            ]

            for scid in expired_scids:
                del self._checkpoints[scid]
                cleaned += 1

            if cleaned > 0:
                log.info(
                    f"[STATE_MANAGER] Cleaned up {cleaned} expired checkpoints "
                    f"(older than {max_age_seconds}s)"
                )

        return cleaned