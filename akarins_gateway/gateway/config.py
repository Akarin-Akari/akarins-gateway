"""
Gateway 配置模块

包含后端配置、模型路由配置、重试配置等。

从 unified_gateway_router.py 抽取的配置常量和辅助函数。

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-01-18
"""

from typing import Dict, Any, Set, List, Tuple
import os
import re

# [REFACTOR 2026-02-14] Public station module — unified abstraction
from akarins_gateway.gateway.backends.public_station import get_public_station_manager as _get_psm

__all__ = [
    # 后端配置
    "BACKENDS",
    "KIRO_GATEWAY_MODELS",
    "KIRO_GATEWAY_MODELS_ENV",
    # 重试配置
    "RETRY_CONFIG",
    # 模型路由
    "ROUTABLE_MODELS",
    "USE_PATTERN",
    "AT_PATTERN",
    "ANTIGRAVITY_SUPPORTED_PATTERNS",
    "COPILOT_MODEL_MAPPING",
    # 功能开关
    "BUGMENT_TOOL_RESULT_SHORTCIRCUIT_ENABLED",
    # 辅助函数
    "extract_model_from_prompt",
    "normalize_model_name",
    "is_antigravity_supported",
    "map_model_for_copilot",
    # AnyRouter 辅助函数
    "get_anyrouter_endpoint",
    "rotate_anyrouter_endpoint",
    "get_anyrouter_all_endpoints",
    # 模型路由规则（新增）
    "MODEL_ROUTING",
    "get_model_routing_rule",
    "reload_model_routing_config",
    "ModelRoutingRule",
    "BackendEntry",
]


# ==================== 功能开关 ====================

BUGMENT_TOOL_RESULT_SHORTCIRCUIT_ENABLED = os.getenv(
    "BUGMENT_TOOL_RESULT_SHORTCIRCUIT", ""
).strip().lower() in ("1", "true", "yes")


# ==================== 重试配置 ====================

RETRY_CONFIG: Dict[str, Any] = {
    "max_retries": 3,           # 最大重试次数
    "base_delay": 1.0,          # 基础延迟（秒）
    "max_delay": 1800.0,        # 最大延迟（秒）- 对齐 cliproxy: 30 分钟
    "exponential_base": 2,      # 指数退避基数
    # [FIX 2026-01-23] 支持 429 和 5xx 错误重试
    # 注意：429 重试会检查是否是配额耗尽，配额耗尽时不重试
    "retry_on_status": [429, 500, 502, 503, 504, 529],  # 需要重试的状态码
}


# ==================== 后端服务配置 ====================

# [REFACTOR 2026-02-14] AnyRouter URL/Key rotation is now managed by PublicStationManager.
# Legacy variables kept for backward compatibility with get_anyrouter_endpoint() function.
_ANYROUTER_BASE_URLS_SOURCE = os.getenv("ANYROUTER_BASE_URLS", "").strip()
if not _ANYROUTER_BASE_URLS_SOURCE:
    _ANYROUTER_BASE_URLS_SOURCE = os.getenv("ANYROUTER_ENDPOINT", "").strip()
if not _ANYROUTER_BASE_URLS_SOURCE:
    _ANYROUTER_BASE_URLS_SOURCE = (
        "https://a-ocnfniawgw.cn-shanghai.fcapp.run,https://anyrouter.top"
    )

_ANYROUTER_API_KEYS_SOURCE = os.getenv("ANYROUTER_API_KEYS", "").strip()
if not _ANYROUTER_API_KEYS_SOURCE:
    _ANYROUTER_API_KEYS_SOURCE = os.getenv("ANYROUTER_API_KEY", "").strip()
if not _ANYROUTER_API_KEYS_SOURCE:
    _ANYROUTER_API_KEYS_SOURCE = (
        "sk-be7LKJwag3qXSRL77tVbxUsIHEi71UfAVOvqjGI13BJiXGD5"
    )

BACKENDS: Dict[str, Dict[str, Any]] = {
    # ==================== [NEW 2026-02-20] ZeroGravity MITM Proxy ====================
    # ZeroGravity: Rust-based MITM proxy using official LS binary
    # Highest priority — traffic fingerprint indistinguishable from real Antigravity
    "zerogravity": {
        "name": "ZeroGravity",
        "base_url": os.getenv("ZEROGRAVITY_BASE_URL", "http://127.0.0.1:8880/v1"),
        "priority": 0,  # 最高优先级：流量指纹不可区分
        "timeout": float(os.getenv("ZEROGRAVITY_TIMEOUT", "120.0")),
        "stream_timeout": float(os.getenv("ZEROGRAVITY_STREAM_TIMEOUT", "600.0")),
        "max_retries": int(os.getenv("ZEROGRAVITY_MAX_RETRIES", "2")),
        "enabled": os.getenv("ZEROGRAVITY_ENABLED", "true").lower() in ("true", "1", "yes"),
        "api_format": "openai",  # ZG exposes OpenAI-compatible API
        # [NEW 2026-02-25] Circuit breaker threshold (local service, default strictness)
        "circuit_breaker_threshold": 5,
        "supported_models": [
            # Claude 4.6 series
            "claude-opus-4.6", "claude-opus-4-6",
            "claude-sonnet-4.6", "claude-sonnet-4-6",
            # Gemini 3.1 Pro series (upstream v1.2.0 — replaces sunset gemini-3-pro)
            "gemini-3.1-pro", "gemini-3.1-pro-high", "gemini-3.1-pro-low",
            # Gemini 3 Flash
            "gemini-3-flash",
        ],
    },
    # ===========================================================================
    "gcli2api-antigravity": {
        "name": "Antigravity",
        # [FIX 2026-02-28] Cluster mode: gcli2api runs on 7862 as antigravity backend
        "base_url": os.getenv("ANTIGRAVITY_BASE_URL", "http://127.0.0.1:7862/antigravity/v1"),
        "priority": 1,  # 数字越小优先级越高
        "timeout": 60.0,  # 普通请求超时
        "stream_timeout": 300.0,  # 流式请求超时（5分钟）
        "max_retries": 2,  # 最大重试次数
        # [FIX 2026-02-28] Default enabled for cluster mode; set ANTIGRAVITY_ENABLED=false for standalone
        "enabled": os.getenv("ANTIGRAVITY_ENABLED", "true").lower() in ("true", "1", "yes"),
        # [NEW 2026-02-25] Circuit breaker threshold (local service, default strictness)
        "circuit_breaker_threshold": 5,
    },
    # ==================== [NEW 2026-02-06] Antigravity Tools ====================
    # Antigravity Tools 作为自研 Antigravity 的 fallback
    # 当自研后端凭证不可用时（完整降级条件），自动降级到这里
    "antigravity-tools": {
        "name": "Antigravity Tools",
        "base_url": os.getenv("ANTIGRAVITY_TOOLS_BASE_URL", "http://127.0.0.1:9046/v1"),
        "priority": 1.5,  # 在 antigravity (1) 之后，kiro-gateway (2) 之前
        "timeout": float(os.getenv("ANTIGRAVITY_TOOLS_TIMEOUT", "120.0")),
        "stream_timeout": float(os.getenv("ANTIGRAVITY_TOOLS_STREAM_TIMEOUT", "600.0")),
        "max_retries": int(os.getenv("ANTIGRAVITY_TOOLS_MAX_RETRIES", "2")),
        "enabled": os.getenv("ANTIGRAVITY_TOOLS_ENABLED", "false").lower() in ("true", "1", "yes"),
        # Antigravity Tools 使用 Anthropic Messages 格式
        "api_format": "anthropic",
        # 特殊端点后缀（/v1/messages?beta=true）
        "endpoint_suffix": "?beta=true",
        # [NEW 2026-02-25] Circuit breaker threshold (local service, default strictness)
        "circuit_breaker_threshold": 5,
        # 支持的模型列表（与自研 antigravity 一致：Gemini + Claude 系列）
        "supported_models": [
            # Claude 4.6 系列
            "claude-opus-4.6", "claude-opus-4-6",
            # Claude 4.5 系列（主要目标）
            "claude-opus-4.5", "claude-sonnet-4.5",
            # Gemini 2.5 系列
            "gemini-2.5-pro", "gemini-2.5-flash",
            # Gemini 3 系列
            "gemini-3-pro", "gemini-3-pro-high", "gemini-3-pro-low", "gemini-3-flash",
            # GPT 系列
            "gpt-4", "gpt-4o", "gpt-4-turbo",
            "gpt-5", "gpt-5.1", "gpt-5.2",
        ],
    },
    # ===========================================================================
    "kiro-gateway": {
        "name": "Kiro Gateway",
        # Kiro Gateway 专门用于 Claude 模型的降级
        # 优先级调整为 2，次于 Antigravity
        "base_url": os.getenv("KIRO_GATEWAY_BASE_URL", "http://127.0.0.1:9876/v1"),
        "priority": 2,  # 优先级次于 Antigravity
        "timeout": float(os.getenv("KIRO_GATEWAY_TIMEOUT", "120.0")),
        "stream_timeout": float(os.getenv("KIRO_GATEWAY_STREAM_TIMEOUT", "600.0")),
        "max_retries": int(os.getenv("KIRO_GATEWAY_MAX_RETRIES", "2")),
        "enabled": os.getenv("KIRO_GATEWAY_ENABLED", "true").lower() in ("true", "1", "yes"),
        # [NEW 2026-02-25] Circuit breaker threshold (stable service, lenient)
        "circuit_breaker_threshold": 10,
        # [FIX 2026-02-26] Kiro Gateway 实际支持的模型列表（已移除不支持的 Opus 系列）
        "supported_models": [
            "claude-sonnet-4.5", "claude-sonnet-4", "claude-haiku-4.5",
            "deepseek-3.2", "minimax-m2.1", "qwen3-coder-next",
        ],
    },
    "ruoli": {
        "name": "Ruoli API",
        # [REFACTOR 2026-02-14] Ruoli config now managed by PublicStationManager
        # Kept as placeholder — will be overwritten by auto-injection below
    },
    # [DISABLED 2026-02-14] dkapi/cifang API Keys 已失效，占位条目已移除
    # 恢复时只需在 stations.py 取消注释即可，auto-injection 会自动创建 BACKENDS 条目
    "anyrouter": {
        "name": "AnyRouter",
        # [REFACTOR 2026-02-14] Managed by PublicStationManager
    },
    "copilot": {
        "name": "Copilot",
        "base_url": "http://127.0.0.1:8141/v1",
        "priority": 5,  # 优先级最低，作为最终兜底（在所有公益站之后）
        "timeout": 120.0,  # 思考模型需要更长时间
        "stream_timeout": 600.0,  # 流式请求超时（10分钟，GPT-5.2思考模型）
        "max_retries": 3,  # 最大重试次数
        "enabled": True,
        # [NEW 2026-02-25] Circuit breaker threshold (stable service, lenient)
        "circuit_breaker_threshold": 10,
    },
}

# [REFACTOR 2026-02-14] Auto-inject public station configs from PublicStationManager
# This replaces ~90 lines of hand-written public station BACKENDS entries.
# To add a new public station, edit src/gateway/backends/public_station/stations.py instead.
_get_psm().inject_into_backends(BACKENDS)


# [FIX 2026-03-14] Apply gateway.yaml overrides to BACKENDS at startup
# Without this, panel changes (priority, enabled, etc.) are lost on restart
# because config.py re-creates BACKENDS from hardcoded values each time.
def _apply_yaml_overrides() -> None:
    """
    Read gateway.yaml and merge backend settings into the runtime BACKENDS dict.

    Merge strategy:
    - For each backend in YAML:
      - If already in BACKENDS: override enabled, priority, timeout, stream_timeout, max_retries
      - If not in BACKENDS: create a new entry (supports panel-added backends)
    - Backends in BACKENDS but not in YAML: keep as-is (backward compat)
    - base_url: only override if the YAML value is NOT an env-var reference (${...})
    """
    from pathlib import Path
    try:
        import yaml as _yaml
    except ImportError:
        return  # No YAML library available, skip

    config_path = Path(__file__).parent.parent.parent / "config" / "gateway.yaml"
    if not config_path.exists():
        return

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = _yaml.safe_load(f)
    except Exception:
        return  # Silently skip on parse error

    if not isinstance(raw, dict):
        return

    yaml_backends = raw.get("backends")
    if not isinstance(yaml_backends, dict):
        return

    # Fields to merge from YAML into BACKENDS
    _MERGE_FIELDS = ("enabled", "priority", "timeout", "stream_timeout", "max_retries")

    for key, yaml_cfg in yaml_backends.items():
        if not isinstance(yaml_cfg, dict):
            continue

        if key in BACKENDS:
            # Existing backend — merge overridable fields
            target = BACKENDS[key]
            for field in _MERGE_FIELDS:
                if field in yaml_cfg:
                    target[field] = yaml_cfg[field]

            # base_url: only override if YAML value is a concrete URL (not ${...})
            yaml_url = yaml_cfg.get("base_url", "")
            if yaml_url and isinstance(yaml_url, str) and not yaml_url.startswith("${"):
                target["base_url"] = yaml_url
        else:
            # New backend from YAML (panel-added) — create entry
            from akarins_gateway.gateway.config_loader import expand_env_vars
            expanded = expand_env_vars(yaml_cfg)
            BACKENDS[key] = {
                "name": expanded.get("name", key),
                "base_url": expanded.get("base_url", ""),
                "priority": expanded.get("priority", 99),
                "timeout": expanded.get("timeout", 60.0),
                "stream_timeout": expanded.get("stream_timeout", 300.0),
                "max_retries": expanded.get("max_retries", 2),
                "enabled": expanded.get("enabled", True),
                "api_format": expanded.get("api_format", "openai"),
                "api_keys": expanded.get("api_keys", []),
                "supported_models": expanded.get("models", []),
                "scid_enabled": False,
            }


# Execute at import time — after hardcoded BACKENDS + PSM injection
_apply_yaml_overrides()


# ==================== Kiro Gateway 路由配置 ====================

# 通过环境变量 KIRO_GATEWAY_MODELS 指定哪些模型路由到 kiro-gateway
# 格式：逗号分隔的模型名称列表，例如: "gpt-4,claude-3-opus,gemini-pro"
KIRO_GATEWAY_MODELS_ENV = os.getenv("KIRO_GATEWAY_MODELS", "").strip()
KIRO_GATEWAY_MODELS: List[str] = (
    [m.strip().lower() for m in KIRO_GATEWAY_MODELS_ENV.split(",") if m.strip()]
    if KIRO_GATEWAY_MODELS_ENV
    else []
)


# ==================== AnyRouter 辅助函数 (DEPRECATED) ====================
# [DEPRECATED 2026-02-14] These functions are superseded by PublicStationManager.
# Rotation logic is now handled internally by PublicStationBackend.get_rotation_endpoint().
# Kept for backward compatibility — safe to remove once all external references are confirmed gone.

def get_anyrouter_endpoint() -> Tuple[str, str]:
    """
    获取当前 AnyRouter 的端点和 API Key

    .. deprecated:: 2026-02-14
        Use ``get_public_station_manager().get("anyrouter").get_rotation_endpoint()`` instead.

    Returns:
        Tuple[str, str]: (base_url, api_key)
    """
    config = BACKENDS.get("anyrouter", {})
    base_urls = config.get("base_urls", [])
    api_keys = config.get("api_keys", [])

    if not base_urls or not api_keys:
        return "", ""

    url_index = config.get("_current_url_index", 0) % len(base_urls)
    key_index = config.get("_current_key_index", 0) % len(api_keys)

    return base_urls[url_index], api_keys[key_index]


def rotate_anyrouter_endpoint(rotate_url: bool = True, rotate_key: bool = False) -> None:
    """
    轮换 AnyRouter 端点或 API Key

    当某个端点失败时调用此函数切换到下一个

    Args:
        rotate_url: 是否轮换端点
        rotate_key: 是否轮换 API Key
    """
    config = BACKENDS.get("anyrouter", {})
    base_urls = config.get("base_urls", [])
    api_keys = config.get("api_keys", [])

    if rotate_url and base_urls:
        current = config.get("_current_url_index", 0)
        config["_current_url_index"] = (current + 1) % len(base_urls)

    if rotate_key and api_keys:
        current = config.get("_current_key_index", 0)
        config["_current_key_index"] = (current + 1) % len(api_keys)


def get_anyrouter_all_endpoints() -> List[Tuple[str, str]]:
    """
    获取所有 AnyRouter 端点和 API Key 的组合

    用于遍历所有可能的组合进行重试

    Returns:
        List[Tuple[str, str]]: [(base_url, api_key), ...]
    """
    config = BACKENDS.get("anyrouter", {})
    base_urls = config.get("base_urls", [])
    api_keys = config.get("api_keys", [])

    if not base_urls or not api_keys:
        return []

    # 返回所有端点和 Key 的组合（端点优先轮询，Key 保持不变以维持会话）
    # 策略：先尝试所有端点用同一个 Key，失败后换 Key 再试所有端点
    combinations = []
    for key in api_keys:
        for url in base_urls:
            combinations.append((url, key))

    return combinations


# ==================== Prompt Model Routing ====================

# Supported model names for routing
ROUTABLE_MODELS: Set[str] = {
    # GPT models -> Copilot
    "gpt-4", "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
    "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
    "gpt-5", "gpt-5.1", "gpt-5.2",
    "o1", "o1-mini", "o1-pro", "o3", "o3-mini",
    # Claude models -> Antigravity
    "claude-3-opus", "claude-3-sonnet", "claude-3-haiku",
    "claude-3.5-opus", "claude-3.5-sonnet", "claude-3.5-haiku",
    "claude-sonnet-4", "claude-opus-4", "claude-haiku-4",
    "claude-sonnet-4.5", "claude-opus-4.5", "claude-haiku-4.5",
    # [FIX 2026-02-15] Claude Opus 4.6
    "claude-opus-4.6", "claude-opus-4-6",
    # Gemini models -> Antigravity
    "gemini-pro", "gemini-ultra",
    "gemini-2.5-pro", "gemini-2.5-flash",
    "gemini-3-pro", "gemini-3-pro-high", "gemini-3-pro-low", "gemini-3-flash",
}

# Regex patterns for model markers
# Pattern 1: [use:model-name] - High priority
USE_PATTERN = re.compile(r'\[use:([a-zA-Z0-9._-]+)\]', re.IGNORECASE)
# Pattern 2: @model-name - Low priority (at start of message or after whitespace)
AT_PATTERN = re.compile(r'(?:^|\s)@([a-zA-Z0-9._-]+)(?=\s|$)', re.IGNORECASE)


# ==================== Antigravity 支持的模型 ====================

ANTIGRAVITY_SUPPORTED_PATTERNS: Set[str] = {
    # ==================== [FIX 2026-01-24] Antigravity 支持范围（排除 Haiku）====================
    # Gemini 2.5 系列
    "gemini-2.5", "gemini-25", "gemini2.5", "gemini25",
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-25-pro", "gemini-25-flash",
    # Gemini 3 系列
    "gemini-3", "gemini3", "gemini-3-pro", "gemini-3-flash",
    # Claude 4.5 系列（❌ 不包括 haiku！与旧网关保持一致）
    "claude-sonnet-4.5", "claude-4.5-sonnet", "claude-45-sonnet",
    "claude-opus-4.5", "claude-4.5-opus", "claude-45-opus",
    # ✅ [FIX 2026-02-11] Claude 4.6 系列（Opus 4.6）
    "claude-opus-4.6", "claude-4.6-opus", "claude-46-opus",
    "claude-opus-4-6", "claude-4-6-opus",
    # ❌ 移除 Haiku 支持（旧网关不支持）
    # "claude-haiku-4.5", "claude-4.5-haiku", "claude-45-haiku",
    # GPT 系列
    "gpt-4", "gpt4", "gpt-4o", "gpt4o", "gpt-4-turbo",
    "gpt-5", "gpt5", "gpt-5.1", "gpt-5.2",
    "gpt-oos",
    # ====================================================================================
}


# ==================== Copilot 模型名称映射 ====================

COPILOT_MODEL_MAPPING: Dict[str, str] = {
    # Claude Haiku 系列 -> claude-haiku-4.5
    "claude-3-haiku": "claude-haiku-4.5",
    "claude-3.5-haiku": "claude-haiku-4.5",
    "claude-haiku-3": "claude-haiku-4.5",
    "claude-haiku-3.5": "claude-haiku-4.5",
    "claude-haiku": "claude-haiku-4.5",

    # Claude Sonnet 系列
    "claude-3-sonnet": "claude-sonnet-4",
    "claude-3.5-sonnet": "claude-sonnet-4",
    "claude-sonnet-3": "claude-sonnet-4",
    "claude-sonnet-3.5": "claude-sonnet-4",
    "claude-sonnet": "claude-sonnet-4",

    # Claude 4 系列
    "claude-4-sonnet": "claude-sonnet-4",
    "claude-sonnet-4": "claude-sonnet-4",
    "claude-4.5-sonnet": "claude-sonnet-4.5",
    "claude-sonnet-4.5": "claude-sonnet-4.5",

    "claude-4-opus": "claude-opus-4.5",
    "claude-opus-4": "claude-opus-4.5",
    "claude-4.5-opus": "claude-opus-4.5",
    "claude-opus-4.5": "claude-opus-4.5",

    "claude-4-haiku": "claude-haiku-4.5",
    "claude-haiku-4": "claude-haiku-4.5",
    "claude-4.5-haiku": "claude-haiku-4.5",
    "claude-haiku-4.5": "claude-haiku-4.5",

    # ✅ [FIX 2026-02-11] Claude Opus 4.6 系列（全面支持）
    "claude-4.6-opus": "claude-opus-4.6",
    "claude-4-6-opus": "claude-opus-4.6",
    "claude-opus-4.6": "claude-opus-4.6",
    "claude-opus-4-6": "claude-opus-4.6",

    # GPT 系列
    "gpt-4-turbo": "gpt-4-0125-preview",
    "gpt-4-turbo-preview": "gpt-4-0125-preview",
    "gpt-4o-latest": "gpt-4o",
    "gpt-4o-mini-latest": "gpt-4o-mini",

    # Gemini 系列（从旧网关迁移）
    "gemini-2.5-pro-latest": "gemini-2.5-pro",
    "gemini-2.5-pro-preview": "gemini-2.5-pro",
    "gemini-3-pro": "gemini-3-pro-high",
    "gemini-3-pro-preview": "gemini-3-pro-high",
    "gemini-3-flash": "gemini-3-flash",
    "gemini-3-flash-preview": "gemini-3-flash",
}


# ==================== 辅助函数 ====================

def normalize_model_name(model: str) -> str:
    """
    规范化模型名称，移除变体后缀

    Args:
        model: 原始模型名称

    Returns:
        规范化后的模型名称
    """
    model_lower = model.lower()

    # 移除常见后缀
    suffixes = [
        "-thinking", "-think", "-extended", "-preview", "-latest",
        "-high", "-low", "-medium",
        "-20241022", "-20240620", "-20250101", "-20250514",
    ]
    for suffix in suffixes:
        model_lower = model_lower.replace(suffix, "")

    # 移除日期后缀
    model_lower = re.sub(r'-\d{8}$', '', model_lower)

    return model_lower.strip("-")


def is_antigravity_supported(model: str) -> bool:
    """
    [DEPRECATED] 检查模型是否被 Antigravity 支持

    ⚠️ 此函数已废弃，将在 Phase B-3 中删除。
    请使用 config_loader.is_backend_capable("gcli2api-antigravity", model) 替代。
    当前仅在 ROUTING_USE_YAML=false 的 legacy 路径中保留。

    Antigravity 支持：
    - Gemini 2.5 系列 (gemini-2.5-pro, gemini-2.5-flash 等)
    - Gemini 3 系列 (gemini-3-pro, gemini-3-flash)
    - Claude 4.5 系列 (sonnet-4.5, opus-4.5) - ❌ 不支持 haiku-4.5！
    - GPT 4/5 系列 (gpt-4, gpt-4o, gpt-5, gpt-5.1, gpt-5.2)
    - GPT OOS 120B

    注意：Antigravity 不支持 Haiku 模型，Haiku 直接走 Kiro/Copilot

    Args:
        model: 模型名称

    Returns:
        是否被 Antigravity 支持
    
    作者: 浮浮酱 (Claude Sonnet 4.5)
    更新: 2026-01-24 - 扩展支持范围，修复降级链跳过问题
    """
    if not model:
        return False
    
    normalized = normalize_model_name(model)
    model_lower = model.lower()

    # ==================== [FIX 2026-01-24] 改进模型支持检查 ====================
    
    # 1. 优先检查精确匹配（性能优化）
    if normalized in ANTIGRAVITY_SUPPORTED_PATTERNS:
        return True
    
    # 2. 检查 Gemini - 支持 2.5 和 3 系列
    if "gemini" in model_lower:
        # Gemini 2.5 系列（最新版本）
        if any(x in normalized for x in ["gemini-2.5", "gemini-2-5", "gemini2.5", "gemini25"]):
            return True
        # Gemini 3 系列
        if any(x in normalized for x in ["gemini-3", "gemini3"]):
            return True
        # 使用正则匹配更灵活的格式
        if re.search(r'gemini[.\-_]?(2[.\-]5|25|3)', normalized):
            return True
        # 其他 Gemini 版本（2.0, 1.5 等）不支持
        return False

    # 3. 检查 Claude - 支持 4.5 系列的 sonnet, opus（❌ 不支持 haiku！）
    if "claude" in model_lower:
        # ==================== [FIX 2026-01-24] Haiku 排除 Antigravity ====================
        # Antigravity 不支持 Haiku 模型，Haiku 直接走 Kiro/Copilot
        if "haiku" in model_lower:
            return False  # ❌ 排除 Haiku
        # ====================================================================================
        
        # 检查版本号 4.5 / 4-5 / 4.6 / 4-6
        # 支持格式: claude-sonnet-4.5, claude-4.5-sonnet, claude-opus-4-6-20251101 等
        # 使用正则匹配 4.5/4-5 或 4.6/4-6 格式
        has_45 = bool(re.search(r'4[.\-_]?5', normalized))
        # ✅ [FIX 2026-02-11] 添加 4.6 版本检测
        has_46 = bool(re.search(r'4[.\-_]?6', normalized))

        # 检查模型类型（只支持 sonnet 和 opus）
        has_sonnet = "sonnet" in normalized
        has_opus = "opus" in normalized

        if (has_45 or has_46) and (has_sonnet or has_opus):
            return True

        # 其他 Claude 版本不支持
        return False

    # 4. 检查 GPT 系列 - 支持 GPT-4, GPT-5 系列
    if "gpt" in model_lower:
        # GPT-4 系列
        if re.search(r'gpt[.\-_]?4', normalized):
            return True
        # GPT-5 系列
        if re.search(r'gpt[.\-_]?5', normalized):
            return True
        # GPT OOS 120B
        if "oos" in normalized:
            return True
        # 其他 GPT 版本（gpt-3.5）不支持
        return False
    
    # 5. 检查 GPT OOS
    if "gpt-oos" in model_lower or "gptoos" in model_lower:
        return True

    # 其他模型都不支持
    return False


def map_model_for_copilot(model: str) -> str:
    """
    将模型名称映射为 Copilot API 能识别的格式
    
    该函数从旧网关迁移，包含完整的后缀移除和模型映射逻辑。

    Args:
        model: 原始模型名称

    Returns:
        Copilot 能识别的模型ID
    """
    if not model:
        return "gpt-4o"  # 默认模型

    model_lower = model.lower()

    # 移除常见后缀进行匹配
    base_model = model_lower
    for suffix in ["-thinking", "-think", "-extended", "-preview", "-latest",
                   "-20241022", "-20240620", "-20250101", "-20250514"]:
        base_model = base_model.replace(suffix, "")

    # 移除日期后缀（格式：-YYYYMMDD）
    base_model = re.sub(r'-\d{8}$', '', base_model).strip("-")

    # 1. 直接匹配原始名称
    if model_lower in COPILOT_MODEL_MAPPING:
        return COPILOT_MODEL_MAPPING[model_lower]

    # 2. 匹配去除后缀的名称
    if base_model in COPILOT_MODEL_MAPPING:
        return COPILOT_MODEL_MAPPING[base_model]

    # 3. 智能模糊匹配 Claude 模型
    if "claude" in model_lower:
        # 检测模型类型
        if "haiku" in model_lower:
            return "claude-haiku-4.5"
        elif "opus" in model_lower:
            # ✅ [FIX 2026-02-11] 优先匹配 4.6，再 fallback 到 4.5
            if "4.6" in model_lower or "4-6" in model_lower:
                return "claude-opus-4.6"
            return "claude-opus-4.5"
        elif "sonnet" in model_lower:
            # 检查版本号
            if "4.5" in model_lower or "45" in model_lower:
                return "claude-sonnet-4.5"
            else:
                return "claude-sonnet-4"
        else:
            # 默认 Claude -> sonnet
            return "claude-sonnet-4"

    # 4. 智能模糊匹配 GPT 模型
    if "gpt" in model_lower:
        if "5.2" in model_lower:
            return "gpt-5.2"
        elif "5.1" in model_lower:
            # GPT 5.1 codex 系列支持
            if "codex" in model_lower:
                if "mini" in model_lower:
                    return "gpt-5.1-codex-mini"
                elif "max" in model_lower:
                    return "gpt-5.1-codex-max"
                return "gpt-5.1-codex"
            return "gpt-5.1"
        elif "gpt-5" in model_lower or "gpt5" in model_lower:
            if "mini" in model_lower:
                return "gpt-5-mini"
            return "gpt-5"
        elif "4.1" in model_lower or "41" in model_lower:
            return "gpt-4.1"
        elif "4o-mini" in model_lower or "4o mini" in model_lower:
            return "gpt-4o-mini"
        elif "4o" in model_lower:
            return "gpt-4o"
        elif "4-turbo" in model_lower:
            return "gpt-4-0125-preview"
        elif "3.5" in model_lower:
            return "gpt-3.5-turbo"
        else:
            return "gpt-4"

    # 5. 智能模糊匹配 Gemini 模型
    if "gemini" in model_lower:
        if "3" in model_lower:
            if "flash" in model_lower:
                return "gemini-3-flash"
            return "gemini-3-pro-high"
        elif "2.5" in model_lower:
            return "gemini-2.5-pro"
        else:
            return "gemini-2.5-pro"  # 默认

    # 6. O1/O3 模型 (如果 Copilot 支持)
    if model_lower.startswith("o1") or model_lower.startswith("o3"):
        # 目前 Copilot 可能不支持，返回原名尝试
        return model

    # 7. 返回原始模型名（可能 Copilot 直接支持）
    return model


def _fuzzy_match_model(model_name: str) -> bool:
    """
    Fuzzy match model name against known patterns.
    Allows variations like 'gpt4o' -> 'gpt-4o', 'claude35' -> 'claude-3.5'

    Args:
        model_name: 模型名称

    Returns:
        是否匹配已知模型
    """
    # Normalize: remove dashes and dots for comparison
    normalized = model_name.replace('-', '').replace('.', '').replace('_', '')

    for known_model in ROUTABLE_MODELS:
        known_normalized = known_model.replace('-', '').replace('.', '').replace('_', '')
        if normalized == known_normalized:
            return True

    # Check prefixes for model families
    model_prefixes = ['gpt', 'claude', 'gemini', 'o1', 'o3']
    for prefix in model_prefixes:
        if normalized.startswith(prefix):
            return True

    return False


def _extract_and_clean(text: str, current_model: str = None) -> Tuple[str, str]:
    """
    Extract model marker from text and return cleaned text.

    Args:
        text: The text to search
        current_model: Currently extracted model (for priority)

    Returns:
        Tuple of (model_name or None, cleaned_text)
    """
    extracted_model = current_model
    cleaned_text = text

    # Priority 1: [use:model-name]
    use_match = USE_PATTERN.search(text)
    if use_match:
        model_name = use_match.group(1).lower()
        if model_name in ROUTABLE_MODELS or _fuzzy_match_model(model_name):
            extracted_model = model_name
            # Remove the marker from text
            cleaned_text = USE_PATTERN.sub('', cleaned_text).strip()

    # Priority 2: @model-name (only if no [use:] found)
    if not use_match:
        at_match = AT_PATTERN.search(text)
        if at_match:
            model_name = at_match.group(1).lower()
            if model_name in ROUTABLE_MODELS or _fuzzy_match_model(model_name):
                extracted_model = model_name
                # Remove the marker from text
                cleaned_text = AT_PATTERN.sub(' ', cleaned_text).strip()

    return extracted_model, cleaned_text


def extract_model_from_prompt(messages: list) -> Tuple[str, list]:
    """
    Extract model name from prompt markers in messages.

    Priority:
    1. [use:model-name] - Highest priority
    2. @model-name - Lower priority

    Args:
        messages: List of message dicts with 'role' and 'content'

    Returns:
        Tuple of (extracted_model_name or None, cleaned_messages)
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

        # Handle different content types
        if isinstance(content, list):
            # Multi-modal content (text + images)
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

    return extracted_model, cleaned_messages


# ==================== 模型特定路由规则 ====================

# 延迟导入，避免循环依赖
from .config_loader import (
    load_model_routing_config,
    get_model_routing_rule,
    reload_model_routing_config,
    ModelRoutingRule,
    BackendEntry,
)

# 全局模型路由配置（启动时加载）
MODEL_ROUTING: Dict[str, "ModelRoutingRule"] = {}

def _init_model_routing():
    """初始化模型路由配置"""
    global MODEL_ROUTING
    try:
        MODEL_ROUTING = load_model_routing_config()
    except Exception:
        # 配置加载失败时使用空配置
        MODEL_ROUTING = {}

# 模块加载时初始化
_init_model_routing()
