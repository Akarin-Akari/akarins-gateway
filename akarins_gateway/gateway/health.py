"""
后端健康状态管理器

跟踪每个后端的健康状态，用于动态调整路由优先级。

作者: 浮浮喵 (Claude Opus 4.5)
创建日期: 2026-01-23
迁移自: src/unified_gateway_router.py (114-220行)
"""

import asyncio
import time
from typing import Dict, Optional

from akarins_gateway.core.log import log

__all__ = [
    "BackendHealthManager",
    "get_backend_health_manager",
]


class BackendHealthManager:
    """
    后端健康状态管理器

    跟踪每个后端的健康状态，用于动态调整路由优先级：
    - 成功请求增加健康分数
    - 失败请求降低健康分数
    - 健康分数影响后端选择顺序
    - [FIX 2026-02-03] 支持后端冻结机制：当检测到连接拒绝时自动冻结
    """

    # 默认冻结时长（秒）
    DEFAULT_FREEZE_DURATION = 300  # 5 分钟
    # [FIX 2026-02-03 v2] 最大冻结记录数，防止内存泄漏
    MAX_FROZEN_BACKENDS = 100
    # [FIX 2026-02-26] 最大健康数据条目数，防止 _health_data 无限增长
    MAX_HEALTH_ENTRIES = 200

    def __init__(self):
        # 健康状态: {backend_key: {"success": int, "failure": int, "last_success": float, "last_failure": float}}
        self._health_data: Dict[str, Dict] = {}
        self._lock = asyncio.Lock()
        # [FIX 2026-02-03] 冻结状态: {backend_key: expire_timestamp}
        self._frozen_backends: Dict[str, float] = {}
        # [FIX 2026-02-03 v2] 冻结次数计数器（用于指数退避）
        self._freeze_count: Dict[str, int] = {}

    def _get_or_create(self, backend_key: str) -> Dict:
        """获取或创建后端健康数据"""
        if backend_key not in self._health_data:
            # [FIX 2026-02-26] 驱逐最久未活跃的条目，防止 _health_data 无限增长
            if len(self._health_data) > self.MAX_HEALTH_ENTRIES:
                evict_key = min(
                    self._health_data,
                    key=lambda k: max(
                        self._health_data[k].get("last_success", 0),
                        self._health_data[k].get("last_failure", 0),
                    ),
                )
                del self._health_data[evict_key]
                self._freeze_count.pop(evict_key, None)
                self._frozen_backends.pop(evict_key, None)
            self._health_data[backend_key] = {
                "success": 0,
                "failure": 0,
                "last_success": 0.0,
                "last_failure": 0.0,
                "consecutive_failures": 0,
            }
        return self._health_data[backend_key]

    def _freeze_backend_locked(self, backend_key: str, duration: float) -> tuple:
        """内部方法：在 self._lock 已持有的前提下执行冻结逻辑。

        返回 (actual_duration, freeze_count) 供调用方记录日志。
        """
        count = self._freeze_count.get(backend_key, 0) + 1
        self._freeze_count[backend_key] = count
        actual_duration = min(duration * (2 ** (count - 1)), 1800)  # 指数退避，最大 30 分钟
        self._frozen_backends[backend_key] = time.time() + actual_duration
        # 限制最大冻结记录数，防止内存泄漏
        if len(self._frozen_backends) > self.MAX_FROZEN_BACKENDS:
            oldest_key = min(self._frozen_backends, key=self._frozen_backends.get)
            self._frozen_backends.pop(oldest_key, None)
            self._freeze_count.pop(oldest_key, None)
        return actual_duration, count

    async def record_success(self, backend_key: str) -> None:
        """记录后端请求成功"""
        async with self._lock:
            data = self._get_or_create(backend_key)
            data["success"] += 1
            data["last_success"] = time.time()
            data["consecutive_failures"] = 0  # 重置连续失败计数
            # [FIX 2026-02-26] 后端真正恢复时重置冻结计数器，让指数退避归零
            if backend_key in self._freeze_count:
                old_count = self._freeze_count.pop(backend_key)
                log.info(
                    f"[BACKEND HEALTH] {backend_key} 成功 (total={data['success']}), "
                    f"freeze_count 已重置 (was {old_count})",
                    tag="GATEWAY"
                )
            else:
                log.info(f"[BACKEND HEALTH] {backend_key} 成功 (total={data['success']})", tag="GATEWAY")

    async def record_failure(self, backend_key: str, error_code: int = 0) -> None:
        """记录后端请求失败"""
        async with self._lock:
            data = self._get_or_create(backend_key)
            data["failure"] += 1
            data["last_failure"] = time.time()
            data["consecutive_failures"] += 1
            log.debug(
                f"[BACKEND HEALTH] {backend_key} 失败 (code={error_code}, consecutive={data['consecutive_failures']})",
                tag="GATEWAY"
            )

    def get_health_score(self, backend_key: str) -> float:
        """
        计算后端健康分数 (0-100)

        计算公式：
        - 基础分数 = 成功率 * 60
        - 时效分数 = 最近成功加分 * 20
        - 稳定分数 = (1 - 连续失败惩罚) * 20

        NOTE: 此方法为同步只读方法，不获取 lock。
        在 CPython asyncio 模型中，单次 dict.get() 是原子的（GIL 保证），
        最差情况仅读到略微过时的数据，对健康评分计算可接受。
        """
        data = self._health_data.get(backend_key)
        if not data:
            return 50.0  # 默认中等分数

        total = data["success"] + data["failure"]
        if total == 0:
            return 50.0

        # 成功率分数 (0-60)
        success_rate = data["success"] / total
        success_score = success_rate * 60

        # 时效分数 (0-20) - 最近 5 分钟内有成功则加分
        now = time.time()
        recency_score = 0.0
        if data["last_success"] > 0:
            time_since_success = now - data["last_success"]
            if time_since_success < 300:  # 5 分钟内
                recency_score = 20.0 * (1 - time_since_success / 300)

        # 稳定分数 (0-20) - 连续失败越多分数越低
        consecutive_failures = data["consecutive_failures"]
        stability_score = max(0, 20.0 - consecutive_failures * 5)

        return min(100.0, success_score + recency_score + stability_score)

    def get_priority_adjustment(self, backend_key: str) -> float:
        """
        获取优先级调整值

        健康分数高的后端获得负调整（优先级提高）
        健康分数低的后端获得正调整（优先级降低）

        返回值范围: -0.5 到 +0.5

        NOTE: 此方法为同步只读方法，不获取 lock。
        在 CPython asyncio 模型中，单次 dict.get() 是原子的（GIL 保证），
        最差情况仅读到略微过时的数据，对健康评分计算可接受。
        """
        score = self.get_health_score(backend_key)
        # 将 0-100 的分数映射到 -0.5 到 +0.5
        # 分数 50 -> 调整 0
        # 分数 100 -> 调整 -0.5 (优先级提高)
        # 分数 0 -> 调整 +0.5 (优先级降低)
        return (50 - score) / 100

    # ==================== [FIX 2026-02-03] 后端冻结机制 ====================

    async def freeze_backend(
        self,
        backend_key: str,
        duration: Optional[float] = None,
        reason: str = ""
    ) -> None:
        """
        冻结后端一段时间

        当检测到连接被拒绝（Connection Refused）时调用此方法，
        后续请求将跳过该后端直到冻结期结束。

        [FIX 2026-02-03 v2] 添加指数退避：连续冻结时长翻倍，上限 30 分钟

        Args:
            backend_key: 后端标识
            duration: 冻结时长（秒），默认 5 分钟
            reason: 冻结原因（用于日志）
        """
        async with self._lock:
            # [FIX 2026-02-03 v2] 指数退避：每次冻结时长翻倍，上限 30 分钟
            # [设计意图] freeze_count 跨所有 error_category 共享：
            # "惯犯"后端即使切换错误类型，冻结时长仍持续升级。
            # 这是有意为之的行为，确保持续不稳定的后端获得更长的冷却期。
            base_duration = duration if duration is not None else self.DEFAULT_FREEZE_DURATION
            actual_duration, count = self._freeze_backend_locked(backend_key, base_duration)

        log.warning(
            f"[CIRCUIT BREAKER] 🔒 后端 {backend_key} 已冻结 {actual_duration:.0f} 秒 "
            f"(第 {count} 次冻结)。原因: {reason[:200] if reason else 'Connection Refused'}",
            tag="GATEWAY"
        )

    async def is_frozen(self, backend_key: str) -> bool:
        """
        [FIX 2026-02-03 v2] 检查后端是否处于冻结状态（线程安全版本）

        Args:
            backend_key: 后端标识

        Returns:
            True 如果后端被冻结且未过期，否则 False
        """
        async with self._lock:
            expire_time = self._frozen_backends.get(backend_key, 0)
            if expire_time <= 0:
                return False

            now = time.time()
            if now < expire_time:
                return True

            # 冻结已过期，清理冻结状态
            self._frozen_backends.pop(backend_key, None)
            # [FIX 2026-02-26] 清理孤儿 freeze_count：如果后端已被驱逐出 _health_data，
            # 则 freeze_count 不再有意义，一并清理防止内存泄漏
            if backend_key not in self._health_data:
                self._freeze_count.pop(backend_key, None)
            # [FIX 2026-02-26] 不再重置冻结计数器！
            # freeze_count 只在 record_success()（后端真正恢复）或 unfreeze_backend()（手动解冻）时重置
            # 这样指数退避才能在持续失败场景下生效：
            #   第1次冻结 300s → 过期 → 再次失败 → 第2次冻结 600s → ...
            log.info(f"[CIRCUIT BREAKER] 🔓 后端 {backend_key} 冻结已过期，恢复可用 (freeze_count={self._freeze_count.get(backend_key, 0)})", tag="GATEWAY")
            return False

    async def get_freeze_remaining(self, backend_key: str) -> float:
        """
        [FIX 2026-02-03 v2] 获取后端冻结剩余时间（线程安全版本）

        Args:
            backend_key: 后端标识

        Returns:
            剩余冻结秒数，如果未冻结则返回 0
        """
        async with self._lock:
            expire_time = self._frozen_backends.get(backend_key, 0)
            if expire_time <= 0:
                return 0.0

            remaining = expire_time - time.time()
            return max(0.0, remaining)

    async def unfreeze_backend(self, backend_key: str) -> None:
        """
        手动解除后端冻结

        Args:
            backend_key: 后端标识
        """
        async with self._lock:
            if backend_key in self._frozen_backends:
                del self._frozen_backends[backend_key]
                # [FIX 2026-02-26] 手动解冻 = 运维确认后端已恢复，重置冻结计数器
                self._freeze_count.pop(backend_key, None)
                log.info(f"[CIRCUIT BREAKER] 🔓 后端 {backend_key} 手动解除冻结 (freeze_count 已重置)", tag="GATEWAY")

    def get_frozen_backends(self) -> Dict[str, float]:
        """
        获取所有被冻结的后端及其剩余冻结时间

        NOTE: 此方法为同步只读方法，不获取 lock，不变异 _frozen_backends。
        过期条目的清理交给 is_frozen() 的 async+lock 路径处理。

        Returns:
            Dict[backend_key, remaining_seconds]
        """
        now = time.time()
        return {
            backend_key: expire_time - now
            for backend_key, expire_time in self._frozen_backends.items()
            if expire_time > now
        }

    @staticmethod
    def is_connection_refused_error(error_msg: str) -> bool:
        """
        检测错误消息是否表示连接被拒绝

        Args:
            error_msg: 错误消息字符串

        Returns:
            True 如果是连接拒绝类错误
        """
        if not error_msg:
            return False

        error_lower = error_msg.lower()
        # 匹配各种连接拒绝的错误模式
        patterns = [
            "connection refused",      # Unix/Linux
            "connectex",               # Windows
            "actively refused",        # Windows 详细消息
            "no connection could be made",  # Windows
            "target machine actively refused",  # Windows 完整消息
            "econnrefused",            # Node.js / 某些库
            "dial tcp",                # Go 风格错误
        ]
        return any(pattern in error_lower for pattern in patterns)

    # ==================== [NEW 2026-02-25] HTTP 级连续失败熔断器 ====================

    # Freeze duration (seconds) by error category
    HTTP_FREEZE_DURATION_MAP = {
        "auth": 600,       # 401/403 → 10min (API key won't self-fix)
        "server": 120,     # 5xx → 2min (might recover soon)
        "timeout": 60,     # timeout → 1min (likely transient)
        "connection": 300,  # ConnectionRefused → 5min (existing default)
        "rate_limit": 30,  # 429 → 30s (transient, self-resolving)
    }

    async def record_http_failure(
        self,
        backend_key: str,
        error_category: str = "server",
        threshold: int = 5,
        status_code: int = 0,
    ) -> bool:
        """
        Record an HTTP-level failure and auto-freeze if consecutive failures reach threshold.

        Called after all retries for a backend are exhausted. Classifies the failure by
        error_category and freezes with a category-specific duration when the threshold is met.

        Args:
            backend_key: Backend identifier
            error_category: "auth" | "server" | "timeout" | "connection"
            threshold: Number of consecutive failures before auto-freeze
            status_code: HTTP status code (for logging)

        Returns:
            True if the backend was auto-frozen, False otherwise
        """
        # [FIX 2026-02-26] 冻结逻辑内联到 lock 作用域内，消除竞态窗口
        frozen_log_msg = None
        async with self._lock:
            data = self._get_or_create(backend_key)
            consecutive = data["consecutive_failures"]  # already incremented by record_failure()

            if consecutive < threshold:
                log.debug(
                    f"[CIRCUIT BREAKER] {backend_key}: consecutive_failures={consecutive}/{threshold} "
                    f"(category={error_category}, status={status_code})",
                    tag="GATEWAY"
                )
                return False

            # 阈值已达 — 使用共享冻结逻辑（不调用 self.freeze_backend，避免二次加锁）
            duration = self.HTTP_FREEZE_DURATION_MAP.get(error_category, 120)
            actual_duration, count = self._freeze_backend_locked(backend_key, duration)

            # 准备日志消息（在 lock 外输出）
            frozen_log_msg = (
                f"[CIRCUIT BREAKER] 🔒 Auto-frozen {backend_key} after {consecutive} consecutive "
                f"{error_category} failures (threshold={threshold}, freeze={actual_duration:.0f}s, "
                f"freeze_count={count})"
            )

        # 日志不需要锁保护，在 lock 外输出
        log.warning(frozen_log_msg, tag="GATEWAY")
        return True

    @staticmethod
    def classify_http_error(status_code: Optional[int], error_msg: str = "") -> str:
        """
        Classify an HTTP error into a category for circuit breaker freeze duration.

        Returns: "auth" | "server" | "timeout" | "connection"
        """
        if status_code is None:
            status_code = 0
        if status_code in (401, 403):
            return "auth"
        if status_code == 408:
            return "timeout"
        if status_code == 429:
            return "rate_limit"
        if status_code >= 500:
            return "server"
        if status_code == 0:
            error_lower = (error_msg or "").lower()
            if "timeout" in error_lower or "timed out" in error_lower:
                return "timeout"
            if "refused" in error_lower or "econnrefused" in error_lower:
                return "connection"
        return "server"  # default fallback

    # ==================== HTTP 级熔断器结束 ====================


# 全局后端健康管理器实例
_backend_health_manager = BackendHealthManager()


def get_backend_health_manager() -> BackendHealthManager:
    """获取后端健康管理器实例"""
    return _backend_health_manager
