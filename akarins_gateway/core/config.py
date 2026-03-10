"""
Pure environment-based configuration for akarins-gateway.

Unlike gcli2api's config.py which depends on a database storage adapter,
this module uses only environment variables and python-dotenv.
No database dependency. No async initialization required.

Priority: ENV variable > .env file > default value
"""

import logging
import os
from typing import Optional

_logger = logging.getLogger(__name__)


def _get_env_str(key: str, default: str) -> str:
    """Get string config from environment."""
    return os.getenv(key, default)


def _get_env_int(key: str, default: int) -> int:
    """Get integer config from environment."""
    value = os.getenv(key)
    if value is not None:
        try:
            return int(value)
        except ValueError:
            _logger.warning(f"[CONFIG] Invalid integer for {key}='{value}', using default {default}")
    return default


def _get_env_float(key: str, default: float) -> float:
    """Get float config from environment."""
    value = os.getenv(key)
    if value is not None:
        try:
            return float(value)
        except ValueError:
            _logger.warning(f"[CONFIG] Invalid float for {key}='{value}', using default {default}")
    return default


def _get_env_bool(key: str, default: bool) -> bool:
    """Get boolean config from environment."""
    value = os.getenv(key)
    if value is not None:
        return value.lower() in ("true", "1", "yes", "on")
    return default


# ====================== Server Configuration ======================

def get_server_host() -> str:
    """Get server bind host. Default: 0.0.0.0"""
    return _get_env_str("HOST", "0.0.0.0")


def get_server_port() -> int:
    """Get server bind port. Default: 7861"""
    return _get_env_int("PORT", 7861)


def get_api_password() -> str:
    """
    Get API password for chat endpoints.

    Priority: API_PASSWORD > PASSWORD > 'pwd'
    """
    api_pwd = os.getenv("API_PASSWORD")
    if api_pwd:
        return api_pwd
    return _get_env_str("PASSWORD", "pwd")


# ====================== Proxy Configuration ======================

def get_proxy_config() -> Optional[str]:
    """Get proxy URL. Returns None if not configured."""
    proxy = os.getenv("PROXY")
    return proxy if proxy else None


# ====================== Feature Flags ======================

def get_enable_antigravity() -> bool:
    """Check if Antigravity backend is enabled. Default: false"""
    return _get_env_bool("ENABLE_ANTIGRAVITY", False)


# ====================== TLS Configuration ======================

def get_tls_impersonate_enabled() -> bool:
    """Check if TLS fingerprint impersonation is enabled. Default: true"""
    return _get_env_bool("TLS_IMPERSONATE_ENABLED", True)


def get_tls_impersonate_target() -> str:
    """Get TLS impersonation target. Default: chrome131"""
    return _get_env_str("TLS_IMPERSONATE_TARGET", "chrome131")


def get_tls_impersonate_backend() -> str:
    """Get TLS impersonation backend. Default: auto"""
    return _get_env_str("TLS_IMPERSONATE_BACKEND", "auto")


# ====================== Retry / Rate Limit ======================

def get_retry_429_max_retries() -> int:
    """Get max retries for 429 errors. Default: 5"""
    return _get_env_int("RETRY_429_MAX_RETRIES", 5)


def get_retry_429_enabled() -> bool:
    """Get 429 retry enabled. Default: true"""
    return _get_env_bool("RETRY_429_ENABLED", True)


def get_retry_429_interval() -> float:
    """Get 429 retry base interval in seconds. Default: 1.0"""
    return _get_env_float("RETRY_429_INTERVAL", 1.0)


# ====================== Panel Configuration ======================

def get_panel_password() -> str:
    """
    Get panel password for admin endpoints.

    Priority: PANEL_PASSWORD > API_PASSWORD > PASSWORD > 'pwd'
    Falls back to API password if not explicitly set.
    """
    panel_pwd = os.getenv("PANEL_PASSWORD")
    if panel_pwd:
        return panel_pwd
    return get_api_password()


# ====================== Gateway Configuration ======================

def get_gateway_config_path() -> str:
    """Get path to gateway.yaml config file. Default: config/gateway.yaml"""
    return _get_env_str("GATEWAY_CONFIG_PATH", "config/gateway.yaml")


def get_max_retries() -> int:
    """Get max retries for upstream requests. Default: 3"""
    return _get_env_int("MAX_RETRIES", 3)


def get_request_timeout() -> float:
    """Get request timeout in seconds. Default: 300.0 (5 minutes)"""
    return _get_env_float("REQUEST_TIMEOUT", 300.0)


def get_streaming_timeout() -> float:
    """Get streaming request timeout in seconds. Default: 600.0 (10 minutes)"""
    return _get_env_float("STREAMING_TIMEOUT", 600.0)


# ====================== Logging ======================

def get_log_level() -> str:
    """Get log level. Default: info"""
    return _get_env_str("LOG_LEVEL", "info").lower()


def get_log_format() -> str:
    """Get log format (text or json). Default: text"""
    return _get_env_str("LOG_FORMAT", "text").lower()


# ====================== Tool Stealth Configuration ======================

def get_tool_semantic_conversion_enabled() -> bool:
    """Check if tool semantic conversion (name obfuscation) is enabled. Default: true"""
    return _get_env_bool("TOOL_SEMANTIC_CONVERSION_ENABLED", True)


# ====================== Thinking / Frontend Configuration ======================

async def get_return_thoughts_to_frontend() -> bool:
    """
    Check if thinking content should be returned to frontend.

    In gcli2api this was backed by database config; in akarins-gateway
    we use a simple ENV variable. The function is async for backward
    compatibility with callers that await it.

    Default: true (return thinking blocks to IDE/frontend)
    """
    return _get_env_bool("RETURN_THOUGHTS_TO_FRONTEND", True)
