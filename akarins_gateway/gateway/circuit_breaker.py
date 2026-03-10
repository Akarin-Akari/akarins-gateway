"""
通用熔断器机制 v2.0

[FIX 2026-02-03] 重构为完整的状态机熔断器：
- 支持 CLOSED -> OPEN -> HALF_OPEN 状态转换
- 后端级别的熔断粒度
- 可配置的失败阈值和恢复时间
- 支持手动重置和健康检查

原版本（v1.0）只有 Copilot 的简单布尔熔断，功能过于简陋。

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-01-23
重构日期: 2026-02-03
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Optional, List
import time
import threading

from akarins_gateway.core.log import log

__all__ = [
    # 新版通用熔断器
    "CircuitState",
    "BackendCircuitBreaker",
    "get_circuit_breaker",
    "get_all_circuit_breakers",
    "reset_all_circuit_breakers",
    # 旧版 Copilot 熔断器（向后兼容）
    "is_copilot_circuit_open",
    "open_copilot_circuit_breaker",
    "reset_copilot_circuit_breaker",
]


# ====================== 熔断器状态定义 ======================

class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"        # 正常状态：允许请求通过
    OPEN = "open"            # 熔断状态：拒绝所有请求
    HALF_OPEN = "half_open"  # 半开状态：允许探测请求


# ====================== 熔断器配置 ======================

# 默认配置
DEFAULT_FAILURE_THRESHOLD = 3      # 连续失败多少次后开启熔断
DEFAULT_RECOVERY_TIMEOUT = 60.0    # 熔断后多少秒进入半开状态
DEFAULT_SUCCESS_THRESHOLD = 2      # 半开状态下连续成功多少次后关闭熔断

# 针对不同后端的特殊配置
BACKEND_CONFIGS = {
    # Copilot 后端：更敏感的熔断配置（余额问题通常是持久的）
    "copilot": {
        "failure_threshold": 1,        # 1 次失败就熔断
        "recovery_timeout": 300.0,     # 5 分钟后才尝试恢复
        "success_threshold": 1,
    },
    # Ruoli 后端：较宽松的配置
    "ruoli": {
        "failure_threshold": 3,
        "recovery_timeout": 60.0,
        "success_threshold": 2,
    },
    # AnyRouter 后端
    "anyrouter": {
        "failure_threshold": 3,
        "recovery_timeout": 60.0,
        "success_threshold": 2,
    },
    # Kiro 后端
    "kiro": {
        "failure_threshold": 3,
        "recovery_timeout": 60.0,
        "success_threshold": 2,
    },
    # Antigravity 后端（模型级别）
    "gcli2api-antigravity": {
        "failure_threshold": 5,        # 更高的阈值（有多个凭证）
        "recovery_timeout": 120.0,     # 2 分钟
        "success_threshold": 3,
    },
}


# ====================== 熔断器实现 ======================

@dataclass
class BackendCircuitBreaker:
    """
    后端级别的熔断器

    实现标准的熔断器模式：
    - CLOSED: 正常状态，请求通过，记录失败次数
    - OPEN: 熔断状态，直接拒绝请求，等待恢复超时
    - HALF_OPEN: 半开状态，允许少量探测请求

    [FIX 2026-02-03] 新增功能：
    - 支持错误码级别的失败记录
    - 支持手动重置
    - 支持获取熔断器状态摘要
    """
    backend_name: str
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    recovery_timeout: float = DEFAULT_RECOVERY_TIMEOUT
    success_threshold: int = DEFAULT_SUCCESS_THRESHOLD

    # 内部状态（不在构造函数中初始化）
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _last_error_code: Optional[int] = field(default=None, init=False)
    _last_error_message: Optional[str] = field(default=None, init=False)
    _total_failures: int = field(default=0, init=False)
    _total_successes: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def record_failure(self, error_code: int = 0, error_message: str = "") -> None:
        """
        记录一次失败

        Args:
            error_code: HTTP 状态码（可选）
            error_message: 错误信息（可选）
        """
        with self._lock:
            self._failure_count += 1
            self._total_failures += 1
            self._success_count = 0  # 重置连续成功计数
            self._last_failure_time = time.time()
            self._last_error_code = error_code
            self._last_error_message = error_message

            if self._state == CircuitState.HALF_OPEN:
                # 半开状态下失败，立即回到熔断状态
                self._state = CircuitState.OPEN
                log.warning(
                    f"[CIRCUIT_BREAKER] {self.backend_name} 半开状态探测失败，"
                    f"重新熔断 (error_code={error_code})"
                )

            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    log.warning(
                        f"[CIRCUIT_BREAKER] {self.backend_name} 熔断开启，"
                        f"连续失败 {self._failure_count} 次 "
                        f"(threshold={self.failure_threshold}, error_code={error_code})"
                    )

    def record_success(self) -> None:
        """记录一次成功"""
        with self._lock:
            self._total_successes += 1
            self._failure_count = 0  # 重置连续失败计数

            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    # 半开状态下连续成功，关闭熔断
                    self._state = CircuitState.CLOSED
                    self._success_count = 0
                    log.info(
                        f"[CIRCUIT_BREAKER] {self.backend_name} 熔断关闭，"
                        f"恢复正常 (连续成功 {self.success_threshold} 次)"
                    )

            elif self._state == CircuitState.CLOSED:
                # 正常状态下成功，重置计数
                self._success_count = 0

    def is_available(self) -> bool:
        """
        检查后端是否可用

        Returns:
            True 如果可以发送请求，False 如果应该跳过
        """
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                elapsed = time.time() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    # 恢复超时已过，进入半开状态
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    log.info(
                        f"[CIRCUIT_BREAKER] {self.backend_name} 进入半开状态，"
                        f"允许探测请求 (elapsed={elapsed:.1f}s)"
                    )
                    return True
                return False

            # HALF_OPEN 状态允许请求
            return True

    def reset(self) -> None:
        """手动重置熔断器"""
        with self._lock:
            old_state = self._state
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_error_code = None
            self._last_error_message = None
            log.info(
                f"[CIRCUIT_BREAKER] {self.backend_name} 已手动重置 "
                f"(from {old_state.value} to closed)"
            )

    def get_status(self) -> Dict:
        """获取熔断器状态摘要"""
        with self._lock:
            remaining_timeout = 0.0
            if self._state == CircuitState.OPEN:
                elapsed = time.time() - self._last_failure_time
                remaining_timeout = max(0, self.recovery_timeout - elapsed)

            return {
                "backend": self.backend_name,
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "total_failures": self._total_failures,
                "total_successes": self._total_successes,
                "last_error_code": self._last_error_code,
                "last_error_message": self._last_error_message,
                "remaining_timeout": round(remaining_timeout, 1),
                "config": {
                    "failure_threshold": self.failure_threshold,
                    "recovery_timeout": self.recovery_timeout,
                    "success_threshold": self.success_threshold,
                }
            }

    @property
    def state(self) -> CircuitState:
        """获取当前状态"""
        return self._state


# ====================== 熔断器注册表 ======================

_circuit_breakers: Dict[str, BackendCircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_circuit_breaker(backend_name: str) -> BackendCircuitBreaker:
    """
    获取指定后端的熔断器（懒加载）

    Args:
        backend_name: 后端名称（如 "copilot", "ruoli", "gemini-3-pro"）

    Returns:
        BackendCircuitBreaker 实例
    """
    with _registry_lock:
        if backend_name not in _circuit_breakers:
            # 获取后端特定配置，或使用默认配置
            config = BACKEND_CONFIGS.get(backend_name, {})
            _circuit_breakers[backend_name] = BackendCircuitBreaker(
                backend_name=backend_name,
                failure_threshold=config.get("failure_threshold", DEFAULT_FAILURE_THRESHOLD),
                recovery_timeout=config.get("recovery_timeout", DEFAULT_RECOVERY_TIMEOUT),
                success_threshold=config.get("success_threshold", DEFAULT_SUCCESS_THRESHOLD),
            )
            log.info(f"[CIRCUIT_BREAKER] 创建熔断器: {backend_name}")

        return _circuit_breakers[backend_name]


def get_all_circuit_breakers() -> Dict[str, Dict]:
    """获取所有熔断器的状态摘要"""
    with _registry_lock:
        return {
            name: cb.get_status()
            for name, cb in _circuit_breakers.items()
        }


def reset_all_circuit_breakers() -> None:
    """重置所有熔断器"""
    with _registry_lock:
        for cb in _circuit_breakers.values():
            cb.reset()
        log.info(f"[CIRCUIT_BREAKER] 已重置所有熔断器 (共 {len(_circuit_breakers)} 个)")


# ====================== 旧版 Copilot 熔断器（向后兼容） ======================
# [DEPRECATED] 这些函数保留是为了向后兼容，新代码应使用 get_circuit_breaker("copilot")

def is_copilot_circuit_open() -> bool:
    """
    [DEPRECATED] 检查 Copilot 熔断器是否开启

    建议使用: not get_circuit_breaker("copilot").is_available()
    """
    return not get_circuit_breaker("copilot").is_available()


def open_copilot_circuit_breaker(reason: str = "") -> None:
    """
    [DEPRECATED] 开启 Copilot 熔断器

    建议使用: get_circuit_breaker("copilot").record_failure(402, reason)
    """
    cb = get_circuit_breaker("copilot")
    cb.record_failure(error_code=402, error_message=reason)
    log.warning(
        f"[COPILOT CIRCUIT BREAKER] 熔断器已开启，后续请求将跳过 Copilot。原因: {reason}",
        tag="GATEWAY"
    )


def reset_copilot_circuit_breaker() -> None:
    """
    [DEPRECATED] 重置 Copilot 熔断器

    建议使用: get_circuit_breaker("copilot").reset()
    """
    get_circuit_breaker("copilot").reset()
    log.info("[COPILOT CIRCUIT BREAKER] 熔断器已重置", tag="GATEWAY")
