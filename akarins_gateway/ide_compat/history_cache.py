"""
历史缓存管理器 (History Cache Manager)

核心模块，负责完整历史缓存和智能消息选择。

职责：
1. 缓存完整历史（不删除）
2. 智能选择消息发送给后端
3. 支持多种缓存后端（LRU / Redis）
4. 支持多种选择策略（Smart / Recent / Token-based）

设计原则：
- 单一职责原则(SRP): 只负责历史缓存和选择
- 开放封闭原则(OCP): 易于扩展新的后端和策略
- 依赖倒置原则(DIP): 依赖抽象接口而非具体实现
"""

from typing import List, Dict, Optional
import logging
import time
import threading  # [FIX 2026-02-01] 添加线程锁支持

from .cache_backends.base import CacheBackend
from .cache_backends.lru_backend import LRUCacheBackend
from .selection_strategies.base import SelectionStrategy
from .selection_strategies.smart_selector import SmartSelectionStrategy

log = logging.getLogger("gcli2api.history_cache")


class HistoryCacheManager:
    """
    历史缓存管理器（主入口）
    
    使用示例：
        ```python
        # 初始化（LRU后端 + Smart选择策略）
        cache = HistoryCacheManager(
            backend="lru",
            max_cache_size=1000,
            strategy="smart"
        )
        
        # 存储完整历史
        scid = "conversation_123"
        history = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            # ... 47 more messages
        ]
        cache.store_full_history(scid, history)
        
        # 智能选择消息（从50条选择20条）
        selected = cache.select_for_backend(scid, max_messages=20)
        assert len(selected) <= 20
        assert selected[0]["role"] == "system"
        
        # 获取统计信息
        stats = cache.get_stats()
        print(f"缓存条目: {stats['total_entries']}")
        ```
    
    集成到网关：
        ```python
        # 在网关初始化时创建
        _history_cache = HistoryCacheManager(backend="lru")
        
        # 在 chat_completions 函数中使用
        @router.post("/v1/chat/completions")
        async def chat_completions(request: Request):
            messages = request_data.get("messages", [])
            scid = extract_or_generate_scid(messages)
            
            # 存储 + 选择
            _history_cache.store_full_history(scid, messages)
            selected = _history_cache.select_for_backend(scid, max_messages=20)
            
            # 使用精选消息发送给后端
            request_data["messages"] = selected
            ...
        ```
    """
    
    def __init__(
        self,
        backend: str = "lru",
        redis_url: Optional[str] = None,
        max_cache_size: int = 1000,
        strategy: str = "smart",
        recent_count: int = 10
    ):
        """
        初始化历史缓存管理器
        
        Args:
            backend: 缓存后端类型 ("lru" or "redis")
                    - "lru": LRU内存缓存（快速，适合单实例）
                    - "redis": Redis持久化缓存（预留，适合多实例）
            redis_url: Redis URL（backend="redis"时需要）
                      格式: "redis://localhost:6379"
            max_cache_size: LRU缓存最大条目数（backend="lru"时有效）
                           建议值: 100-1000
            strategy: 选择策略 ("smart", "recent", "token_based")
                     - "smart": 智能选择（system+最近+重要中间）
                     - "recent": 仅保留最近N条（简单）
                     - "token_based": 基于Token的选择（预留）
            recent_count: 保留的最近消息数量（strategy="smart"时有效）
                         建议值: 5-15
        """
        # 初始化缓存后端
        self.backend: CacheBackend = self._init_backend(
            backend,
            redis_url,
            max_cache_size
        )

        # 初始化选择策略
        self.strategy: SelectionStrategy = self._init_strategy(
            strategy,
            recent_count
        )

        # 任务 1.1.1: 添加 pinned anchors 数据结构
        # 用于存储不可淘汰的工具定义，key 为 scid，value 为工具定义消息列表和时间戳
        self._pinned_anchors: Dict[str, Dict] = {}
        # 格式: { scid: { "tools": List[Dict], "timestamp": float } }

        # [FIX 2026-02-01] 添加线程锁保护 _pinned_anchors
        # 在异步环境中，复合操作（检查+写入）需要锁保护以避免数据竞争
        self._pinned_anchors_lock = threading.Lock()

        # [FIX 2026-02-01] 添加 TTL 配置，默认 24 小时后自动清理
        self._pinned_anchors_ttl = 86400  # 24 hours in seconds

        log.info(
            f"[HISTORY CACHE] 初始化完成 - "
            f"backend={backend}, strategy={strategy}, "
            f"max_cache_size={max_cache_size}, recent_count={recent_count}"
        )
    
    def _init_backend(
        self,
        backend: str,
        redis_url: Optional[str],
        max_cache_size: int
    ) -> CacheBackend:
        """
        初始化缓存后端
        
        Args:
            backend: 后端类型
            redis_url: Redis URL
            max_cache_size: LRU最大容量
            
        Returns:
            CacheBackend 实例
            
        Raises:
            ValueError: 不支持的后端类型
        """
        if backend == "lru":
            return LRUCacheBackend(max_size=max_cache_size)
        elif backend == "redis":
            # TODO: 实现 Redis 后端（Phase 2）
            raise NotImplementedError(
                "Redis backend is not implemented yet. "
                "Please use 'lru' backend for now."
            )
        else:
            raise ValueError(
                f"Unsupported backend: {backend}. "
                f"Available options: 'lru', 'redis'"
            )
    
    def _init_strategy(
        self,
        strategy: str,
        recent_count: int
    ) -> SelectionStrategy:
        """
        初始化选择策略
        
        Args:
            strategy: 策略类型
            recent_count: 最近消息数量
            
        Returns:
            SelectionStrategy 实例
            
        Raises:
            ValueError: 不支持的策略类型
        """
        if strategy == "smart":
            return SmartSelectionStrategy(recent_count=recent_count)
        elif strategy == "recent":
            # TODO: 实现 Recent Only 策略
            raise NotImplementedError(
                "Recent-only strategy is not implemented yet. "
                "Please use 'smart' strategy for now."
            )
        elif strategy == "token_based":
            # TODO: 实现 Token-based 策略（Phase 3）
            raise NotImplementedError(
                "Token-based strategy is not implemented yet. "
                "Please use 'smart' strategy for now."
            )
        else:
            raise ValueError(
                f"Unsupported strategy: {strategy}. "
                f"Available options: 'smart', 'recent', 'token_based'"
            )

    def _is_tool_definition(self, msg: Dict) -> bool:
        """
        任务 1.1.2: 识别消息是否为工具定义

        识别规则:
        1. system 消息中包含 "tools" 或 "function" 关键字
        2. 消息直接包含 "tools" 字段 (OpenAI 格式)
        3. 消息包含 "tool_choice" 字段

        Args:
            msg: 消息字典

        Returns:
            bool: 是否为工具定义消息
        """
        if not isinstance(msg, dict):
            return False

        # 规则 2: 消息直接包含 "tools" 字段 (OpenAI 格式)
        if "tools" in msg:
            return True

        # 规则 3: 消息包含 "tool_choice" 字段
        if "tool_choice" in msg:
            return True

        # 规则 1: system 消息中包含 "tools" 或 "function" 关键字
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system" and isinstance(content, str):
            content_lower = content.lower()
            # 检查是否包含工具定义相关的关键字
            tool_keywords = [
                "available tools:",
                "you have access to the following tools",
                "function definitions:",
                "tool definitions:",
                "available functions:",
                '"type": "function"',
                "'type': 'function'",
            ]
            for keyword in tool_keywords:
                if keyword.lower() in content_lower:
                    return True

        return False

    def store_full_history(self, scid: str, history: List[Dict]) -> None:
        """
        任务 1.1.3: 存储完整历史（分离工具定义和普通消息）

        Args:
            scid: 会话ID (Session Context ID)
            history: 完整历史消息列表

        注意：
            - 工具定义消息存入 _pinned_anchors（不可淘汰）
            - 普通消息存入 LRU 缓存后端
            - 如果SCID已存在，会覆盖旧数据
            - 线程安全（由后端保证）
        """
        if not history:
            log.debug(f"[HISTORY CACHE] 存储空历史 - SCID: {scid[:8]}...")
            return

        # 分离工具定义和普通消息
        tool_definitions: List[Dict] = []
        regular_messages: List[Dict] = []

        for msg in history:
            if self._is_tool_definition(msg):
                tool_definitions.append(msg)
            else:
                regular_messages.append(msg)

        # 存储工具定义到 pinned anchors（不可淘汰）
        # [FIX 2026-02-01] 使用锁保护 _pinned_anchors 的写入操作
        with self._pinned_anchors_lock:
            if tool_definitions:
                self._pinned_anchors[scid] = {
                    "tools": tool_definitions,
                    "timestamp": time.time()
                }
                log.debug(
                    f"[HISTORY CACHE] 存储工具定义到 pinned anchors - "
                    f"SCID: {scid[:8]}..., {len(tool_definitions)} 个工具定义"
                )
            elif scid in self._pinned_anchors:
                # 如果新历史没有工具定义，但之前有，更新时间戳保持活跃
                self._pinned_anchors[scid]["timestamp"] = time.time()

        # 存储普通消息到 LRU 缓存后端
        self.backend.store(scid, regular_messages)

        log.debug(
            f"[HISTORY CACHE] 存储完整历史 - "
            f"SCID: {scid[:8]}..., "
            f"工具定义: {len(tool_definitions)}, "
            f"普通消息: {len(regular_messages)}"
        )
    
    def get_full_history(self, scid: str) -> Optional[List[Dict]]:
        """
        任务 1.1.4: 获取完整历史（合并工具定义和普通消息）

        Args:
            scid: 会话ID

        Returns:
            完整历史消息列表（工具定义 + 普通消息），如果不存在返回 None

        注意：
            - 工具定义从 _pinned_anchors 获取
            - 普通消息从 LRU 缓存后端获取
            - 合并顺序：工具定义在前，普通消息在后
            - 线程安全（由后端保证）
        """
        # 从 pinned anchors 获取工具定义
        # [FIX 2026-02-01] 使用锁保护 _pinned_anchors 的读取操作
        tool_definitions: List[Dict] = []
        with self._pinned_anchors_lock:
            if scid in self._pinned_anchors:
                tool_definitions = self._pinned_anchors[scid].get("tools", [])

        # 从 LRU 缓存后端获取普通消息
        regular_messages = self.backend.get(scid)

        # 如果两者都不存在，返回 None
        if not tool_definitions and not regular_messages:
            log.debug(
                f"[HISTORY CACHE] 获取完整历史失败（不存在） - "
                f"SCID: {scid[:8]}..."
            )
            return None

        # 合并：工具定义 + 普通消息
        history: List[Dict] = []
        if tool_definitions:
            history.extend(tool_definitions)
        if regular_messages:
            history.extend(regular_messages)

        log.debug(
            f"[HISTORY CACHE] 获取完整历史 - "
            f"SCID: {scid[:8]}..., "
            f"工具定义: {len(tool_definitions)}, "
            f"普通消息: {len(regular_messages) if regular_messages else 0}, "
            f"总计: {len(history)} 消息"
        )

        return history
    
    def select_for_backend(
        self,
        scid: str,
        max_messages: int = 20,
        max_tokens: Optional[int] = None
    ) -> List[Dict]:
        """
        智能选择历史发送给后端（工具定义始终包含）

        这是核心方法！集成了缓存和选择策略。

        流程：
        1. 从 pinned anchors 获取工具定义（始终包含）
        2. 从 LRU 缓存获取普通消息
        3. 对普通消息应用智能选择策略
        4. 合并：工具定义 + 选中的普通消息
        5. 确保工具链完整性
        6. 返回精选消息

        Args:
            scid: 会话ID
            max_messages: 最大消息数量（默认20，不含工具定义）
            max_tokens: 最大Token数量（可选，暂未实现）

        Returns:
            精选后的消息列表（工具定义 + 普通消息）

        注意：
            - 工具定义始终包含，不计入 max_messages
            - 如果SCID不存在，返回空列表
            - 线程安全
            - 不修改缓存中的原始数据
        """
        # Step 1: 从 pinned anchors 获取工具定义（始终包含）
        # [FIX 2026-02-01] 使用锁保护 _pinned_anchors 的读取操作
        tool_definitions: List[Dict] = []
        with self._pinned_anchors_lock:
            if scid in self._pinned_anchors:
                tool_definitions = self._pinned_anchors[scid].get("tools", [])

        # Step 2: 从 LRU 缓存获取普通消息
        regular_messages = self.backend.get(scid)

        # 如果两者都不存在，返回空列表
        if not tool_definitions and not regular_messages:
            log.warning(
                f"[HISTORY CACHE] select_for_backend - "
                f"SCID不存在: {scid[:8]}..., 返回空列表"
            )
            return []

        # Step 3: 对普通消息应用智能选择策略
        selected_regular: List[Dict] = []
        if regular_messages:
            selected_regular = self.strategy.select(
                regular_messages,
                max_messages,
                max_tokens
            )

        # Step 4: 合并：工具定义 + 选中的普通消息
        result: List[Dict] = []
        if tool_definitions:
            result.extend(tool_definitions)
        if selected_regular:
            result.extend(selected_regular)

        log.info(
            f"[HISTORY CACHE] select_for_backend - "
            f"SCID: {scid[:8]}..., "
            f"工具定义: {len(tool_definitions)}, "
            f"普通消息: {len(regular_messages) if regular_messages else 0} → {len(selected_regular)}, "
            f"输出: {len(result)} 消息"
        )

        return result
    
    def delete_conversation(self, scid: str) -> bool:
        """
        删除指定会话的缓存（包括工具定义和普通消息）

        Args:
            scid: 会话ID

        Returns:
            是否删除成功
        """
        # 删除 pinned anchors 中的工具定义
        # [FIX 2026-02-01] 使用锁保护 _pinned_anchors 的删除操作
        with self._pinned_anchors_lock:
            pinned_deleted = scid in self._pinned_anchors
            if pinned_deleted:
                del self._pinned_anchors[scid]

        # 删除 LRU 缓存中的普通消息
        backend_deleted = self.backend.delete(scid)

        result = pinned_deleted or backend_deleted

        if result:
            log.info(
                f"[HISTORY CACHE] 删除会话 - SCID: {scid[:8]}..., "
                f"pinned: {pinned_deleted}, backend: {backend_deleted}"
            )
        else:
            log.warning(f"[HISTORY CACHE] 删除会话失败（不存在） - SCID: {scid[:8]}...")

        return result
    
    def clear_all(self) -> int:
        """
        清空所有缓存（包括工具定义和普通消息）

        警告：这是危险操作！会清空所有会话的历史缓存。

        Returns:
            清除的条目数量（pinned anchors + backend）
        """
        # 清空 pinned anchors
        # [FIX 2026-02-01] 使用锁保护 _pinned_anchors 的清空操作
        with self._pinned_anchors_lock:
            pinned_count = len(self._pinned_anchors)
            self._pinned_anchors.clear()

        # 清空 LRU 缓存
        backend_count = self.backend.clear_all()

        total_count = pinned_count + backend_count

        log.warning(
            f"[HISTORY CACHE] 清空所有缓存 - "
            f"pinned: {pinned_count}, backend: {backend_count}, "
            f"总计: {total_count} 个会话"
        )

        return total_count

    def get_stats(self) -> Dict:
        """
        获取缓存统计信息

        Returns:
            统计信息字典：
            - backend: str 后端类型
            - strategy: str 策略类型
            - total_entries: int 总条目数
            - total_messages: int 总消息数
            - pinned_anchors_count: int pinned anchors 条目数
            - pinned_tool_definitions: int 工具定义总数
            - ... (其他后端特定的统计)
        """
        stats = self.backend.get_stats()

        # 添加策略信息
        stats["strategy"] = self.strategy.__class__.__name__

        # 添加 pinned anchors 统计
        # [FIX 2026-02-01] 使用锁保护 _pinned_anchors 的读取操作
        with self._pinned_anchors_lock:
            stats["pinned_anchors_count"] = len(self._pinned_anchors)
            stats["pinned_tool_definitions"] = sum(
                len(anchor.get("tools", []))
                for anchor in self._pinned_anchors.values()
            )

        log.debug(
            f"[HISTORY CACHE] 统计信息 - "
            f"backend={stats['backend']}, "
            f"strategy={stats['strategy']}, "
            f"entries={stats['total_entries']}, "
            f"messages={stats['total_messages']}, "
            f"pinned={stats['pinned_anchors_count']}, "
            f"tools={stats['pinned_tool_definitions']}"
        )

        return stats

    def cleanup_pinned_anchors(self, max_age_seconds: int = 86400) -> int:
        """
        任务 1.1.5: 清理过期的 pinned anchors

        清理超过指定时间未访问的工具定义缓存，防止内存泄漏。

        Args:
            max_age_seconds: 最大存活时间（秒），默认24小时

        Returns:
            清理的条目数量
        """
        # [FIX 2026-02-01] 使用锁保护整个清理操作
        with self._pinned_anchors_lock:
            if not self._pinned_anchors:
                return 0

            current_time = time.time()
            expired_scids: List[str] = []

            for scid, anchor in self._pinned_anchors.items():
                timestamp = anchor.get("timestamp", 0)
                age = current_time - timestamp
                if age > max_age_seconds:
                    expired_scids.append(scid)

            # 删除过期的 anchors
            for scid in expired_scids:
                del self._pinned_anchors[scid]

        if expired_scids:
            log.info(
                f"[HISTORY CACHE] 清理过期 pinned anchors - "
                f"已清理 {len(expired_scids)} 个过期条目"
            )

        return len(expired_scids)

    def get_pinned_anchors_info(self) -> Dict:
        """
        获取 pinned anchors 的详细信息（调试用）

        Returns:
            字典包含每个 scid 的工具定义数量和时间戳
        """
        info = {}
        current_time = time.time()

        # [FIX 2026-02-01] 使用锁保护 _pinned_anchors 的读取操作
        with self._pinned_anchors_lock:
            for scid, anchor in self._pinned_anchors.items():
                tools = anchor.get("tools", [])
                timestamp = anchor.get("timestamp", 0)
                age = current_time - timestamp

                info[scid[:8] + "..."] = {
                    "tool_count": len(tools),
                    "age_seconds": round(age, 1),
                    "age_human": self._format_age(age)
                }

        return info

    def _format_age(self, seconds: float) -> str:
        """格式化时间间隔为人类可读格式"""
        if seconds < 60:
            return f"{int(seconds)}秒"
        elif seconds < 3600:
            return f"{int(seconds / 60)}分钟"
        elif seconds < 86400:
            return f"{int(seconds / 3600)}小时"
        else:
            return f"{int(seconds / 86400)}天"
