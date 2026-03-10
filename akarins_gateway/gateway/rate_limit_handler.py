"""
Rate Limit Handler - 429 限流处理模块

[FIX 2026-01-23] 参考三个官方仓库的最佳实践：
- cliproxy: BaseURL 回退 + Retry-After 解析
- Antigravity-Manager: 限流原因识别 + 智能退避 + 模型级别限流
- gcli2api_official: 基础重试机制

实现功能：
1. 限流原因识别（QUOTA_EXHAUSTED vs RATE_LIMIT_EXCEEDED）
2. Retry-After 头解析
3. 智能退避策略（根据失败次数动态调整）
4. 模型级别限流支持
5. 精确时间锁定
"""

import json
import re
import time
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum

from akarins_gateway.core.log import log
from akarins_gateway.core.retry_utils import parse_retry_delay, parse_retry_delay_seconds


class RateLimitReason(Enum):
    """限流原因类型"""
    QUOTA_EXHAUSTED = "quota_exhausted"           # 配额耗尽
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"  # 速率限制
    MODEL_CAPACITY_EXHAUSTED = "model_capacity_exhausted"  # 模型容量耗尽
    SERVER_ERROR = "server_error"                # 服务器错误 (5xx)
    UNKNOWN = "unknown"                           # 未知原因


@dataclass
class RateLimitInfo:
    """限流信息"""
    reset_time: float  # 重置时间戳（Unix 时间）
    retry_after_sec: float  # 重试间隔（秒）
    detected_at: float  # 检测时间戳
    reason: RateLimitReason  # 限流原因
    model: Optional[str] = None  # 关联的模型（用于模型级别限流）


class RateLimitTracker:
    """
    限流跟踪器
    
    功能：
    - 跟踪账号/模型的限流状态
    - 智能退避策略（根据失败次数动态调整）
    - 精确时间锁定（使用 Retry-After 或配额刷新时间）
    """
    
    def __init__(self):
        # 限流记录: {account_id: RateLimitInfo}
        self._limits: Dict[str, RateLimitInfo] = {}
        # 连续失败计数: {account_id: (count, timestamp)}
        self._failure_counts: Dict[str, Tuple[int, float]] = {}
        # 失败计数过期时间：1小时
        self._failure_count_expiry = 3600.0
    
    def get_remaining_wait(self, account_id: str) -> float:
        """
        获取账号剩余的等待时间（秒）
        
        Args:
            account_id: 账号标识（可以是凭证名、模型名等）
            
        Returns:
            剩余等待时间（秒），如果未限流则返回 0
        """
        if account_id in self._limits:
            info = self._limits[account_id]
            now = time.time()
            if info.reset_time > now:
                return info.reset_time - now
        return 0.0
    
    def mark_success(self, account_id: str) -> None:
        """
        标记账号请求成功，重置连续失败计数
        
        当账号成功完成请求后调用此方法，将其失败计数归零，
        这样下次失败时会从最短的锁定时间开始。
        """
        if account_id in self._failure_counts:
            del self._failure_counts[account_id]
            log.info(f"[RATE_LIMIT] 账号 {account_id} 请求成功，已重置失败计数")
        # 同时清除限流记录（如果有）
        if account_id in self._limits:
            del self._limits[account_id]
    
    def set_lockout_until(
        self,
        account_id: str,
        reset_time: float,
        reason: RateLimitReason,
        model: Optional[str] = None
    ) -> None:
        """
        精确锁定账号到指定时间点
        
        使用账号配额中的 reset_time 来精确锁定账号，
        这比指数退避更加精准。
        
        Args:
            account_id: 账号标识
            reset_time: 重置时间戳（Unix 时间）
            reason: 限流原因
            model: 可选的模型名称（用于模型级别限流）
        """
        now = time.time()
        retry_sec = max(60.0, reset_time - now)  # 如果时间已过，使用默认 60 秒
        
        info = RateLimitInfo(
            reset_time=reset_time,
            retry_after_sec=retry_sec,
            detected_at=now,
            reason=reason,
            model=model
        )
        
        self._limits[account_id] = info
        
        if model:
            log.info(
                f"[RATE_LIMIT] 账号 {account_id} 的模型 {model} 已精确锁定到配额刷新时间，"
                f"剩余 {retry_sec:.1f} 秒"
            )
        else:
            log.info(
                f"[RATE_LIMIT] 账号 {account_id} 已精确锁定到配额刷新时间，"
                f"剩余 {retry_sec:.1f} 秒"
            )
    
    def parse_from_error(
        self,
        account_id: str,
        status_code: int,
        retry_after_header: Optional[str] = None,
        error_body: str = "",
        model: Optional[str] = None
    ) -> Optional[RateLimitInfo]:
        """
        从错误响应解析限流信息
        
        支持 429 (限流) 以及 500/503/529 (后端故障软避让)
        
        Args:
            account_id: 账号标识
            status_code: HTTP 状态码
            retry_after_header: Retry-After 头值
            error_body: 错误响应 body
            model: 可选的模型名称（用于模型级别限流）
            
        Returns:
            RateLimitInfo 对象，如果无法解析则返回 None
        """
        # 支持 429 (限流) 以及 500/503/529 (后端故障软避让)
        if status_code not in (429, 500, 503, 529):
            return None
        
        # 1. 解析限流原因类型
        reason = self._parse_rate_limit_reason(status_code, error_body)
        
        # 2. 从 Retry-After header 提取
        retry_after_sec = None
        if retry_after_header:
            try:
                retry_after_sec = float(retry_after_header)
            except ValueError:
                pass
        
        # 3. 从错误消息提取（优先尝试 JSON 解析，再试正则）
        if retry_after_sec is None:
            retry_after_sec = self._parse_retry_time_from_body(error_body)
            if retry_after_sec is not None:
                retry_after_sec = retry_after_sec / 1000.0  # 转换为秒
        
        # 4. 处理默认值与软避让逻辑（根据限流类型设置不同默认值）
        if retry_after_sec is None:
            # 获取连续失败次数，用于指数退避（带自动过期逻辑）
            failure_count = self._get_failure_count(account_id)
            
            retry_after_sec = self._get_default_retry_delay(reason, failure_count)
        
        # 设置安全缓冲区：最小 2 秒，防止极高频无效重试
        retry_after_sec = max(2.0, retry_after_sec)
        
        info = RateLimitInfo(
            reset_time=time.time() + retry_after_sec,
            retry_after_sec=retry_after_sec,
            detected_at=time.time(),
            reason=reason,
            model=model
        )
        
        # 存储
        self._limits[account_id] = info
        
        log.warning(
            f"[RATE_LIMIT] 账号 {account_id} [{status_code}] 限流类型: {reason.value}, "
            f"重置延时: {retry_after_sec:.1f}秒"
        )
        
        return info
    
    def _parse_rate_limit_reason(self, status_code: int, error_body: str) -> RateLimitReason:
        """
        解析限流原因类型
        
        Args:
            status_code: HTTP 状态码
            error_body: 错误响应 body
            
        Returns:
            RateLimitReason 枚举值
        """
        if status_code != 429:
            return RateLimitReason.SERVER_ERROR
        
        # 尝试从 JSON 中提取 reason 字段
        error_lower = error_body.lower()
        trimmed = error_body.strip()
        
        if trimmed.startswith('{') or trimmed.startswith('['):
            try:
                json_data = json.loads(trimmed)
                # 尝试从 error.details[0].reason 提取
                reason_str = (
                    json_data.get("error", {})
                    .get("details", [{}])[0]
                    .get("reason", "")
                )
                
                if reason_str:
                    reason_upper = reason_str.upper()
                    if "QUOTA_EXHAUSTED" in reason_upper:
                        return RateLimitReason.QUOTA_EXHAUSTED
                    elif "RATE_LIMIT_EXCEEDED" in reason_upper:
                        return RateLimitReason.RATE_LIMIT_EXCEEDED
                    elif "MODEL_CAPACITY_EXHAUSTED" in reason_upper:
                        return RateLimitReason.MODEL_CAPACITY_EXHAUSTED
                
                # 尝试从 message 字段进行文本匹配
                message = (
                    json_data.get("error", {})
                    .get("message", "")
                    .lower()
                )
                if "per minute" in message or "rate limit" in message:
                    return RateLimitReason.RATE_LIMIT_EXCEEDED
            except (json.JSONDecodeError, (KeyError, IndexError, TypeError)):
                pass
        
        # 如果无法从 JSON 解析，尝试从消息文本判断
        # 优先判断分钟级限制，避免将 TPM 误判为 Quota
        if "per minute" in error_lower or "rate limit" in error_lower or "too many requests" in error_lower:
            return RateLimitReason.RATE_LIMIT_EXCEEDED
        elif "exhausted" in error_lower or "quota" in error_lower:
            return RateLimitReason.QUOTA_EXHAUSTED
        
        return RateLimitReason.UNKNOWN
    
    def _parse_retry_time_from_body(self, error_body: str) -> Optional[float]:
        """
        从错误消息 body 中解析重置时间（毫秒）
        
        Args:
            error_body: 错误响应 body
            
        Returns:
            重试延迟（毫秒），解析失败返回 None
        """
        # 使用 retry_utils 中的解析函数
        delay_ms = parse_retry_delay(error_body)
        return delay_ms
    
    def _get_failure_count(self, account_id: str) -> int:
        """
        获取连续失败次数（带自动过期逻辑）
        
        Args:
            account_id: 账号标识
            
        Returns:
            连续失败次数
        """
        now = time.time()
        
        if account_id in self._failure_counts:
            count, timestamp = self._failure_counts[account_id]
            # 检查是否超过过期时间，如果是则重置计数
            elapsed = now - timestamp
            if elapsed > self._failure_count_expiry:
                log.debug(f"[RATE_LIMIT] 账号 {account_id} 失败计数已过期（{elapsed:.0f}秒），重置为 0")
                count = 0
        else:
            count = 0
        
        # 增加失败计数
        count += 1
        self._failure_counts[account_id] = (count, now)
        
        return count
    
    def _get_default_retry_delay(self, reason: RateLimitReason, failure_count: int) -> float:
        """
        根据限流原因和失败次数获取默认重试延迟
        
        Args:
            reason: 限流原因
            failure_count: 连续失败次数
            
        Returns:
            重试延迟（秒）
        """
        if reason == RateLimitReason.QUOTA_EXHAUSTED:
            # [智能限流] 根据连续失败次数动态调整锁定时间
            # 第1次: 60s, 第2次: 5min, 第3次: 30min, 第4次+: 2h
            if failure_count == 1:
                log.warning("检测到配额耗尽 (QUOTA_EXHAUSTED)，第1次失败，锁定 60秒")
                return 60.0
            elif failure_count == 2:
                log.warning("检测到配额耗尽 (QUOTA_EXHAUSTED)，第2次连续失败，锁定 5分钟")
                return 300.0
            elif failure_count == 3:
                log.warning("检测到配额耗尽 (QUOTA_EXHAUSTED)，第3次连续失败，锁定 30分钟")
                return 1800.0
            else:
                log.warning(f"检测到配额耗尽 (QUOTA_EXHAUSTED)，第{failure_count}次连续失败，锁定 2小时")
                return 7200.0
        elif reason == RateLimitReason.RATE_LIMIT_EXCEEDED:
            # 速率限制：通常是短暂的，使用较短的默认值（30秒）
            log.debug("检测到速率限制 (RATE_LIMIT_EXCEEDED)，使用默认值 30秒")
            return 30.0
        elif reason == RateLimitReason.MODEL_CAPACITY_EXHAUSTED:
            # 模型容量耗尽：服务端暂时无可用 GPU 实例
            # 这是临时性问题，使用较短的重试时间（15秒）
            log.warning("检测到模型容量不足 (MODEL_CAPACITY_EXHAUSTED)，服务端暂无可用实例，15秒后重试")
            return 15.0
        elif reason == RateLimitReason.SERVER_ERROR:
            # 服务器错误：执行"软避让"，默认锁定 20 秒
            log.warning("检测到 5xx 错误，执行 20s 软避让...")
            return 20.0
        else:
            # 未知原因：使用中等默认值（60秒）
            log.warning("无法解析 429 限流原因，使用默认值 60秒")
            return 60.0
    
    def is_rate_limited(self, account_id: str) -> bool:
        """
        检查账号是否仍在限流中
        
        Args:
            account_id: 账号标识
            
        Returns:
            是否仍在限流中
        """
        if account_id in self._limits:
            info = self._limits[account_id]
            return time.time() < info.reset_time
        return False
    
    def get_reset_seconds(self, account_id: str) -> Optional[float]:
        """
        获取距离限流重置还有多少秒
        
        Args:
            account_id: 账号标识
            
        Returns:
            剩余秒数，如果未限流则返回 None
        """
        if account_id in self._limits:
            info = self._limits[account_id]
            now = time.time()
            if info.reset_time > now:
                return info.reset_time - now
        return None
    
    def clear(self, account_id: str) -> bool:
        """
        清除指定账号的限流记录
        
        Args:
            account_id: 账号标识
            
        Returns:
            是否成功清除
        """
        if account_id in self._limits:
            del self._limits[account_id]
            return True
        return False


# 全局限流跟踪器实例
_global_rate_limit_tracker = RateLimitTracker()


def get_rate_limit_tracker() -> RateLimitTracker:
    """获取全局限流跟踪器实例"""
    return _global_rate_limit_tracker


def parse_rate_limit_from_response(
    account_id: str,
    status_code: int,
    headers: Dict[str, str],
    error_body: str = "",
    model: Optional[str] = None
) -> Optional[RateLimitInfo]:
    """
    从 HTTP 响应解析限流信息（便捷函数）
    
    Args:
        account_id: 账号标识
        status_code: HTTP 状态码
        headers: 响应头
        error_body: 错误响应 body
        model: 可选的模型名称
        
    Returns:
        RateLimitInfo 对象，如果无法解析则返回 None
    """
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    return _global_rate_limit_tracker.parse_from_error(
        account_id=account_id,
        status_code=status_code,
        retry_after_header=retry_after,
        error_body=error_body,
        model=model
    )
