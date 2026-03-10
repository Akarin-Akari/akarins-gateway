"""
HTTP Error Logger - 统一的 HTTP 错误分类和日志记录模块

[创建于 2026-01-24] 增强网关对 429 和 400 系列错误的日志分类
参考：antigravity_manager, cliproxy 等项目的最佳实践

功能：
1. HTTP 状态码的详细分类和解释
2. 结构化错误日志记录（包含完整上下文）
3. 错误原因识别和建议
4. 统一的日志格式，便于分析和监控
"""

import json
from enum import Enum
from typing import Any, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime

from akarins_gateway.core.log import log


class HTTPErrorCategory(Enum):
    """HTTP 错误分类"""
    # 4xx Client Errors
    BAD_REQUEST = "bad_request"                    # 400
    UNAUTHORIZED = "unauthorized"                  # 401
    PAYMENT_REQUIRED = "payment_required"          # 402
    FORBIDDEN = "forbidden"                        # 403
    NOT_FOUND = "not_found"                        # 404
    METHOD_NOT_ALLOWED = "method_not_allowed"      # 405
    REQUEST_TIMEOUT = "request_timeout"            # 408
    CONFLICT = "conflict"                          # 409
    GONE = "gone"                                  # 410
    UNPROCESSABLE_ENTITY = "unprocessable_entity"  # 422
    TOO_MANY_REQUESTS = "too_many_requests"        # 429
    
    # 5xx Server Errors
    INTERNAL_SERVER_ERROR = "internal_server_error"  # 500
    BAD_GATEWAY = "bad_gateway"                      # 502
    SERVICE_UNAVAILABLE = "service_unavailable"      # 503
    GATEWAY_TIMEOUT = "gateway_timeout"              # 504
    OVERLOADED = "overloaded"                        # 529
    
    # Special
    UNKNOWN = "unknown"


class RateLimitSubType(Enum):
    """429 错误的子类型（来自 rate_limit_handler.py）"""
    QUOTA_EXHAUSTED = "quota_exhausted"                      # 配额耗尽
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"              # 速率限制（RPM/TPM）
    MODEL_CAPACITY_EXHAUSTED = "model_capacity_exhausted"    # 模型容量耗尽
    CONCURRENT_REQUESTS = "concurrent_requests_exceeded"     # 并发请求过多
    TOKEN_LIMIT_EXCEEDED = "token_limit_exceeded"            # Token 限制
    DAILY_LIMIT_EXCEEDED = "daily_limit_exceeded"            # 日限额
    UNKNOWN = "unknown"


@dataclass
class ErrorContext:
    """错误上下文信息"""
    # 基本信息
    status_code: int
    category: HTTPErrorCategory
    
    # 请求信息
    backend_key: str
    account_id: Optional[str] = None
    model_name: Optional[str] = None
    request_id: Optional[str] = None
    
    # 响应信息
    response_body: str = ""
    response_headers: Dict[str, str] = field(default_factory=dict)
    
    # 429 特定信息
    rate_limit_sub_type: Optional[RateLimitSubType] = None
    retry_after_sec: Optional[float] = None
    
    # 额外上下文
    error_message: Optional[str] = None
    suggestions: list = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class HTTPErrorLogger:
    """HTTP 错误日志记录器"""
    
    # HTTP 状态码到分类的映射
    STATUS_CODE_MAP = {
        400: HTTPErrorCategory.BAD_REQUEST,
        401: HTTPErrorCategory.UNAUTHORIZED,
        402: HTTPErrorCategory.PAYMENT_REQUIRED,
        403: HTTPErrorCategory.FORBIDDEN,
        404: HTTPErrorCategory.NOT_FOUND,
        405: HTTPErrorCategory.METHOD_NOT_ALLOWED,
        408: HTTPErrorCategory.REQUEST_TIMEOUT,
        409: HTTPErrorCategory.CONFLICT,
        410: HTTPErrorCategory.GONE,
        422: HTTPErrorCategory.UNPROCESSABLE_ENTITY,
        429: HTTPErrorCategory.TOO_MANY_REQUESTS,
        500: HTTPErrorCategory.INTERNAL_SERVER_ERROR,
        502: HTTPErrorCategory.BAD_GATEWAY,
        503: HTTPErrorCategory.SERVICE_UNAVAILABLE,
        504: HTTPErrorCategory.GATEWAY_TIMEOUT,
        529: HTTPErrorCategory.OVERLOADED,
    }
    
    # 错误描述和建议
    ERROR_INFO = {
        HTTPErrorCategory.BAD_REQUEST: {
            "name": "Bad Request",
            "description": "请求格式错误或参数无效",
            "common_causes": [
                "请求体 JSON 格式错误",
                "必需参数缺失或类型错误",
                "参数值超出允许范围",
                "Content-Type 头不正确",
            ],
            "suggestions": [
                "检查请求体格式是否正确",
                "验证所有必需参数是否存在",
                "确认参数类型和值是否符合 API 规范",
            ]
        },
        HTTPErrorCategory.UNAUTHORIZED: {
            "name": "Unauthorized",
            "description": "身份验证失败",
            "common_causes": [
                "API Key 缺失或无效",
                "API Key 已过期",
                "API Key 格式不正确",
                "认证头缺失",
            ],
            "suggestions": [
                "检查 API Key 是否正确配置",
                "验证 API Key 是否已激活",
                "确认认证头格式是否正确",
                "检查 API Key 是否有访问该资源的权限",
            ]
        },
        HTTPErrorCategory.PAYMENT_REQUIRED: {
            "name": "Payment Required",
            "description": "需要付费或余额不足",
            "common_causes": [
                "账户余额不足",
                "未绑定支付方式",
                "配额已用完需要升级",
                "账户欠费被暂停",
            ],
            "suggestions": [
                "检查账户余额",
                "充值或升级账户套餐",
                "联系账户管理员",
                "检查账单和付费状态",
            ]
        },
        HTTPErrorCategory.FORBIDDEN: {
            "name": "Forbidden",
            "description": "权限不足，禁止访问",
            "common_causes": [
                "API Key 无访问该资源的权限",
                "账户被限制或封禁",
                "IP 地址被限制",
                "区域限制",
            ],
            "suggestions": [
                "检查 API Key 的权限范围",
                "联系账户管理员确认账户状态",
                "检查是否触发了安全策略",
                "确认 IP 地址是否在白名单中",
            ]
        },
        HTTPErrorCategory.NOT_FOUND: {
            "name": "Not Found",
            "description": "请求的资源不存在",
            "common_causes": [
                "API 端点 URL 错误",
                "资源 ID 不存在",
                "模型名称错误",
                "API 版本不支持",
            ],
            "suggestions": [
                "检查 API 端点 URL 是否正确",
                "验证资源 ID 是否存在",
                "确认模型名称拼写是否正确",
                "检查 API 版本是否支持该功能",
            ]
        },
        HTTPErrorCategory.METHOD_NOT_ALLOWED: {
            "name": "Method Not Allowed",
            "description": "HTTP 方法不被允许",
            "common_causes": [
                "使用了错误的 HTTP 方法（GET/POST/PUT/DELETE）",
                "该端点不支持该 HTTP 方法",
            ],
            "suggestions": [
                "检查 API 文档确认正确的 HTTP 方法",
                "确认端点是否支持该方法",
            ]
        },
        HTTPErrorCategory.REQUEST_TIMEOUT: {
            "name": "Request Timeout",
            "description": "请求超时",
            "common_causes": [
                "网络连接不稳定",
                "请求处理时间过长",
                "服务器响应慢",
            ],
            "suggestions": [
                "检查网络连接",
                "增加超时时间",
                "优化请求内容（减少 token 数量）",
                "稍后重试",
            ]
        },
        HTTPErrorCategory.CONFLICT: {
            "name": "Conflict",
            "description": "请求冲突",
            "common_causes": [
                "资源状态冲突",
                "并发修改冲突",
                "重复创建资源",
            ],
            "suggestions": [
                "检查资源当前状态",
                "使用乐观锁或版本控制",
                "避免并发修改同一资源",
            ]
        },
        HTTPErrorCategory.GONE: {
            "name": "Gone",
            "description": "资源已永久删除",
            "common_causes": [
                "资源已被删除",
                "API 端点已废弃",
            ],
            "suggestions": [
                "使用新的 API 端点",
                "检查 API 版本更新日志",
            ]
        },
        HTTPErrorCategory.UNPROCESSABLE_ENTITY: {
            "name": "Unprocessable Entity",
            "description": "请求格式正确但语义错误",
            "common_causes": [
                "参数值不符合业务规则",
                "模型不支持该功能",
                "输入内容违反安全策略",
                "Token 数量超限",
            ],
            "suggestions": [
                "检查参数值是否符合业务规则",
                "确认模型是否支持该功能",
                "检查输入内容是否包含敏感信息",
                "减少输入 token 数量",
            ]
        },
        HTTPErrorCategory.TOO_MANY_REQUESTS: {
            "name": "Too Many Requests",
            "description": "请求频率超限",
            "common_causes": [
                "超过每分钟请求限制（RPM）",
                "超过每分钟 Token 限制（TPM）",
                "配额已用完",
                "并发请求过多",
                "日限额已用完",
            ],
            "suggestions": [
                "等待一段时间后重试",
                "降低请求频率",
                "增加账户配额",
                "使用指数退避策略",
                "优化请求内容减少 token 消耗",
            ]
        },
        HTTPErrorCategory.INTERNAL_SERVER_ERROR: {
            "name": "Internal Server Error",
            "description": "服务器内部错误",
            "common_causes": [
                "服务器程序异常",
                "后端服务故障",
                "临时性错误",
            ],
            "suggestions": [
                "稍后重试",
                "联系技术支持",
                "检查服务状态页面",
            ]
        },
        HTTPErrorCategory.BAD_GATEWAY: {
            "name": "Bad Gateway",
            "description": "网关错误",
            "common_causes": [
                "上游服务器无响应",
                "上游服务器返回无效响应",
                "网关配置错误",
            ],
            "suggestions": [
                "稍后重试",
                "检查上游服务状态",
                "联系网关管理员",
            ]
        },
        HTTPErrorCategory.SERVICE_UNAVAILABLE: {
            "name": "Service Unavailable",
            "description": "服务暂时不可用",
            "common_causes": [
                "服务器维护中",
                "服务器过载",
                "临时故障",
            ],
            "suggestions": [
                "等待一段时间后重试",
                "检查服务状态页面",
                "使用备用服务",
            ]
        },
        HTTPErrorCategory.GATEWAY_TIMEOUT: {
            "name": "Gateway Timeout",
            "description": "网关超时",
            "common_causes": [
                "上游服务器响应超时",
                "请求处理时间过长",
                "网络延迟过高",
            ],
            "suggestions": [
                "增加超时时间",
                "优化请求内容",
                "稍后重试",
                "使用更快的后端",
            ]
        },
        HTTPErrorCategory.OVERLOADED: {
            "name": "Overloaded",
            "description": "服务器过载（Google 特有）",
            "common_causes": [
                "服务器负载过高",
                "请求量超过服务器处理能力",
                "临时性过载",
            ],
            "suggestions": [
                "等待一段时间后重试",
                "降低请求频率",
                "使用指数退避策略",
            ]
        },
    }
    
    @classmethod
    def get_category(cls, status_code: int) -> HTTPErrorCategory:
        """获取状态码对应的错误分类"""
        return cls.STATUS_CODE_MAP.get(status_code, HTTPErrorCategory.UNKNOWN)
    
    @classmethod
    def parse_429_sub_type(cls, error_body: str) -> RateLimitSubType:
        """
        解析 429 错误的子类型
        
        参考 rate_limit_handler.py 的实现
        """
        if not error_body:
            return RateLimitSubType.UNKNOWN
        
        error_lower = error_body.lower()
        
        # 尝试 JSON 解析
        if error_body.strip().startswith('{'):
            try:
                json_data = json.loads(error_body)
                reason_str = (
                    json_data.get("error", {})
                    .get("details", [{}])[0]
                    .get("reason", "")
                )
                
                if reason_str:
                    reason_upper = reason_str.upper()
                    if "QUOTA_EXHAUSTED" in reason_upper:
                        return RateLimitSubType.QUOTA_EXHAUSTED
                    elif "RATE_LIMIT_EXCEEDED" in reason_upper:
                        return RateLimitSubType.RATE_LIMIT_EXCEEDED
                    elif "MODEL_CAPACITY_EXHAUSTED" in reason_upper:
                        return RateLimitSubType.MODEL_CAPACITY_EXHAUSTED
                    elif "CONCURRENT" in reason_upper:
                        return RateLimitSubType.CONCURRENT_REQUESTS
                
                # 检查 message 字段
                message = (
                    json_data.get("error", {})
                    .get("message", "")
                    .lower()
                )
                if "per minute" in message or "rate limit" in message:
                    return RateLimitSubType.RATE_LIMIT_EXCEEDED
                elif "per day" in message or "daily" in message:
                    return RateLimitSubType.DAILY_LIMIT_EXCEEDED
                elif "token" in message and "limit" in message:
                    return RateLimitSubType.TOKEN_LIMIT_EXCEEDED
                elif "concurrent" in message:
                    return RateLimitSubType.CONCURRENT_REQUESTS
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                pass
        
        # 文本匹配
        if "per minute" in error_lower or "rpm" in error_lower or "tpm" in error_lower:
            return RateLimitSubType.RATE_LIMIT_EXCEEDED
        elif "per day" in error_lower or "daily" in error_lower:
            return RateLimitSubType.DAILY_LIMIT_EXCEEDED
        elif "concurrent" in error_lower:
            return RateLimitSubType.CONCURRENT_REQUESTS
        elif "token" in error_lower and "limit" in error_lower:
            return RateLimitSubType.TOKEN_LIMIT_EXCEEDED
        elif "exhausted" in error_lower or "quota" in error_lower:
            return RateLimitSubType.QUOTA_EXHAUSTED
        elif "capacity" in error_lower:
            return RateLimitSubType.MODEL_CAPACITY_EXHAUSTED
        
        return RateLimitSubType.UNKNOWN
    
    @classmethod
    def extract_retry_after(cls, headers: Dict[str, str]) -> Optional[float]:
        """从响应头提取 Retry-After 值（秒）"""
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return None
    
    @classmethod
    def build_error_context(
        cls,
        status_code: int,
        backend_key: str,
        response_body: str = "",
        response_headers: Optional[Dict[str, str]] = None,
        account_id: Optional[str] = None,
        model_name: Optional[str] = None,
        request_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ErrorContext:
        """
        构建错误上下文
        
        Args:
            status_code: HTTP 状态码
            backend_key: 后端标识
            response_body: 响应体
            response_headers: 响应头
            account_id: 账号 ID
            model_name: 模型名
            request_id: 请求 ID
            metadata: 额外元数据
            
        Returns:
            ErrorContext 对象
        """
        headers = response_headers or {}
        category = cls.get_category(status_code)
        
        # 提取错误消息
        error_message = cls._extract_error_message(response_body)
        
        # 构建上下文
        context = ErrorContext(
            status_code=status_code,
            category=category,
            backend_key=backend_key,
            account_id=account_id,
            model_name=model_name,
            request_id=request_id,
            response_body=response_body[:500] if response_body else "",  # 限制长度
            response_headers=headers,
            error_message=error_message,
            metadata=metadata or {},
        )
        
        # 429 特殊处理
        if status_code == 429:
            context.rate_limit_sub_type = cls.parse_429_sub_type(response_body)
            context.retry_after_sec = cls.extract_retry_after(headers)
        
        # 添加建议
        error_info = cls.ERROR_INFO.get(category)
        if error_info:
            context.suggestions = error_info["suggestions"]
        
        return context
    
    @classmethod
    def _extract_error_message(cls, response_body: str) -> Optional[str]:
        """从响应体提取错误消息"""
        if not response_body:
            return None
        
        # 尝试 JSON 解析
        if response_body.strip().startswith('{'):
            try:
                json_data = json.loads(response_body)
                # 尝试多个可能的字段
                for field in ["error.message", "message", "error", "detail"]:
                    parts = field.split(".")
                    value = json_data
                    for part in parts:
                        if isinstance(value, dict):
                            value = value.get(part)
                        else:
                            break
                    if value and isinstance(value, str):
                        return value
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        
        # 返回前 200 字符
        return response_body[:200] if len(response_body) > 200 else response_body
    
    @classmethod
    def log_error(cls, context: ErrorContext, level: str = "warning") -> None:
        """
        记录错误日志
        
        Args:
            context: 错误上下文
            level: 日志级别（debug/info/warning/error）
        """
        category = context.category
        error_info = cls.ERROR_INFO.get(category, {})
        
        # 构建日志消息
        log_parts = []
        log_parts.append(f"[HTTP {context.status_code}] {error_info.get('name', 'Unknown Error')}")
        log_parts.append(f"Backend: {context.backend_key}")
        
        if context.account_id:
            log_parts.append(f"Account: {context.account_id}")
        
        if context.model_name:
            log_parts.append(f"Model: {context.model_name}")
        
        if context.request_id:
            log_parts.append(f"RequestID: {context.request_id}")
        
        # 429 特殊处理
        if context.status_code == 429 and context.rate_limit_sub_type:
            log_parts.append(f"SubType: {context.rate_limit_sub_type.value}")
            if context.retry_after_sec:
                log_parts.append(f"RetryAfter: {context.retry_after_sec:.1f}s")
        
        if context.error_message:
            log_parts.append(f"Message: {context.error_message}")
        
        # 描述
        if error_info.get('description'):
            log_parts.append(f"Description: {error_info['description']}")
        
        log_message = " | ".join(log_parts)
        
        # 记录日志
        if level == "debug":
            log.debug(log_message, tag="HTTP_ERROR")
        elif level == "info":
            log.info(log_message, tag="HTTP_ERROR")
        elif level == "warning":
            log.warning(log_message, tag="HTTP_ERROR")
        elif level == "error":
            log.error(log_message, tag="HTTP_ERROR")
        
        # 详细信息（debug 级别）
        if level in ("warning", "error"):
            if error_info.get('common_causes'):
                log.debug(f"[HTTP_ERROR] Common causes: {', '.join(error_info['common_causes'][:3])}", tag="HTTP_ERROR")
            
            if context.suggestions:
                log.debug(f"[HTTP_ERROR] Suggestions: {', '.join(context.suggestions[:3])}", tag="HTTP_ERROR")
    
    @classmethod
    def log_error_simple(
        cls,
        status_code: int,
        backend_key: str,
        response_body: str = "",
        response_headers: Optional[Dict[str, str]] = None,
        account_id: Optional[str] = None,
        model_name: Optional[str] = None,
        request_id: Optional[str] = None,
        level: str = "warning",
    ) -> None:
        """
        简化的错误日志记录接口
        
        Args:
            status_code: HTTP 状态码
            backend_key: 后端标识
            response_body: 响应体
            response_headers: 响应头
            account_id: 账号 ID
            model_name: 模型名
            request_id: 请求 ID
            level: 日志级别
        """
        context = cls.build_error_context(
            status_code=status_code,
            backend_key=backend_key,
            response_body=response_body,
            response_headers=response_headers,
            account_id=account_id,
            model_name=model_name,
            request_id=request_id,
        )
        cls.log_error(context, level=level)


# 便捷函数
def log_http_error(
    status_code: int,
    backend_key: str,
    response_body: str = "",
    response_headers: Optional[Dict[str, str]] = None,
    account_id: Optional[str] = None,
    model_name: Optional[str] = None,
    request_id: Optional[str] = None,
    level: str = "warning",
) -> None:
    """便捷的错误日志记录函数"""
    HTTPErrorLogger.log_error_simple(
        status_code=status_code,
        backend_key=backend_key,
        response_body=response_body,
        response_headers=response_headers,
        account_id=account_id,
        model_name=model_name,
        request_id=request_id,
        level=level,
    )
