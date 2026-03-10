"""
Constants and model helper functions.

Extracted from gcli2api/src/utils.py — contains:
- OAuth configuration constants
- Antigravity version and User-Agent
- Default safety settings
- Model name helper functions
- GeminiCLI User-Agent
- Quota reset timestamp parsing
"""

import os
import re
import platform
import time
from datetime import datetime, timezone
from typing import List, Optional

from .log import log

# ====================== CLI Version ======================

CLI_VERSION = "0.1.5"  # Match current gemini-cli version

# ====================== OAuth Configuration ======================

# OAuth Configuration - 标准模式
# Production deployments should set OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET env vars.
CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Antigravity OAuth Configuration
# Production deployments should set AG_OAUTH_CLIENT_ID and AG_OAUTH_CLIENT_SECRET env vars.
ANTIGRAVITY_CLIENT_ID = os.environ.get("AG_OAUTH_CLIENT_ID", "")
ANTIGRAVITY_CLIENT_SECRET = os.environ.get("AG_OAUTH_CLIENT_SECRET", "")
ANTIGRAVITY_SCOPES = [
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/cclog',
    'https://www.googleapis.com/auth/experimentsandconfigs'
]

# 统一的 Token URL（两种模式相同）
TOKEN_URL = "https://oauth2.googleapis.com/token"

# 回调服务器配置
CALLBACK_HOST = "localhost"

# ====================== API Configuration ======================

STANDARD_USER_AGENT = "GeminiCLI/0.1.5 (Windows; AMD64)"

# Antigravity User-Agent - Chrome/Electron 格式 (对齐 Antigravity-Manager v4.1.20)
# [FIX 2026-02-17] 从简单格式升级为 Chrome/Electron 桌面应用格式
# 参考: Antigravity-Manager src-tauri/src/constants.rs
#
# 已知的稳定版本映射 (来自 Antigravity-Manager 源码):
#   Antigravity 1.16.5 → Electron 39.2.3 → Chrome 132.0.6834.160
#
# 环境变量:
#   ANTIGRAVITY_VERSION: Antigravity 版本号 (默认 1.16.5)
#   ANTIGRAVITY_UA_FORMAT: UA 格式 "electron" (默认) 或 "legacy" (旧格式回退)
#   ANTIGRAVITY_UA_PLATFORM: UA 平台 "macos" (默认) 或 "windows"

ANTIGRAVITY_VERSION = os.getenv("ANTIGRAVITY_VERSION", "1.16.5")
ANTIGRAVITY_ELECTRON_VERSION = "39.2.3"
ANTIGRAVITY_CHROME_VERSION = "132.0.6834.160"

# UA 平台模板
_UA_PLATFORM_TEMPLATES = {
    "macos": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Antigravity/{version} Chrome/{chrome} Electron/{electron} Safari/537.36",
    "windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Antigravity/{version} Chrome/{chrome} Electron/{electron} Safari/537.36",
}

# 旧格式保留 (环境变量 ANTIGRAVITY_UA_FORMAT=legacy 时使用)
ANTIGRAVITY_PLATFORM = os.getenv("ANTIGRAVITY_PLATFORM", "darwin/arm64")
_LEGACY_USER_AGENT = f"antigravity/{ANTIGRAVITY_VERSION} {ANTIGRAVITY_PLATFORM}"


def _build_electron_ua(version: str, ua_platform: str = "macos") -> str:
    """构建 Chrome/Electron 格式的 User-Agent"""
    template = _UA_PLATFORM_TEMPLATES.get(ua_platform)
    if template is None:
        log.warning(f"[UA] Unknown UA platform '{ua_platform}', falling back to 'macos'")
        template = _UA_PLATFORM_TEMPLATES["macos"]
    return template.format(
        version=version,
        chrome=ANTIGRAVITY_CHROME_VERSION,
        electron=ANTIGRAVITY_ELECTRON_VERSION,
    )


# 根据环境变量选择 UA 格式
_UA_FORMAT = os.getenv("ANTIGRAVITY_UA_FORMAT", "electron").lower()
_UA_PLATFORM = os.getenv("ANTIGRAVITY_UA_PLATFORM", "macos").lower()

if _UA_FORMAT == "legacy":
    ANTIGRAVITY_USER_AGENT = _LEGACY_USER_AGENT
else:
    ANTIGRAVITY_USER_AGENT = _build_electron_ua(ANTIGRAVITY_VERSION, _UA_PLATFORM)


def get_antigravity_user_agent() -> str:
    """
    动态获取 Antigravity User-Agent

    在版本检测模块更新环境变量后，此函数返回最新的 UA
    用于需要实时获取最新 UA 的场景

    Returns:
        Chrome/Electron 格式 UA 或旧格式 (取决于 ANTIGRAVITY_UA_FORMAT 环境变量)
    """
    version = os.getenv("ANTIGRAVITY_VERSION", "1.16.5")
    ua_format = os.getenv("ANTIGRAVITY_UA_FORMAT", "electron").lower()
    if ua_format == "legacy":
        ua_platform = os.getenv("ANTIGRAVITY_PLATFORM", "darwin/arm64")
        return f"antigravity/{version} {ua_platform}"
    ua_platform = os.getenv("ANTIGRAVITY_UA_PLATFORM", "macos").lower()
    return _build_electron_ua(version, ua_platform)


# ====================== Model Configuration ======================

# Default Safety Settings for Google API
DEFAULT_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_HATE", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_JAILBREAK", "threshold": "BLOCK_NONE"},
]

# Model name lists for different features
BASE_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview"
]


# ====================== Model Helper Functions ======================

def get_base_model_name(model_name: str) -> str:
    """Convert variant model name to base model name."""
    # Remove all possible suffixes (supports multiple suffixes in any order)
    suffixes = ["-maxthinking", "-nothinking", "-search"]
    result = model_name
    # Keep removing suffixes until no more matches
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if result.endswith(suffix):
                result = result[: -len(suffix)]
                changed = True
                break
    return result


def is_search_model(model_name: str) -> bool:
    """Check if model name indicates search grounding should be enabled."""
    return "-search" in model_name


def is_nothinking_model(model_name: str) -> bool:
    """Check if model name indicates thinking should be disabled."""
    return "-nothinking" in model_name


def is_maxthinking_model(model_name: str) -> bool:
    """Check if model name indicates maximum thinking budget should be used."""
    return "-maxthinking" in model_name


def get_thinking_budget(model_name: str) -> Optional[int]:
    """Get the appropriate thinking budget for a model based on its name and variant."""
    if is_nothinking_model(model_name):
        return 128  # Limited thinking for pro
    elif is_maxthinking_model(model_name):
        base_model = get_base_model_name(get_base_model_from_feature_model(model_name))
        if "flash" in base_model:
            return 24576
        return 32768
    else:
        # Default thinking budget for regular models
        return None  # Default for all models


def should_include_thoughts(model_name: str) -> bool:
    """Check if thoughts should be included in the response."""
    if is_nothinking_model(model_name):
        # For nothinking mode, still include thoughts if it's a pro model
        base_model = get_base_model_name(model_name)
        return "pro" in base_model
    else:
        # For all other modes, include thoughts
        return True


def is_fake_streaming_model(model_name: str) -> bool:
    """Check if model name indicates fake streaming should be used."""
    return model_name.startswith("假流式/")


def is_anti_truncation_model(model_name: str) -> bool:
    """Check if model name indicates anti-truncation should be used."""
    return model_name.startswith("流式抗截断/")


def get_base_model_from_feature_model(model_name: str) -> str:
    """Get base model name from feature model name."""
    # Remove feature prefixes
    for prefix in ["假流式/", "流式抗截断/"]:
        if model_name.startswith(prefix):
            return model_name[len(prefix):]
    return model_name


def get_available_models(router_type: str = "openai") -> List[str]:
    """
    Get available models with feature prefixes.

    Args:
        router_type: "openai" or "gemini"

    Returns:
        List of model names with feature prefixes
    """
    models = []

    for base_model in BASE_MODELS:
        # 基础模型
        models.append(base_model)

        # 假流式模型 (前缀格式)
        models.append(f"假流式/{base_model}")

        # 流式抗截断模型 (仅在流式传输时有效，前缀格式)
        models.append(f"流式抗截断/{base_model}")

        # 支持thinking模式后缀与功能前缀组合
        thinking_suffixes = ["-maxthinking", "-nothinking"]
        search_suffix = "-search"

        # 1. 单独的 thinking 后缀
        for thinking_suffix in thinking_suffixes:
            models.append(f"{base_model}{thinking_suffix}")
            models.append(f"假流式/{base_model}{thinking_suffix}")
            models.append(f"流式抗截断/{base_model}{thinking_suffix}")

        # 2. 单独的 search 后缀
        models.append(f"{base_model}{search_suffix}")
        models.append(f"假流式/{base_model}{search_suffix}")
        models.append(f"流式抗截断/{base_model}{search_suffix}")

        # 3. thinking + search 组合后缀
        for thinking_suffix in thinking_suffixes:
            combined_suffix = f"{thinking_suffix}{search_suffix}"
            models.append(f"{base_model}{combined_suffix}")
            models.append(f"假流式/{base_model}{combined_suffix}")
            models.append(f"流式抗截断/{base_model}{combined_suffix}")

    return models


def get_model_group(model_name: str) -> str:
    """
    获取模型组，用于 GCLI CD 机制。

    Args:
        model_name: 模型名称

    Returns:
        "pro" 或 "flash"
    """
    # 去除功能前缀和后缀，获取基础模型名
    base_model = get_base_model_from_feature_model(model_name)
    base_model = get_base_model_name(base_model)

    # 判断模型组
    if "flash" in base_model.lower():
        return "flash"
    else:
        return "pro"


# ====================== User Agent ======================


def get_user_agent():
    """Generate User-Agent string matching gemini-cli format."""
    version = CLI_VERSION
    system = platform.system()
    arch = platform.machine()
    return f"GeminiCLI/{version} ({system}; {arch})"


# ====================== Quota Reset Parsing ======================


def parse_quota_reset_timestamp(
    error_response: dict,
    response_headers: Optional[dict] = None,
    error_message: Optional[str] = None,
) -> Optional[float]:
    """
    从Google API错误响应中提取quota重置时间戳

    对齐 CLIProxyAPI 的完整解析策略：
    1. 优先解析 HTTP 响应头 Retry-After
    2. 解析 Google RPC RetryInfo.retryDelay（标准格式）
    3. 解析 ErrorInfo.metadata.quotaResetTimeStamp（ISO 时间戳）
    4. 解析 ErrorInfo.metadata.quotaResetDelay（duration 字符串）
    5. 从错误消息中正则提取（最后备用）

    Args:
        error_response: Google API返回的错误响应字典
        response_headers: HTTP 响应头（可选，用于解析 Retry-After）
        error_message: 错误消息文本（可选，用于正则提取）

    Returns:
        Unix时间戳（秒），如果无法解析则返回None
    """
    def _parse_duration_seconds(duration_str: str) -> Optional[float]:
        """解析 duration 字符串"""
        if not duration_str:
            return None

        total_ms = 0.0
        matched = False
        for value_str, unit in re.findall(r"([\d.]+)\s*(ms|s|m|h)", duration_str):
            matched = True
            try:
                value = float(value_str)
            except ValueError:
                return None

            if unit == "ms":
                total_ms += value
            elif unit == "s":
                total_ms += value * 1000.0
            elif unit == "m":
                total_ms += value * 60.0 * 1000.0
            elif unit == "h":
                total_ms += value * 60.0 * 60.0 * 1000.0

        if not matched:
            return None
        return total_ms / 1000.0

    try:
        # 方式 0：HTTP 响应头 Retry-After（最高优先级）
        if response_headers:
            retry_after = response_headers.get("Retry-After") or response_headers.get("retry-after")
            if retry_after:
                try:
                    seconds = int(retry_after)
                    return time.time() + seconds
                except ValueError:
                    try:
                        from email.utils import parsedate_to_datetime
                        retry_dt = parsedate_to_datetime(retry_after)
                        return retry_dt.timestamp()
                    except Exception:
                        pass

        details = error_response.get("error", {}).get("details", []) or []

        # 方式 1：google.rpc.RetryInfo.retryDelay
        for detail in details:
            if not isinstance(detail, dict):
                continue
            type_str = detail.get("@type")
            if isinstance(type_str, str) and "RetryInfo" in type_str:
                retry_delay = detail.get("retryDelay")
                if isinstance(retry_delay, str):
                    seconds = _parse_duration_seconds(retry_delay)
                    if seconds is not None:
                        return time.time() + seconds

        # 方式 2：google.rpc.ErrorInfo.metadata.quotaResetTimeStamp（ISO 时间）
        for detail in details:
            if not isinstance(detail, dict):
                continue
            if detail.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo":
                reset_timestamp_str = (detail.get("metadata", {}) or {}).get("quotaResetTimeStamp")

                if isinstance(reset_timestamp_str, str) and reset_timestamp_str:
                    if reset_timestamp_str.endswith("Z"):
                        reset_timestamp_str = reset_timestamp_str.replace("Z", "+00:00")

                    reset_dt = datetime.fromisoformat(reset_timestamp_str)
                    if reset_dt.tzinfo is None:
                        reset_dt = reset_dt.replace(tzinfo=timezone.utc)

                    return reset_dt.astimezone(timezone.utc).timestamp()

        # 方式 3：metadata.quotaResetDelay（duration）
        for detail in details:
            if not isinstance(detail, dict):
                continue
            quota_delay = (detail.get("metadata", {}) or {}).get("quotaResetDelay")
            if isinstance(quota_delay, str) and quota_delay:
                seconds = _parse_duration_seconds(quota_delay)
                if seconds is not None:
                    return time.time() + seconds

        # 方式 4：从错误消息中正则提取（最后备用）
        if error_message:
            match = re.search(r"(?:reset|retry)\s+(?:after|in)\s+([\d.]+)\s*s(?:econds?)?", error_message, re.IGNORECASE)
            if match:
                try:
                    seconds = float(match.group(1))
                    return time.time() + seconds
                except ValueError:
                    pass

            match = re.search(r"(?:try again|retry)\s+in\s+([\d.]+)\s*([mh])", error_message, re.IGNORECASE)
            if match:
                try:
                    value = float(match.group(1))
                    unit = match.group(2).lower()
                    if unit == "m":
                        return time.time() + value * 60.0
                    elif unit == "h":
                        return time.time() + value * 3600.0
                except ValueError:
                    pass

        return None

    except Exception:
        return None
