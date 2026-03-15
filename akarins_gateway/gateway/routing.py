"""
Gateway 路由决策模块

包含后端选择和优先级排序逻辑。

从 unified_gateway_router.py 抽取的路由决策函数。

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-01-18
"""

import os
import re
from typing import List, Tuple, Optional, Dict, Any

from .config import (
    BACKENDS,
    KIRO_GATEWAY_MODELS,
    RETRY_CONFIG,
    normalize_model_name,
    is_antigravity_supported,
    MODEL_ROUTING,
    get_model_routing_rule,
    ModelRoutingRule,
)
from .config_loader import (
    BackendEntry,
    get_default_routing_rule,
    get_catch_all_routing,
    get_final_fallback,
    is_backend_capable,
)

# [REFACTOR 2026-02-14] Public station module — unified model support checks
from akarins_gateway.gateway.backends.public_station import get_public_station_manager as _get_psm

# 延迟导入 log，避免循环依赖
try:
    from akarins_gateway.core.log import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)



def _is_model_supported_by_backend(backend: str, model: str) -> bool:
    """
    统一检查后端是否支持该模型

    整合 YAML backend_capabilities 和 PublicStationManager（公益站）:
    - 公益站 (ruoli, anyrouter 等) → PSM 优先判断
    - YAML 定义的后端 (antigravity, kiro-gateway 等) → is_backend_capable()
    - copilot → YAML "*" 模式，永远返回 True

    Args:
        backend: 后端名称
        model: 模型名称

    Returns:
        是否支持
    """
    # [FIX 2026-02-21] S2: 公益站由 PSM 优先判断，避免被 is_backend_capable 的默认 True 遮蔽
    try:
        psm = _get_psm()
        if psm.is_public_station(backend):
            return psm.supports_model(backend, model)
    except Exception:
        pass

    # YAML 定义的后端能力（含 copilot 的 "*" 通配）
    if is_backend_capable(backend, model):
        return True

    # [NEW 2026-03-10] Layer 2: Check ModelRegistry dynamic discovery
    try:
        from .model_registry import get_model_registry
        registry = get_model_registry()
        if registry.enabled and registry.initialized:
            if registry.is_model_known_to_backend(model, backend):
                return True
    except Exception:
        pass

    return False

__all__ = [
    "get_sorted_backends",
    "get_backend_for_model",
    "get_backend_and_model_for_routing",  # 新增：返回后端和目标模型
    "get_backend_base_url",
    "calculate_retry_delay",
    "should_retry",
    "check_backend_health",
    # [DEPRECATED 2026-03-14] Legacy model support checks — kept for backward compat but no longer exported
    # Use config_loader.is_backend_capable(backend, model) instead.
    # "is_kiro_gateway_supported", "KIRO_GATEWAY_SUPPORTED_MODELS",
    # "is_ruoli_supported", "RUOLI_SUPPORTED_MODELS",
    # "is_anyrouter_supported", "ANYROUTER_SUPPORTED_MODELS",
    # "is_newapi_public_supported", "NEWAPI_PUBLIC_SUPPORTED_MODELS",
    # [DEPRECATED 2026-03-14] Dead code — proxy.py builds chains directly, does not use these
    # "get_backend_chain_for_model",
    # "get_fallback_backend",
    # "get_fallback_backend_and_model",
    "sanitize_model_params",  # 新增：清理模型参数
    # [FIX 2026-03-10] Export for proxy.py capability filtering
    "_is_model_supported_by_backend",
    # [P1 迁移 2026-01-24] 从提示词提取模型
    "extract_model_from_prompt",
]

# Kiro Gateway 支持的模型列表（Claude 4.5 全家桶 + Claude Sonnet 4）
KIRO_GATEWAY_SUPPORTED_MODELS = {
    "claude-sonnet-4.5", "claude-opus-4.5", "claude-haiku-4.5", "claude-sonnet-4",
}


def get_backend_base_url(backend_config: Dict[str, Any]) -> Optional[str]:
    """
    获取后端的 base_url

    处理两种配置格式：
    1. base_url: 单个 URL（如 antigravity, copilot, kiro-gateway）
    2. base_urls: URL 列表（如 anyrouter）

    Args:
        backend_config: 后端配置字典

    Returns:
        base_url 字符串，如果都不存在则返回 None
    """
    # 优先使用 base_url（单数）
    if "base_url" in backend_config:
        return backend_config["base_url"]

    # 然后尝试 base_urls（复数），取第一个
    if "base_urls" in backend_config:
        base_urls = backend_config["base_urls"]
        if base_urls and len(base_urls) > 0:
            return base_urls[0]

    return None


def get_sorted_backends() -> List[Tuple[str, Dict[str, Any]]]:
    """
    获取按优先级排序的后端列表

    Returns:
        List[Tuple[str, dict]]: [(backend_key, backend_config), ...]
            按 priority 升序排列（数字越小优先级越高）
    """
    enabled_backends = [
        (k, v) for k, v in BACKENDS.items()
        if v.get("enabled", True)
    ]
    return sorted(enabled_backends, key=lambda x: x[1]["priority"])


def get_backend_for_model(model: str) -> Optional[str]:
    """
    根据模型名称获取指定后端

    路由策略（按优先级）：
    0. 检查是否有模型特定路由规则（model_routing 配置，优先级最高）
    1. 模型特定硬编码规则（Sonnet 4.5 -> Kiro, Opus 4.5 -> Antigravity）
    2. 检查是否配置了 Kiro Gateway 路由（环境变量）
    3. 检查是否在 Antigravity 支持列表中
    4. 检查 Ruoli API 支持（公益站）
    5. 不支持 -> Copilot（按次计费，但支持更多模型）

    Args:
        model: 模型名称

    Returns:
        后端标识 ("gcli2api-antigravity", "copilot", "kiro-gateway", "ruoli") 或 None
    
    作者: 浮浮酱 (Claude Sonnet 4.5)
    更新: 2026-01-24 - 修复路由逻辑，与旧网关保持一致
    """
    # ✅ [FIX 2026-01-24] 调用统一路由函数，确保硬编码规则生效
    backend, _ = get_backend_and_model_for_routing(model)
    return backend


def get_backend_and_model_for_routing(model: str) -> Tuple[Optional[str], str]:
    """
    根据模型名称获取指定后端和目标模型

    这是 get_backend_for_model 的增强版本，返回后端名称和目标模型。
    当配置了模型级别的降级（如 claude-sonnet-4.5 -> gemini-3-pro）时，
    目标模型可能与请求模型不同。

    Args:
        model: 请求的模型名称

    Returns:
        Tuple[Optional[str], str]: (后端名称, 目标模型名称)
        - 如果没有找到后端，返回 (None, model)
        - 如果配置了模型映射，返回 (backend, target_model)
    """
    if not model:
        model = ""

    model_lower = model.lower()

    # 0. 优先检查模型特定路由规则（来自 gateway.yaml）
    routing_rule = get_model_routing_rule(model)
    if routing_rule and routing_rule.enabled and routing_rule.backend_chain:
        first_entry = routing_rule.get_first_backend()
        if first_entry:
            # 检查第一个后端是否启用
            backend_config = BACKENDS.get(first_entry.backend, {})
            if backend_config.get("enabled", True):
                if hasattr(log, 'route'):
                    log.route(
                        f"Model {model} -> {first_entry.backend}({first_entry.model}) "
                        f"(model_routing rule)",
                        tag="GATEWAY"
                    )
                return first_entry.backend, first_entry.model
            else:
                # 第一个后端未启用，尝试下一个
                for entry in routing_rule.backend_chain[1:]:
                    backend_config = BACKENDS.get(entry.backend, {})
                    if backend_config.get("enabled", True):
                        if hasattr(log, 'route'):
                            log.route(
                                f"Model {model} -> {entry.backend}({entry.model}) "
                                f"(model_routing fallback, first backend disabled)",
                                tag="GATEWAY"
                            )
                        return entry.backend, entry.model

    # ==================== YAML-driven routing ====================
    # [REFACTOR 2026-03-14] Legacy Steps 1-5 deleted — YAML-only routing now

    # Step 1: default_routing pattern matching rules from gateway.yaml
    default_rule = get_default_routing_rule(model)
    if default_rule:
        for entry in default_rule.chain:
            backend_config = BACKENDS.get(entry.backend, {})
            if backend_config.get("enabled", True) and _is_model_supported_by_backend(entry.backend, model):
                if hasattr(log, 'route'):
                    log.route(
                        f"Model {model} -> {entry.backend} "
                        f"(default_routing pattern: {default_rule.pattern})",
                        tag="GATEWAY"
                    )
                return entry.backend, model

    # Step 2: catch_all fallback
    # [FIX 2026-03-10] Two-pass: prefer backends that support this model
    catch_all = get_catch_all_routing()
    if catch_all:
        # Pass 1: prefer backends that claim support for this model
        for entry in catch_all.chain:
            backend_config = BACKENDS.get(entry.backend, {})
            if backend_config.get("enabled", True) and _is_model_supported_by_backend(entry.backend, model):
                if hasattr(log, 'route'):
                    log.route(
                        f"Model {model} -> {entry.backend} (catch_all, capability match)",
                        tag="GATEWAY"
                    )
                return entry.backend, model
        # Pass 2: no backend claims support — fall back to first enabled
        for entry in catch_all.chain:
            backend_config = BACKENDS.get(entry.backend, {})
            if backend_config.get("enabled", True):
                if hasattr(log, 'route'):
                    log.route(
                        f"Model {model} -> {entry.backend} (catch_all, no capability match)",
                        tag="GATEWAY"
                    )
                return entry.backend, model

    # [FIX 2026-03-14] Absolute fallback — respect final_fallback config instead of hardcoding "copilot"
    final_fb = get_final_fallback()
    fb_backend = final_fb.backend if (final_fb and final_fb.enabled) else "copilot"
    if hasattr(log, 'route'):
        log.route(f"Model {model} -> {fb_backend} (absolute fallback via final_fallback config)", tag="GATEWAY")
    return fb_backend, model


def sanitize_model_params(body: Dict[str, Any], target_model: str) -> Dict[str, Any]:
    """
    ✅ [FIX 2026-01-22] 清理目标模型不支持的参数
    
    跨模型降级时，不同模型可能有不同的参数要求。
    此函数清理目标模型不支持的参数，避免请求失败。
    
    Args:
        body: 请求体字典
        target_model: 目标模型名称
    
    Returns:
        清理后的请求体
    """
    sanitized = body.copy()
    
    # Gemini 模型可能不支持某些 Claude 特有的参数
    if "gemini" in target_model.lower():
        # 移除 thinking 相关参数（Gemini 不支持）
        if "thinking" in sanitized:
            log.debug(f"[FALLBACK] 移除 thinking 参数（Gemini 不支持）", tag="GATEWAY")
            del sanitized["thinking"]
        
        # 调整 max_tokens 范围（Gemini 3 Pro 支持 65536，2.5 系列支持 32768）
        if "max_tokens" in sanitized:
            max_tokens = sanitized["max_tokens"]
            if isinstance(max_tokens, int) and max_tokens > 65536:
                log.debug(f"[FALLBACK] 调整 max_tokens: {max_tokens} -> 65536 (Gemini 限制)", tag="GATEWAY")
                sanitized["max_tokens"] = 65536
        
        # 清理 messages 中的 thinking 块（如果存在）
        if "messages" in sanitized and isinstance(sanitized["messages"], list):
            for msg in sanitized["messages"]:
                if isinstance(msg, dict) and "content" in msg:
                    content = msg["content"]
                    if isinstance(content, list):
                        # 移除 thinking 类型的 content 块
                        msg["content"] = [
                            block for block in content
                            if not (isinstance(block, dict) and block.get("type") == "thinking")
                        ]
    
    # Claude 模型降级到其他 Claude 模型时，通常不需要清理
    # 但可以在这里添加其他模型的特殊处理
    
    return sanitized


def calculate_retry_delay(
    attempt: int,
    config: Dict[str, Any] = None,
    status_code: Optional[int] = None,
    retry_after_header: Optional[str] = None,
    error_body: str = "",
    account_id: Optional[str] = None,
    model: Optional[str] = None,
    backend_limit_id: Optional[str] = None
) -> float:
    """
    计算重试延迟时间（智能退避 + Retry-After 解析）

    [FIX 2026-01-23] 参考三个官方仓库的最佳实践：
    - 优先使用 Retry-After 头或错误响应中的精确延迟
    - 根据限流原因应用智能退避策略
    - 支持模型级别限流

    Args:
        attempt: 当前重试次数（从0开始）
        config: 重试配置
        status_code: HTTP 状态码（用于限流处理）
        retry_after_header: Retry-After 头值
        error_body: 错误响应 body（用于解析限流信息）
        account_id: 账号标识（用于限流跟踪）
        model: 模型名称（用于模型级别限流）
        backend_limit_id: 后端级别限流标识（用于 5xx 软避让）

    Returns:
        延迟时间（秒）
    """
    if config is None:
        config = RETRY_CONFIG

    # 1. 优先使用 Retry-After 头或错误响应中的精确延迟
    if status_code in (429, 500, 503, 529) and (retry_after_header or error_body):
        from .rate_limit_handler import parse_rate_limit_from_response
        
        # 构建 headers 字典
        headers = {}
        if retry_after_header:
            headers["Retry-After"] = retry_after_header
        
        # 解析限流信息
        limit_id = account_id
        if status_code in (500, 503, 529) and backend_limit_id:
            limit_id = backend_limit_id

        if limit_id:
            rate_limit_info = parse_rate_limit_from_response(
                account_id=limit_id,
                status_code=status_code,
                headers=headers,
                error_body=error_body,
                model=model
            )
            
            if rate_limit_info:
                # 使用解析出的精确延迟
                return rate_limit_info.retry_after_sec
        
        # 如果没有 account_id，尝试从 error_body 解析
        if error_body:
            from akarins_gateway.core.retry_utils import parse_retry_delay_seconds
            delay_sec = parse_retry_delay_seconds(error_body)
            if delay_sec is not None:
                return max(2.0, delay_sec)  # 最小 2 秒
    
    # 2. 使用 Retry-After 头（如果提供）
    if retry_after_header:
        try:
            delay_sec = float(retry_after_header)
            return max(2.0, delay_sec)  # 最小 2 秒
        except ValueError:
            pass
    
    # 3. 默认指数退避
    base_delay = config.get("base_delay", 1.0)
    max_delay = config.get("max_delay", 10.0)
    exponential_base = config.get("exponential_base", 2)

    delay = base_delay * (exponential_base ** attempt)
    return min(delay, max_delay)


def should_retry(
    status_code: int,
    attempt: int,
    max_retries: int,
    error_body: str = "",
    account_id: Optional[str] = None,
    backend_limit_id: Optional[str] = None
) -> bool:
    """
    判断是否应该重试

    [FIX 2026-01-23] 改进：
    - 支持 429 错误重试（但排除配额耗尽的情况）
    - 支持 5xx 错误重试
    - 检查限流状态，避免在限流期间重试

    Args:
        status_code: HTTP 状态码
        attempt: 当前重试次数
        max_retries: 最大重试次数
        error_body: 错误响应 body（用于判断配额耗尽）
        account_id: 账号标识（用于检查限流状态）
        backend_limit_id: 后端级别限流标识（用于检查软避让状态）

    Returns:
        是否应该重试
    """
    if attempt >= max_retries:
        return False

    # ✅ [FIX 2026-02-12] 400 签名错误：可以重试（固定 200ms 延迟）
    # Ported from upstream handlers/common.rs determine_retry_strategy()
    if status_code == 400:
        from .thinking_recovery import is_signature_400_error
        if is_signature_400_error(status_code, error_body):
            log.info(
                f"[RETRY] 检测到 400 签名错误，允许重试 "
                f"(attempt={attempt}/{max_retries})"
            )
            return True
        # Other 400 errors are NOT retryable
        return False

    # 检查限流状态
    if account_id:
        from .rate_limit_handler import get_rate_limit_tracker
        tracker = get_rate_limit_tracker()
        if tracker.is_rate_limited(account_id):
            remaining = tracker.get_reset_seconds(account_id)
            if remaining:
                log.warning(f"[RETRY] 账号 {account_id} 仍在限流中，剩余 {remaining:.1f} 秒，跳过重试")
                return False
    if backend_limit_id:
        from .rate_limit_handler import get_rate_limit_tracker
        tracker = get_rate_limit_tracker()
        if tracker.is_rate_limited(backend_limit_id):
            remaining = tracker.get_reset_seconds(backend_limit_id)
            if remaining:
                log.warning(f"[RETRY] 后端 {backend_limit_id} 软避让中，剩余 {remaining:.1f} 秒，跳过重试")
                return False

    # 429 错误：检查是否是配额耗尽
    if status_code == 429:
        # 检查是否是配额耗尽（不应该重试）
        error_lower = error_body.lower()
        quota_exhausted_keywords = [
            'quota exhausted',
            'quota_exhausted',
            'account quota',
            'billing quota',
            'no capacity available',
        ]
        
        # 排除临时速率限制关键词
        rate_limit_keywords = [
            'rate limit',
            'too many requests',
            'per minute',
        ]
        
        # 如果包含速率限制关键词，不是配额耗尽，可以重试
        if any(kw in error_lower for kw in rate_limit_keywords):
            return True
        
        # 如果包含配额耗尽关键词，不应该重试
        if any(kw in error_lower for kw in quota_exhausted_keywords):
            log.warning("[RETRY] 检测到配额耗尽，不重试")
            return False
        
        # 其他 429 错误可以重试
        return True

    # 5xx 错误：可以重试
    retry_on_status = RETRY_CONFIG.get("retry_on_status", [500, 502, 503, 504, 529])
    return status_code in retry_on_status


async def check_backend_health(backend_key: str) -> bool:
    """
    检查后端服务健康状态

    Args:
        backend_key: 后端标识

    Returns:
        是否健康
    """
    backend = BACKENDS.get(backend_key)
    if not backend or not backend.get("enabled", True):
        return False

    try:
        # 延迟导入 http_client，避免循环依赖
        from akarins_gateway.core.httpx_client import http_client
        
        # 获取 base_url（处理 base_url 和 base_urls 两种情况）
        base_url = get_backend_base_url(backend)
        if not base_url:
            return False
        
        async with http_client.get_client(timeout=5.0) as client:
            response = await client.get(f"{base_url}/models")
            return response.status_code == 200
    except Exception as e:
        log.warning(f"Backend {backend_key} health check failed: {e}", tag="GATEWAY")
        return False


def get_backend_config(backend_key: str) -> Optional[Dict[str, Any]]:
    """
    获取后端配置

    Args:
        backend_key: 后端标识

    Returns:
        后端配置字典或 None
    """
    return BACKENDS.get(backend_key)


def is_backend_enabled(backend_key: str) -> bool:
    """
    检查后端是否启用

    Args:
        backend_key: 后端标识

    Returns:
        是否启用
    """
    backend = BACKENDS.get(backend_key)
    if not backend:
        return False
    return backend.get("enabled", True)


def is_kiro_gateway_supported(model: str) -> bool:
    """
    [DEPRECATED] 检查模型是否被 Kiro Gateway 支持

    ⚠️ 此函数已废弃，将在 Phase B-3 中删除。
    请使用 config_loader.is_backend_capable("kiro-gateway", model) 替代。
    当前仅在 legacy 路径和外部模块 (nodes_bridge, kiro.py) 中保留。

    Kiro Gateway 支持的模型：
    - claude-sonnet-4.5 (含 thinking 变体)
    - claude-opus-4.5 (含 thinking 变体)
    - claude-haiku-4.5
    - claude-sonnet-4

    Args:
        model: 模型名称

    Returns:
        是否被 Kiro Gateway 支持
    """
    if not model:
        return False

    model_lower = model.lower()

    # 必须是 Claude 模型
    if "claude" not in model_lower:
        return False

    # 规范化模型名称（移除 -thinking 等后缀）
    normalized = normalize_model_name(model)

    # 精确匹配
    if normalized in KIRO_GATEWAY_SUPPORTED_MODELS:
        return True

    # 模糊匹配 Claude 4.5 系列
    if "claude" in normalized:
        # 检查版本号 4.5 / 4-5
        has_45 = bool(re.search(r'4[.\-]5', normalized))
        has_sonnet = "sonnet" in normalized
        has_opus = "opus" in normalized
        has_haiku = "haiku" in normalized

        if has_45 and (has_sonnet or has_opus or has_haiku):
            return True

        # 检查 claude-sonnet-4（不是 4.5）
        has_4 = bool(re.search(r'sonnet[.\-]?4(?![.\-]5)', normalized)) or \
                bool(re.search(r'4[.\-]?sonnet(?![.\-]5)', normalized))
        if has_4 and has_sonnet:
            return True

    return False


# ==================== [REFACTOR 2026-02-14] Public Station Support Checks ====================
# All public station model support checks are now delegated to PublicStationManager.
# The old RUOLI_SUPPORTED_MODELS, NEWAPI_PUBLIC_SUPPORTED_MODELS, ANYROUTER_SUPPORTED_MODELS
# constants and their check functions are replaced by thin wrappers for backward compatibility.

# Kept for backward compatibility (imported by other modules via __all__)
RUOLI_SUPPORTED_MODELS = _get_psm().get("ruoli").config.supported_models if _get_psm().get("ruoli") else set()
NEWAPI_PUBLIC_SUPPORTED_MODELS = _get_psm().get("dkapi").config.supported_models if _get_psm().get("dkapi") else set()
ANYROUTER_SUPPORTED_MODELS = _get_psm().get("anyrouter").config.supported_models if _get_psm().get("anyrouter") else set()


def is_ruoli_supported(model: str) -> bool:
    """Check if model is supported by Ruoli API (delegated to PublicStationManager)."""
    return _get_psm().supports_model("ruoli", model)


def is_newapi_public_supported(model: str) -> bool:
    """Check if model is supported by newAPI stations like dkapi/cifang (delegated to PublicStationManager)."""
    return _get_psm().supports_model("dkapi", model)


def is_anyrouter_supported(model: str) -> bool:
    """Check if model is supported by AnyRouter (delegated to PublicStationManager)."""
    return _get_psm().supports_model("anyrouter", model)


# ==================== 模型路由链函数（新增） ====================

def get_backend_chain_for_model(model: str) -> List[str]:
    """
    [DEPRECATED - DEAD CODE] 获取模型的后端优先级链

    ⚠️ proxy.py 使用自己的链构建逻辑（model_routing → default_routing → catch_all → global priority），
    不再调用此函数。保留仅为向后兼容，将在未来版本删除。
    请使用 proxy.py 的 route_request_with_fallback() 中的链构建逻辑。

    Args:
        model: 模型名称

    Returns:
        后端名称列表，按优先级排序

    Example:
        >>> get_backend_chain_for_model("claude-sonnet-4.5")
        ['kiro-gateway', 'antigravity']
        >>> get_backend_chain_for_model("gpt-4o")
        ['copilot']
    """
    routing_rule = get_model_routing_rule(model)
    if routing_rule and routing_rule.enabled and routing_rule.backends:
        # 过滤掉未启用的后端
        enabled_backends = []
        for backend in routing_rule.backends:
            backend_config = BACKENDS.get(backend, {})
            if backend_config.get("enabled", True):
                enabled_backends.append(backend)
        if enabled_backends:
            return enabled_backends

    # 没有特定规则，返回默认后端
    default_backend = get_backend_for_model(model)
    return [default_backend] if default_backend else []


def get_fallback_backend(
    model: str,
    current_backend: str,
    status_code: int = None,
    error_type: str = None,
    visited_backends: Optional[set] = None  # ✅ [FIX 2026-01-22] 防止循环降级
) -> Optional[str]:
    """
    [DEPRECATED - DEAD CODE] 获取降级后端

    ⚠️ proxy.py 在 for 循环中直接迭代 backend_chain 并使用 active_fallback_on 控制降级，
    不再调用此函数。保留仅为向后兼容，将在未来版本删除。

    Args:
        model: 模型名称
        current_backend: 当前失败的后端
        status_code: HTTP 状态码（如 429, 503）
        error_type: 错误类型（timeout, connection_error, unavailable）
        visited_backends: 已访问的后端集合（用于防止循环降级）

    Returns:
        下一个后端名称，如果没有可用的降级后端则返回 None

    Example:
        >>> get_fallback_backend("claude-sonnet-4.5", "kiro-gateway", status_code=429)
        'gcli2api-antigravity'
        >>> get_fallback_backend("claude-sonnet-4.5", "gcli2api-antigravity", status_code=429)
        None  # 已经是最后一个后端
    """
    # ✅ [FIX 2026-02-03] 改为迭代实现，防止大量后端时栈溢出
    # 最大降级深度限制
    MAX_FALLBACK_DEPTH = 20

    if visited_backends is None:
        visited_backends = set()

    routing_rule = get_model_routing_rule(model)
    if not routing_rule or not routing_rule.enabled:
        return None

    # 检查是否应该降级
    if not routing_rule.should_fallback(status_code, error_type):
        if hasattr(log, 'debug'):
            log.debug(
                f"No fallback for {model}: status={status_code}, error={error_type} not in fallback_on",
                tag="GATEWAY"
            )
        return None

    # ✅ [FIX 2026-02-03] 迭代查找下一个启用的后端
    current = current_backend
    depth = 0

    while depth < MAX_FALLBACK_DEPTH:
        depth += 1

        # 防止循环降级
        if current in visited_backends:
            if hasattr(log, 'error'):
                log.error(
                    f"[FALLBACK] 检测到循环降级: {current} 已在访问链中 "
                    f"(visited: {visited_backends})",
                    tag="GATEWAY"
                )
            return None

        visited_backends.add(current)

        # 获取下一个后端
        next_backend = routing_rule.get_next_backend(current)
        if not next_backend:
            return None

        # 检查下一个后端是否已在访问链中
        if next_backend in visited_backends:
            if hasattr(log, 'error'):
                log.error(
                    f"[FALLBACK] 下一个后端 {next_backend} 已在访问链中，避免循环",
                    tag="GATEWAY"
                )
            return None

        # 检查下一个后端是否启用
        backend_config = BACKENDS.get(next_backend, {})
        if backend_config.get("enabled", True):
            if hasattr(log, 'route'):
                log.route(
                    f"Fallback: {model} {current_backend} -> {next_backend} "
                    f"(status={status_code}, error={error_type})",
                    tag="GATEWAY"
                )
            return next_backend

        # 后端未启用，继续迭代到下一个
        current = next_backend

    # 超过最大深度限制
    if hasattr(log, 'error'):
        log.error(
            f"[FALLBACK] 超过最大降级深度 {MAX_FALLBACK_DEPTH}，停止降级",
            tag="GATEWAY"
        )
    return None


def get_fallback_backend_and_model(
    model: str,
    current_backend: str,
    status_code: int = None,
    error_type: str = None,
    visited_backends: Optional[set] = None  # ✅ [FIX 2026-01-22] 防止循环降级
) -> Optional[Tuple[str, str]]:
    """
    [DEPRECATED - DEAD CODE] 获取降级后端和目标模型

    ⚠️ proxy.py 在 for 循环中直接迭代 backend_chain 并使用 active_fallback_on 控制降级，
    不再调用此函数。保留仅为向后兼容，将在未来版本删除。

    Args:
        model: 原始请求的模型名称
        current_backend: 当前失败的后端
        status_code: HTTP 状态码（如 429, 503）
        error_type: 错误类型（timeout, connection_error, unavailable）
        visited_backends: 已访问的后端集合（用于防止循环降级）

    Returns:
        Tuple[str, str]: (下一个后端名称, 目标模型名称)
        如果没有可用的降级后端则返回 None
    """
    # ✅ [FIX 2026-02-03] 改为迭代实现，防止大量后端时栈溢出
    # 最大降级深度限制
    MAX_FALLBACK_DEPTH = 20

    if visited_backends is None:
        visited_backends = set()

    routing_rule = get_model_routing_rule(model)
    if not routing_rule or not routing_rule.enabled:
        if hasattr(log, 'debug'):
            log.debug(
                f"No routing rule or rule disabled for {model}, skipping fallback",
                tag="GATEWAY"
            )
        return None

    # 检查是否应该降级
    if not routing_rule.should_fallback(status_code, error_type):
        if hasattr(log, 'debug'):
            log.debug(
                f"No fallback for {model}: status={status_code}, error={error_type} not in fallback_on "
                f"(fallback_on={routing_rule.fallback_on})",
                tag="GATEWAY"
            )
        return None

    # ✅ [FIX 2026-02-03] 迭代查找下一个启用的后端
    current = current_backend
    depth = 0

    while depth < MAX_FALLBACK_DEPTH:
        depth += 1

        # 防止循环降级
        if current in visited_backends:
            if hasattr(log, 'error'):
                log.error(
                    f"[FALLBACK] 检测到循环降级: {current} 已在访问链中 "
                    f"(visited: {visited_backends})",
                    tag="GATEWAY"
                )
            return None

        visited_backends.add(current)

        # 获取下一个后端条目
        next_entry = routing_rule.get_next_backend_entry(current)
        if not next_entry:
            return None

        # 检查下一个后端是否已在访问链中
        if next_entry.backend in visited_backends:
            if hasattr(log, 'error'):
                log.error(
                    f"[FALLBACK] 下一个后端 {next_entry.backend} 已在访问链中，避免循环",
                    tag="GATEWAY"
                )
            return None

        # 检查下一个后端是否启用
        backend_config = BACKENDS.get(next_entry.backend, {})
        if backend_config.get("enabled", True):
            if hasattr(log, 'route'):
                log.route(
                    f"Fallback: {model} {current_backend} -> {next_entry.backend}({next_entry.model}) "
                    f"(status={status_code}, error={error_type})",
                    tag="GATEWAY"
                )
            return next_entry.backend, next_entry.model

        # 后端未启用，继续迭代到下一个
        current = next_entry.backend

    # 超过最大深度限制
    if hasattr(log, 'error'):
        log.error(
            f"[FALLBACK] 超过最大降级深度 {MAX_FALLBACK_DEPTH}，停止降级",
            tag="GATEWAY"
        )
    return None




# ==================== 从提示词提取模型 ====================
# [P1 迁移 2026-01-24] 从旧网关迁移，支持动态模型切换

def extract_model_from_prompt(messages: list) -> tuple:
    """
    从消息中提取模型标记，支持动态模型切换。
    
    该函数从旧网关迁移，支持两种模型标记格式：
    1. [use:model-name] - 高优先级
    2. @model-name - 低优先级

    Args:
        messages: 消息列表，包含 'role' 和 'content' 字段

    Returns:
        Tuple[Optional[str], List[Dict]]: (提取的模型名称 or None, 清理后的消息列表)
    
    Example:
        >>> messages = [{"role": "user", "content": "[use:gpt-4o] Hello"}]
        >>> model, cleaned = extract_model_from_prompt(messages)
        >>> print(model)  # "gpt-4o"
        >>> print(cleaned[0]["content"])  # "Hello"
    """
    if not messages:
        return None, messages

    extracted_model = None
    cleaned_messages = []

    for msg in messages:
        if not isinstance(msg, dict):
            cleaned_messages.append(msg)
            continue

        content = msg.get("content", "")

        # 处理不同的内容类型
        if isinstance(content, list):
            # 多模态内容（文本 + 图片）
            new_content = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    model, cleaned_text = _extract_and_clean(text, extracted_model)
                    if model:
                        extracted_model = model
                    new_content.append({**item, "text": cleaned_text})
                else:
                    new_content.append(item)
            cleaned_messages.append({**msg, "content": new_content})
        elif isinstance(content, str):
            model, cleaned_content = _extract_and_clean(content, extracted_model)
            if model:
                extracted_model = model
            cleaned_messages.append({**msg, "content": cleaned_content})
        else:
            cleaned_messages.append(msg)

    if extracted_model:
        log.info(f"从提示词中提取模型: {extracted_model}", tag="GATEWAY")

    return extracted_model, cleaned_messages


def _extract_and_clean(text: str, current_model: str = None) -> tuple:
    """
    从文本中提取模型标记并返回清理后的文本
    
    内部辅助函数，支持 extract_model_from_prompt()。

    Args:
        text: 待搜索的文本
        current_model: 当前已提取的模型（用于优先级判断）

    Returns:
        Tuple[Optional[str], str]: (模型名称 or None, 清理后的文本)
    """
    from .config import USE_PATTERN, AT_PATTERN, ROUTABLE_MODELS
    
    extracted_model = current_model
    cleaned_text = text

    # 优先级1: [use:model-name]
    use_match = USE_PATTERN.search(text)
    if use_match:
        model_name = use_match.group(1).lower()
        if model_name in ROUTABLE_MODELS or _fuzzy_match_model(model_name):
            extracted_model = model_name
            # 从文本中移除标记
            cleaned_text = USE_PATTERN.sub('', cleaned_text).strip()

    # 优先级2: @model-name (仅在没有找到 [use:] 时)
    if not use_match:
        at_match = AT_PATTERN.search(text)
        if at_match:
            model_name = at_match.group(1).lower()
            if model_name in ROUTABLE_MODELS or _fuzzy_match_model(model_name):
                extracted_model = model_name
                # 从文本中移除标记
                cleaned_text = AT_PATTERN.sub(' ', cleaned_text).strip()

    return extracted_model, cleaned_text


def _fuzzy_match_model(model_name: str) -> bool:
    """
    模糊匹配模型名称到已知模式
    
    允许变体如 'gpt4o' -> 'gpt-4o', 'claude35' -> 'claude-3.5'
    
    内部辅助函数，支持 extract_model_from_prompt()。

    Args:
        model_name: 待匹配的模型名称

    Returns:
        是否匹配到已知模型
    """
    from .config import ROUTABLE_MODELS
    
    # 规范化：移除短横线、点和下划线进行比较
    normalized = model_name.replace('-', '').replace('.', '').replace('_', '')

    for known_model in ROUTABLE_MODELS:
        known_normalized = known_model.replace('-', '').replace('.', '').replace('_', '')
        if normalized == known_normalized:
            return True

    # 检查模型家族前缀
    model_prefixes = ['gpt', 'claude', 'gemini', 'o1', 'o3']
    for prefix in model_prefixes:
        if normalized.startswith(prefix):
            return True

    return False
