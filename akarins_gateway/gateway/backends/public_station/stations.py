"""
Public Station Declarations — The ONLY file you need to edit to add a new station.

Each station is defined as a PublicStationConfig instance.
The PublicStationManager will auto-handle:
  - Authentication headers (Bearer / x-api-key)
  - anthropic-version injection
  - User-Agent override
  - Model support checks (exact + fuzzy)
  - -thinking suffix stripping
  - URL rotation (if enabled)
  - BACKENDS dict generation
  - Response format conversion flags

To add a new public station:
  1. Add a new entry to PUBLIC_STATIONS dict below
  2. Add env vars to .env.example
  3. Add model routing entries in config/gateway.yaml
  Done!

Author: 浮浮酱 (Claude Opus 4.6)
Created: 2026-02-14
"""

from .base import PublicStationConfig

# ============================================================================
# PUBLIC STATIONS REGISTRY
#
# Add new public stations here. Each entry is a self-contained declaration.
# The backend key (dict key) MUST match the key used in gateway.yaml.
# ============================================================================

PUBLIC_STATIONS: dict[str, PublicStationConfig] = {
    # ===================================================================
    # Ruoli API — 公益站 Claude API (OpenAI 格式)
    # Supports: Claude Opus/Sonnet 4.5, 4.6 (no Haiku)
    # Auth: Bearer token
    # Format: OpenAI compatible
    # ===================================================================
    "ruoli": PublicStationConfig(
        name="ruoli",
        display_name="Ruoli API",
        # Connection
        base_url_env="RUOLI_ENDPOINT",
        base_url_default="https://api.ruoli.dev/v1",
        api_key_env="RUOLI_API_KEY",
        api_key_default="sk-IJDHo468ySwMyRIk2MxmDgobgRhTRB1uhJCu41EHRYTaGUy1",
        # Priority
        priority=3.0,
        # Auth
        auth_type="bearer",
        # Format
        api_format="openai",
        # Model support
        supported_models={
            "claude-opus-4.5", "claude-opus-4-5",
            "claude-sonnet-4.5", "claude-sonnet-4-5",
            "claude-opus-4.6", "claude-opus-4-6",
        },
        # Fuzzy: match Claude 4.5/4.6 but only opus/sonnet (no haiku)
        fuzzy_match_pattern=r'4[.\-][0-9]',
        fuzzy_require_variant=["opus", "sonnet"],
        # Enabled
        enabled_env="RUOLI_ENABLED",
        # [NEW 2026-02-25] Circuit breaker: strict — low success rate station
        failure_threshold=2,
    ),

    # ===================================================================
    # DKAPI — newAPI 框架公益站
    # [DISABLED 2026-02-14] API Key 已失效 (401: 无效的令牌)
    # 待上游恢复后取消注释即可重新启用
    # ===================================================================
    # "dkapi": PublicStationConfig(
    #     name="dkapi",
    #     display_name="DKAPI (newAPI)",
    #     base_url_env="DKAPI_ENDPOINT",
    #     base_url_default="https://api.dkjsiogu.me/v1",
    #     api_key_env="DKAPI_API_KEY",
    #     api_key_default="sk-vQFk5OTJyARNdDY8fZ5OtyM84hdX7EuVJi13lVWoR2RzknT9",
    #     priority=3.2,
    #     auth_type="bearer",
    #     api_format="openai",
    #     supported_models={
    #         "claude-opus-4.6", "claude-opus-4-6",
    #         "claude-opus-4.5", "claude-opus-4-5",
    #         "claude-sonnet-4.5", "claude-sonnet-4-5",
    #         "claude-haiku-4.5", "claude-haiku-4-5",
    #         "claude-opus-4", "claude-sonnet-4",
    #     },
    #     fuzzy_match_pattern=r'4[.\-][0-9]',
    #     fuzzy_require_variant=None,
    #     enabled_env="DKAPI_ENABLED",
    # ),

    # ===================================================================
    # Cifang — newAPI 框架公益站
    # [DISABLED 2026-02-14] API Key 已失效 (401: 该令牌状态不可用)
    # 待上游恢复后取消注释即可重新启用
    # ===================================================================
    # "cifang": PublicStationConfig(
    #     name="cifang",
    #     display_name="Cifang (newAPI)",
    #     base_url_env="CIFANG_ENDPOINT",
    #     base_url_default="https://cifang.xyz/v1",
    #     api_key_env="CIFANG_API_KEY",
    #     api_key_default="sk-XboAMdIlo5FyED0FZVYarY7O4tK1NKaq1KWNzC9W5qc9s684",
    #     priority=3.4,
    #     auth_type="bearer",
    #     api_format="openai",
    #     supported_models={
    #         "claude-opus-4.6", "claude-opus-4-6",
    #         "claude-opus-4.5", "claude-opus-4-5",
    #         "claude-sonnet-4.5", "claude-sonnet-4-5",
    #         "claude-haiku-4.5", "claude-haiku-4-5",
    #         "claude-opus-4", "claude-sonnet-4",
    #     },
    #     fuzzy_match_pattern=r'4[.\-][0-9]',
    #     fuzzy_require_variant=None,
    #     enabled_env="CIFANG_ENABLED",
    # ),

    # ===================================================================
    # AnyRouter — 公益站多模型路由
    # Supports: Claude (all versions), Gemini 2.5, GPT-5 Codex
    # Auth: x-api-key (NOT Bearer)
    # Format: Anthropic Messages (/v1/messages)
    # Special: Multi-URL rotation, request/response format conversion
    # ===================================================================
    "anyrouter": PublicStationConfig(
        name="anyrouter",
        display_name="AnyRouter",
        # Connection (ignored for rotation-enabled, see below)
        base_url_env="ANYROUTER_ENDPOINT",
        base_url_default="https://anyrouter.top",
        api_key_env="ANYROUTER_API_KEY",
        api_key_default="",
        # Priority
        priority=4.0,
        # Auth — AnyRouter uses x-api-key, removes Authorization
        auth_type="x-api-key",
        remove_original_authorization=True,
        # Compatibility — keep legacy Anthropic header expected by AnyRouter
        anthropic_version="2023-06-01",
        # Format — Anthropic Messages API
        api_format="anthropic",
        needs_response_conversion=True,
        needs_request_conversion=True,
        # Model support — broad Claude + extras
        supported_models={
            # Claude 4.5
            "claude-opus-4-5-20251101", "claude-sonnet-4-5-20250929",
            "claude-haiku-4-5-20251001",
            "claude-opus-4.5", "claude-sonnet-4.5", "claude-haiku-4.5",
            # Claude 4
            "claude-opus-4-20250514", "claude-opus-4-1-20250805",
            "claude-sonnet-4-20250514",
            "claude-opus-4", "claude-sonnet-4",
            # Claude 3.7
            "claude-3-7-sonnet-20250219", "claude-3.7-sonnet",
            # Claude 3.5
            "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
            "claude-3.5-sonnet", "claude-3.5-haiku",
            # Other
            "gemini-2.5-pro", "gpt-5-codex",
        },
        # Fuzzy: accept ALL claude models
        fuzzy_claude_only=False,
        fuzzy_match_pattern="",  # No version-based fuzzy for AnyRouter
        extra_fuzzy_checks=[
            # Accept all Claude models
            {"prefix": "claude", "pattern": ""},
            # Accept Gemini 2.5
            {"prefix": "gemini", "pattern": r"2\.5"},
            # Accept GPT-5 Codex
            {"prefix": "gpt-5", "pattern": "codex"},
        ],
        # URL rotation — AnyRouter has multiple endpoints
        enable_url_rotation=True,
        base_urls_env="ANYROUTER_BASE_URLS",
        base_urls_fallback_env="ANYROUTER_ENDPOINT",
        base_urls_default=(
            "https://anyrouter.top,"
            "https://a-ocnfniawgw.cn-shanghai.fcapp.run"
        ),
        api_keys_env="ANYROUTER_API_KEYS",
        api_keys_fallback_env="ANYROUTER_API_KEY",
        api_keys_default=(
            "sk-be7LKJwag3qXSRL77tVbxUsIHEi71UfAVOvqjGI13BJiXGD5"
        ),
        # Timeouts
        max_retries=1,  # Each endpoint only retries once
        # Enabled
        enabled_env="ANYROUTER_ENABLED",
        # [NEW 2026-02-25] Circuit breaker: lenient — stable, high success rate station
        failure_threshold=8,
    ),
}
