"""
Gateway Concurrency Control - 网关并发控制模块 v2.0

[FIX 2026-01-23] 参考 antigravity_api.py 的并发控制：
- 限制每个后端的并发请求数
- 防止瞬时并发过高触发限流
- 主动削峰，减少 429 错误

[FIX 2026-02-01] 添加并发许可获取超时机制：
- 防止因并发槽耗尽导致请求无限等待
- 超时后快速失败，让 Gateway 尝试下一个后端

[FIX 2026-02-03] 新增自适应并发控制：
- AdaptiveConcurrencyController: 动态调整并发数
- 检测到网络错误/429 时自动降低并发
- 网络稳定后自动恢复
- 基于延迟的智能调整

参考实现：src/antigravity_api.py (_antigravity_concurrency_semaphore)

作者: 浮浮酱 (Claude Opus 4.5)
"""

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Deque

from akarins_gateway.core.log import log


# ====================== 自适应并发控制配置 ======================

class ConcurrencyErrorType(Enum):
    """触发并发调整的错误类型"""
    CONNECTION_RESET = "connection_reset"    # WinError 10054 等
    TIMEOUT = "timeout"                      # 请求超时
    RATE_LIMIT = "rate_limit"                # 429 Too Many Requests
    SERVER_ERROR = "server_error"            # 5xx 错误
    SUCCESS = "success"                      # 成功（用于恢复并发）


# 默认配置
DEFAULT_INITIAL_LIMIT = 8   # 默认初始并发数
DEFAULT_MIN_LIMIT = 1       # 最小并发数
DEFAULT_MAX_LIMIT = 16      # 最大并发数

# 调整参数
DECREASE_RATIO = 0.5        # 检测到错误时减少比例
INCREASE_STEP = 1           # 成功时增加步长
SUCCESS_THRESHOLD = 10      # 连续成功多少次后增加
LATENCY_THRESHOLD_MS = 5000 # 高延迟阈值（毫秒）

# 稳定期参数
STABILITY_WINDOW = 30.0     # 稳定期窗口（秒）
MIN_REQUESTS_FOR_INCREASE = 5  # 稳定期内最少请求数


# ====================== 自适应并发控制器 ======================

@dataclass
class AdaptiveConcurrencyController:
    """
    自适应并发控制器

    [FIX 2026-02-03] 核心功能：
    - 动态调整并发限制
    - 检测到 429/连接重置时自动降低
    - 网络稳定后自动恢复
    - 基于延迟的智能调整

    Usage:
        controller = get_adaptive_concurrency_controller()

        async with controller.acquire_context():
            response = await make_request()

        # 或者手动管理
        if await controller.acquire():
            try:
                response = await make_request()
                await controller.record_success(latency_ms)
            except ConnectionResetError:
                await controller.record_failure(ConcurrencyErrorType.CONNECTION_RESET)
            finally:
                controller.release()
    """

    initial_limit: int = DEFAULT_INITIAL_LIMIT
    min_limit: int = DEFAULT_MIN_LIMIT
    max_limit: int = DEFAULT_MAX_LIMIT

    # 调整参数
    decrease_ratio: float = DECREASE_RATIO
    increase_step: int = INCREASE_STEP
    success_threshold: int = SUCCESS_THRESHOLD

    # 内部状态
    _current_limit: int = field(default=0, init=False)
    _semaphore: Optional[asyncio.Semaphore] = field(default=None, init=False)
    _success_count: int = field(default=0, init=False)
    _failure_count: int = field(default=0, init=False)
    _total_requests: int = field(default=0, init=False)
    _total_successes: int = field(default=0, init=False)
    _total_failures: int = field(default=0, init=False)
    _recent_latencies: Deque[float] = field(default_factory=lambda: deque(maxlen=20), init=False)
    _last_decrease_time: float = field(default=0.0, init=False)
    _last_increase_time: float = field(default=0.0, init=False)
    _lock: Optional[asyncio.Lock] = field(default=None, init=False)
    _initialized: bool = field(default=False, init=False)
    # [FIX 2026-02-03] 追踪已获取的槽位数，防止 release() 过度释放
    # [FIX 2026-02-03 v2] 使用 threading.Lock 而非 asyncio.Lock，确保 release() 可同步调用
    _acquired_count: int = field(default=0, init=False)
    _acquired_lock: Optional["threading.Lock"] = field(default=None, init=False)

    def __post_init__(self):
        self._current_limit = self.initial_limit

    def _ensure_initialized(self):
        """确保 Semaphore 已初始化

        [FIX 2026-02-03] 添加双重检查锁定模式，防止并发初始化
        """
        if not self._initialized:
            # 使用同步锁进行初始化（dataclass 初始化时无法使用 asyncio.Lock）
            import threading
            if not hasattr(self, '_init_lock'):
                self._init_lock = threading.Lock()

            with self._init_lock:
                if not self._initialized:  # 双重检查
                    self._semaphore = asyncio.Semaphore(self._current_limit)
                    self._lock = asyncio.Lock()
                    # [FIX 2026-02-03 v2] 使用 threading.Lock 确保 release() 可同步调用
                    import threading as _threading
                    self._acquired_lock = _threading.Lock()
                    self._initialized = True

    async def acquire(self, timeout: float = 120.0) -> bool:
        """获取并发槽位

        [FIX 2026-02-03] 追踪已获取的槽位数，确保 release() 配对
        [FIX 2026-02-03 v2] 使用同步 threading.Lock 进行计数
        """
        self._ensure_initialized()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout)
            with self._acquired_lock:  # 同步锁
                self._acquired_count += 1
            self._total_requests += 1
            return True
        except asyncio.TimeoutError:
            log.warning(
                f"[ADAPTIVE_CONCURRENCY] 获取槽位超时 ({timeout}s), "
                f"current_limit={self._current_limit}"
            )
            return False

    def release(self):
        """释放并发槽位

        [FIX 2026-02-03] 添加配对检查，防止过度释放导致 Semaphore 值超过上限
        [FIX 2026-02-03 v2] 改回同步方法，确保调用者无需 await
        """
        if self._semaphore is None:
            log.warning("[ADAPTIVE_CONCURRENCY] release() 调用时 Semaphore 未初始化")
            return

        with self._acquired_lock:  # 同步锁
            if self._acquired_count <= 0:
                log.warning(
                    "[ADAPTIVE_CONCURRENCY] release() 调用无配对的 acquire()，忽略"
                )
                return
            self._acquired_count -= 1

        self._semaphore.release()

    async def record_success(self, latency_ms: float = 0):
        """记录成功请求"""
        async with self._lock:
            self._success_count += 1
            self._total_successes += 1
            self._failure_count = 0

            if latency_ms > 0:
                self._recent_latencies.append(latency_ms)

            if (self._success_count >= self.success_threshold and
                self._current_limit < self.max_limit and
                self._is_stable_period()):
                await self._increase_limit()
                self._success_count = 0

    async def record_failure(self, error_type: ConcurrencyErrorType):
        """记录失败请求"""
        async with self._lock:
            self._failure_count += 1
            self._total_failures += 1
            self._success_count = 0

            should_decrease = error_type in (
                ConcurrencyErrorType.CONNECTION_RESET,
                ConcurrencyErrorType.TIMEOUT,
                ConcurrencyErrorType.RATE_LIMIT,
            )

            if should_decrease and self._current_limit > self.min_limit:
                await self._decrease_limit(error_type)

    async def _decrease_limit(self, reason: ConcurrencyErrorType):
        """降低并发限制

        [FIX 2026-02-03] 通过消耗槽位实现真正的并发降低：
        - 计算需要减少的槽位数
        - 使用 nowait 尝试获取这些槽位（不阻塞）
        - 获取到的槽位不释放，从而降低实际可用并发数
        """
        old_limit = self._current_limit
        new_limit = max(self.min_limit, int(self._current_limit * self.decrease_ratio))

        if new_limit >= old_limit:
            return

        slots_to_consume = old_limit - new_limit
        consumed = 0

        # 尝试消耗多余的槽位（非阻塞）
        for _ in range(slots_to_consume):
            try:
                # 使用 nowait 尝试获取，不阻塞
                self._semaphore._value  # 检查内部值
                if self._semaphore._value > 0:
                    # 直接减少内部值（安全操作，因为在锁内）
                    self._semaphore._value -= 1
                    consumed += 1
            except Exception:
                break

        self._current_limit = new_limit
        self._last_decrease_time = time.time()

        log.warning(
            f"[ADAPTIVE_CONCURRENCY] 并发降低: {old_limit} -> {new_limit} "
            f"(reason={reason.value}, slots_consumed={consumed})"
        )

    async def _increase_limit(self):
        """增加并发限制"""
        if self._current_limit >= self.max_limit:
            return

        old_limit = self._current_limit
        new_limit = min(self.max_limit, self._current_limit + self.increase_step)

        for _ in range(new_limit - old_limit):
            self._semaphore.release()

        self._current_limit = new_limit
        self._last_increase_time = time.time()

        log.info(
            f"[ADAPTIVE_CONCURRENCY] 并发提升: {old_limit} -> {new_limit}"
        )

    def _is_stable_period(self) -> bool:
        """检查是否处于稳定期"""
        now = time.time()
        if now - self._last_decrease_time < STABILITY_WINDOW:
            return False
        if self._total_requests < MIN_REQUESTS_FOR_INCREASE:
            return False
        if self._recent_latencies:
            avg_latency = sum(self._recent_latencies) / len(self._recent_latencies)
            if avg_latency > LATENCY_THRESHOLD_MS:
                return False
        return True

    @property
    def current_limit(self) -> int:
        return self._current_limit

    def get_stats(self) -> dict:
        """获取统计信息"""
        avg_latency = 0.0
        if self._recent_latencies:
            avg_latency = sum(self._recent_latencies) / len(self._recent_latencies)

        return {
            "current_limit": self._current_limit,
            "total_requests": self._total_requests,
            "total_successes": self._total_successes,
            "total_failures": self._total_failures,
            "consecutive_successes": self._success_count,
            "avg_latency_ms": round(avg_latency, 2),
        }

    def acquire_context(self, timeout: float = 120.0):
        """上下文管理器方式获取槽位"""
        return _AdaptiveAcquireContext(self, timeout)


class _AdaptiveAcquireContext:
    """自适应并发槽位的上下文管理器"""

    def __init__(self, controller: AdaptiveConcurrencyController, timeout: float):
        self._controller = controller
        self._timeout = timeout
        self._acquired = False

    async def __aenter__(self):
        self._acquired = await self._controller.acquire(self._timeout)
        if not self._acquired:
            raise asyncio.TimeoutError("Failed to acquire adaptive concurrency slot")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._acquired:
            self._controller.release()  # [FIX 2026-02-03 v2] release() 已改回同步方法
        return False


# 全局自适应控制器实例
_adaptive_controller: Optional[AdaptiveConcurrencyController] = None
_controller_init_lock = None  # [FIX 2026-02-03] 线程安全的初始化锁


def get_adaptive_concurrency_controller() -> AdaptiveConcurrencyController:
    """获取全局自适应并发控制器（单例）

    [FIX 2026-02-03] 添加线程安全的双重检查锁定
    """
    global _adaptive_controller, _controller_init_lock
    import threading

    if _adaptive_controller is not None:
        return _adaptive_controller

    # 延迟初始化锁
    if _controller_init_lock is None:
        _controller_init_lock = threading.Lock()

    with _controller_init_lock:
        # 双重检查
        if _adaptive_controller is None:
            initial = int(os.getenv("ANTIGRAVITY_MAX_CONCURRENCY", str(DEFAULT_INITIAL_LIMIT)))
            min_limit = int(os.getenv("ANTIGRAVITY_MIN_CONCURRENCY", str(DEFAULT_MIN_LIMIT)))
            max_limit = int(os.getenv("ANTIGRAVITY_MAX_CONCURRENCY_LIMIT", str(DEFAULT_MAX_LIMIT)))

            _adaptive_controller = AdaptiveConcurrencyController(
                initial_limit=initial,
                min_limit=min_limit,
                max_limit=max_limit,
            )

            log.info(
                f"[ADAPTIVE_CONCURRENCY] 控制器已初始化: "
                f"initial={initial}, min={min_limit}, max={max_limit}"
            )

    return _adaptive_controller


def get_concurrency_stats() -> dict:
    """获取当前并发控制器的统计信息"""
    controller = get_adaptive_concurrency_controller()
    return controller.get_stats()


# ====================== 旧版固定并发控制（向后兼容） ======================

# 每个后端的并发信号量: {backend_key: Semaphore}
_backend_semaphores: Dict[str, asyncio.Semaphore] = {}
_semaphore_lock = asyncio.Lock()

# [FIX 2026-02-01] 并发许可获取超时（秒）
# [FIX 2026-02-02] 从 10s 提高到 120s，适应 Claude thinking 模式的长时间思考
# 原因：Claude thinking 模式可能持续几分钟，10s 超时会导致新请求过早失败
PERMIT_ACQUIRE_TIMEOUT = float(os.getenv("GATEWAY_PERMIT_TIMEOUT", "120.0"))


def _get_backend_max_concurrency(backend_key: str) -> int:
    """
    获取指定后端的最大并发数
    
    Args:
        backend_key: 后端标识
        
    Returns:
        最大并发数（默认 2）
    """
    # 从环境变量读取，格式：GATEWAY_MAX_CONCURRENCY_<BACKEND_KEY>=<number>
    # [FIX 2026-02-02] 提高默认并发限制 2 -> 8，解决多客户端（Cursor + Claude Code）并发时响应慢的问题
    env_key = f"GATEWAY_MAX_CONCURRENCY_{backend_key.upper().replace('-', '_')}"
    try:
        v = int(os.getenv(env_key, os.getenv("GATEWAY_MAX_CONCURRENCY", "8")))
    except Exception:
        v = 2
    return max(1, v)


async def get_backend_semaphore(backend_key: str) -> asyncio.Semaphore:
    """
    获取指定后端的并发信号量
    
    Args:
        backend_key: 后端标识
        
    Returns:
        Semaphore 对象
    """
    async with _semaphore_lock:
        if backend_key not in _backend_semaphores:
            max_concurrency = _get_backend_max_concurrency(backend_key)
            _backend_semaphores[backend_key] = asyncio.Semaphore(max_concurrency)
            log.info(f"[CONCURRENCY] 初始化后端 {backend_key} 的并发限制: {max_concurrency}")
        return _backend_semaphores[backend_key]


class PermitAcquireTimeout(Exception):
    """并发许可获取超时异常"""
    pass


class BackendPermit:
    """
    后端请求许可（上下文管理器）
    
    用于自动获取和释放后端并发信号量
    
    [FIX 2026-02-01] 添加超时机制，防止无限等待
    
    Usage:
        async with BackendPermit("gcli2api-antigravity"):
            # 执行请求
            response = await client.post(...)
    
    Raises:
        PermitAcquireTimeout: 如果获取许可超时
    """
    
    def __init__(self, backend_key: str, timeout: float = None):
        self._backend_key = backend_key
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._acquired = False
        self._timeout = timeout if timeout is not None else PERMIT_ACQUIRE_TIMEOUT
    
    async def __aenter__(self):
        """获取并发许可（带超时）"""
        self._semaphore = await get_backend_semaphore(self._backend_key)
        
        # [FIX 2026-02-01] 使用 wait_for 添加超时机制
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._timeout
            )
            self._acquired = True
            log.debug(f"[CONCURRENCY] 获取后端 {self._backend_key} 的并发许可")
            return self
        except asyncio.TimeoutError:
            log.warning(
                f"[CONCURRENCY] 获取后端 {self._backend_key} 的并发许可超时 "
                f"({self._timeout}s)，后端可能过载"
            )
            raise PermitAcquireTimeout(
                f"Failed to acquire permit for {self._backend_key} within {self._timeout}s"
            )
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """释放并发许可"""
        if self._acquired and self._semaphore:
            self._semaphore.release()
            self._acquired = False
            log.debug(f"[CONCURRENCY] 释放后端 {self._backend_key} 的并发许可")


async def acquire_backend_permit(backend_key: str) -> BackendPermit:
    """
    获取后端请求许可（便捷函数）
    
    Args:
        backend_key: 后端标识
        
    Returns:
        BackendPermit 对象（可用作 async context manager）
    """
    return BackendPermit(backend_key)
