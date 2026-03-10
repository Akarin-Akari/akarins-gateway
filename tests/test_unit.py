"""
Unit tests for akarins-gateway core modules.

Covers:
  - core/config.py         — env-based configuration functions
  - converters/model_config.py — model mapping, detection, family, thinking logic
  - streaming_constants.py — STREAMING_HEADERS dict structure
  - gateway/backends/antigravity/__init__.py — feature-flag gating
  - app.py                 — FastAPI app factory

All tests are isolated: no external services, no database, no network calls.
Environment-variable tests use monkeypatch so they cannot bleed between cases.
"""

import importlib
import os
import sys
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_antigravity_module(monkeypatch, enable: bool):
    """
    Re-import the antigravity __init__ with a fresh ENABLE_ANTIGRAVITY env value.

    The module reads the env var at import time, so we must remove the cached
    module and re-import it after patching the environment.
    """
    key = "ENABLE_ANTIGRAVITY"
    if enable:
        monkeypatch.setenv(key, "true")
    else:
        monkeypatch.delenv(key, raising=False)

    mod_path = "akarins_gateway.gateway.backends.antigravity"
    # Remove cached module and all sub-modules so the flag is re-evaluated.
    to_remove = [k for k in sys.modules if k.startswith(mod_path)]
    for k in to_remove:
        del sys.modules[k]

    return importlib.import_module(mod_path)


# ===========================================================================
# 1. core/config.py
# ===========================================================================

class TestGetServerPort:
    """get_server_port() returns 7861 by default, respects PORT env override."""

    def test_default_port(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        from akarins_gateway.core.config import get_server_port
        assert get_server_port() == 7861

    def test_env_override_sets_port(self, monkeypatch):
        monkeypatch.setenv("PORT", "9000")
        from akarins_gateway.core.config import get_server_port
        assert get_server_port() == 9000

    def test_invalid_port_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("PORT", "not_a_number")
        from akarins_gateway.core.config import get_server_port
        assert get_server_port() == 7861

    def test_zero_port_is_accepted(self, monkeypatch):
        monkeypatch.setenv("PORT", "0")
        from akarins_gateway.core.config import get_server_port
        assert get_server_port() == 0


class TestGetServerHost:
    """get_server_host() returns '0.0.0.0' by default, respects HOST env override."""

    def test_default_host(self, monkeypatch):
        monkeypatch.delenv("HOST", raising=False)
        from akarins_gateway.core.config import get_server_host
        assert get_server_host() == "0.0.0.0"

    def test_env_override_sets_host(self, monkeypatch):
        monkeypatch.setenv("HOST", "127.0.0.1")
        from akarins_gateway.core.config import get_server_host
        assert get_server_host() == "127.0.0.1"

    def test_localhost_string_accepted(self, monkeypatch):
        monkeypatch.setenv("HOST", "localhost")
        from akarins_gateway.core.config import get_server_host
        assert get_server_host() == "localhost"


class TestGetApiPassword:
    """
    get_api_password() priority chain:
      API_PASSWORD (highest) > PASSWORD > literal 'pwd' (fallback)
    """

    def test_fallback_is_pwd_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("API_PASSWORD", raising=False)
        monkeypatch.delenv("PASSWORD", raising=False)
        from akarins_gateway.core.config import get_api_password
        assert get_api_password() == "pwd"

    def test_password_env_overrides_fallback(self, monkeypatch):
        monkeypatch.delenv("API_PASSWORD", raising=False)
        monkeypatch.setenv("PASSWORD", "mypassword")
        from akarins_gateway.core.config import get_api_password
        assert get_api_password() == "mypassword"

    def test_api_password_overrides_password(self, monkeypatch):
        monkeypatch.setenv("API_PASSWORD", "top_secret")
        monkeypatch.setenv("PASSWORD", "lower_priority")
        from akarins_gateway.core.config import get_api_password
        assert get_api_password() == "top_secret"

    def test_api_password_overrides_when_password_unset(self, monkeypatch):
        monkeypatch.setenv("API_PASSWORD", "only_api_pwd")
        monkeypatch.delenv("PASSWORD", raising=False)
        from akarins_gateway.core.config import get_api_password
        assert get_api_password() == "only_api_pwd"

    def test_empty_api_password_falls_through_to_password(self, monkeypatch):
        # Empty string is falsy — should fall through to PASSWORD
        monkeypatch.setenv("API_PASSWORD", "")
        monkeypatch.setenv("PASSWORD", "fallback_pw")
        from akarins_gateway.core.config import get_api_password
        assert get_api_password() == "fallback_pw"


class TestGetProxyConfig:
    """get_proxy_config() returns None when PROXY is not set, returns the URL when set."""

    def test_returns_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("PROXY", raising=False)
        from akarins_gateway.core.config import get_proxy_config
        assert get_proxy_config() is None

    def test_returns_none_for_empty_string(self, monkeypatch):
        monkeypatch.setenv("PROXY", "")
        from akarins_gateway.core.config import get_proxy_config
        assert get_proxy_config() is None

    def test_returns_proxy_url_when_set(self, monkeypatch):
        monkeypatch.setenv("PROXY", "http://proxy.example.com:8080")
        from akarins_gateway.core.config import get_proxy_config
        assert get_proxy_config() == "http://proxy.example.com:8080"

    def test_returns_socks_proxy_url(self, monkeypatch):
        monkeypatch.setenv("PROXY", "socks5://127.0.0.1:1080")
        from akarins_gateway.core.config import get_proxy_config
        assert get_proxy_config() == "socks5://127.0.0.1:1080"


class TestGetEnableAntigravity:
    """get_enable_antigravity() defaults to False; accepts truthy env values."""

    def test_default_is_false(self, monkeypatch):
        monkeypatch.delenv("ENABLE_ANTIGRAVITY", raising=False)
        from akarins_gateway.core.config import get_enable_antigravity
        assert get_enable_antigravity() is False

    def test_true_string_enables(self, monkeypatch):
        monkeypatch.setenv("ENABLE_ANTIGRAVITY", "true")
        from akarins_gateway.core.config import get_enable_antigravity
        assert get_enable_antigravity() is True

    def test_one_string_enables(self, monkeypatch):
        monkeypatch.setenv("ENABLE_ANTIGRAVITY", "1")
        from akarins_gateway.core.config import get_enable_antigravity
        assert get_enable_antigravity() is True

    def test_yes_string_enables(self, monkeypatch):
        monkeypatch.setenv("ENABLE_ANTIGRAVITY", "yes")
        from akarins_gateway.core.config import get_enable_antigravity
        assert get_enable_antigravity() is True

    def test_on_string_enables(self, monkeypatch):
        monkeypatch.setenv("ENABLE_ANTIGRAVITY", "on")
        from akarins_gateway.core.config import get_enable_antigravity
        assert get_enable_antigravity() is True

    def test_false_string_disables(self, monkeypatch):
        monkeypatch.setenv("ENABLE_ANTIGRAVITY", "false")
        from akarins_gateway.core.config import get_enable_antigravity
        assert get_enable_antigravity() is False

    def test_zero_string_disables(self, monkeypatch):
        monkeypatch.setenv("ENABLE_ANTIGRAVITY", "0")
        from akarins_gateway.core.config import get_enable_antigravity
        assert get_enable_antigravity() is False

    def test_uppercase_true_enables(self, monkeypatch):
        monkeypatch.setenv("ENABLE_ANTIGRAVITY", "TRUE")
        from akarins_gateway.core.config import get_enable_antigravity
        assert get_enable_antigravity() is True


class TestGetTlsImpersonateEnabled:
    """get_tls_impersonate_enabled() defaults to True."""

    def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv("TLS_IMPERSONATE_ENABLED", raising=False)
        from akarins_gateway.core.config import get_tls_impersonate_enabled
        assert get_tls_impersonate_enabled() is True

    def test_false_string_disables(self, monkeypatch):
        monkeypatch.setenv("TLS_IMPERSONATE_ENABLED", "false")
        from akarins_gateway.core.config import get_tls_impersonate_enabled
        assert get_tls_impersonate_enabled() is False

    def test_true_string_keeps_enabled(self, monkeypatch):
        monkeypatch.setenv("TLS_IMPERSONATE_ENABLED", "true")
        from akarins_gateway.core.config import get_tls_impersonate_enabled
        assert get_tls_impersonate_enabled() is True

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv("TLS_IMPERSONATE_ENABLED", "0")
        from akarins_gateway.core.config import get_tls_impersonate_enabled
        assert get_tls_impersonate_enabled() is False


class TestGetReturnThoughtsToFrontend:
    """get_return_thoughts_to_frontend() is async and defaults to True."""

    @pytest.mark.asyncio
    async def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv("RETURN_THOUGHTS_TO_FRONTEND", raising=False)
        from akarins_gateway.core.config import get_return_thoughts_to_frontend
        result = await get_return_thoughts_to_frontend()
        assert result is True

    @pytest.mark.asyncio
    async def test_false_string_returns_false(self, monkeypatch):
        monkeypatch.setenv("RETURN_THOUGHTS_TO_FRONTEND", "false")
        from akarins_gateway.core.config import get_return_thoughts_to_frontend
        result = await get_return_thoughts_to_frontend()
        assert result is False

    @pytest.mark.asyncio
    async def test_true_string_returns_true(self, monkeypatch):
        monkeypatch.setenv("RETURN_THOUGHTS_TO_FRONTEND", "true")
        from akarins_gateway.core.config import get_return_thoughts_to_frontend
        result = await get_return_thoughts_to_frontend()
        assert result is True

    @pytest.mark.asyncio
    async def test_function_is_awaitable(self, monkeypatch):
        monkeypatch.delenv("RETURN_THOUGHTS_TO_FRONTEND", raising=False)
        import inspect
        from akarins_gateway.core.config import get_return_thoughts_to_frontend
        assert inspect.iscoroutinefunction(get_return_thoughts_to_frontend)


# ===========================================================================
# 2. converters/model_config.py
# ===========================================================================

class TestModelMapping:
    """model_mapping() translates known aliases; passes unknown names through."""

    def test_unknown_model_returned_unchanged(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("some-unknown-model-xyz") == "some-unknown-model-xyz"

    def test_claude_sonnet_thinking_alias(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("claude-sonnet-4-5-thinking") == "claude-sonnet-4-5"

    def test_claude_opus_4_5_maps_to_thinking_variant(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("claude-opus-4-5") == "claude-opus-4-5-thinking"

    def test_gemini_flash_thinking_alias(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("gemini-2.5-flash-thinking") == "gemini-2.5-flash"

    def test_gpt4_maps_to_claude_opus(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("gpt-4") == "claude-opus-4-5"

    def test_gpt4o_maps_to_claude_sonnet(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("gpt-4o") == "claude-sonnet-4-5"

    def test_claude_opus_4_6_dot_notation_maps_to_thinking(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("claude-opus-4.6") == "claude-opus-4-6-thinking"

    def test_claude_opus_4_6_hyphen_notation_maps_to_thinking(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("claude-opus-4-6") == "claude-opus-4-6-thinking"

    def test_cursor_variant_high_thinking_maps_correctly(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("claude-4.6-opus-high-thinking") == "claude-opus-4-6-thinking"

    def test_gemini_3_pro_maps_to_high_variant(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("gemini-3-pro") == "gemini-3-pro-high"

    def test_standard_anthropic_name_maps_to_modern(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("claude-3-5-sonnet-20241022") == "claude-sonnet-4-5"

    def test_empty_string_returns_empty_string(self):
        from akarins_gateway.converters.model_config import model_mapping
        assert model_mapping("") == ""


class TestIsThinkingModel:
    """is_thinking_model() detects thinking/pro/opus models."""

    def test_opus_model_is_thinking(self):
        from akarins_gateway.converters.model_config import is_thinking_model
        assert is_thinking_model("claude-opus-4-5") is True

    def test_opus_4_6_is_thinking(self):
        from akarins_gateway.converters.model_config import is_thinking_model
        assert is_thinking_model("claude-opus-4-6-thinking") is True

    def test_sonnet_thinking_suffix_is_thinking(self):
        from akarins_gateway.converters.model_config import is_thinking_model
        assert is_thinking_model("claude-sonnet-4-5-thinking") is True

    def test_gemini_pro_is_thinking(self):
        from akarins_gateway.converters.model_config import is_thinking_model
        # "pro" keyword triggers thinking flag
        assert is_thinking_model("gemini-3-pro-high") is True

    def test_gemini_2_5_pro_is_thinking(self):
        from akarins_gateway.converters.model_config import is_thinking_model
        assert is_thinking_model("gemini-2.5-pro") is True

    def test_regular_sonnet_is_not_thinking(self):
        from akarins_gateway.converters.model_config import is_thinking_model
        assert is_thinking_model("claude-sonnet-4-5") is False

    def test_gemini_flash_is_not_thinking(self):
        from akarins_gateway.converters.model_config import is_thinking_model
        assert is_thinking_model("gemini-2.5-flash") is False

    def test_gpt4_is_not_thinking(self):
        from akarins_gateway.converters.model_config import is_thinking_model
        assert is_thinking_model("gpt-4") is False

    def test_haiku_model_is_not_thinking(self):
        from akarins_gateway.converters.model_config import is_thinking_model
        assert is_thinking_model("claude-haiku-3-5") is False


class TestIsClaudeModel:
    """is_claude_model() detects Claude/Anthropic models correctly."""

    def test_claude_prefix_detected(self):
        from akarins_gateway.converters.model_config import is_claude_model
        assert is_claude_model("claude-opus-4-5") is True

    def test_claude_sonnet_detected(self):
        from akarins_gateway.converters.model_config import is_claude_model
        assert is_claude_model("claude-sonnet-4-5-thinking") is True

    def test_claude_haiku_detected(self):
        from akarins_gateway.converters.model_config import is_claude_model
        assert is_claude_model("claude-haiku-3-5") is True

    def test_anthropic_dot_prefix_detected(self):
        from akarins_gateway.converters.model_config import is_claude_model
        assert is_claude_model("anthropic.claude-v2") is True

    def test_gemini_model_is_not_claude(self):
        from akarins_gateway.converters.model_config import is_claude_model
        assert is_claude_model("gemini-3-pro-high") is False

    def test_gpt4_is_not_claude(self):
        from akarins_gateway.converters.model_config import is_claude_model
        assert is_claude_model("gpt-4") is False

    def test_empty_string_returns_false(self):
        from akarins_gateway.converters.model_config import is_claude_model
        assert is_claude_model("") is False

    def test_none_equivalent_empty_returns_false(self):
        from akarins_gateway.converters.model_config import is_claude_model
        # None is handled via the 'if not model_name' guard
        assert is_claude_model("") is False

    def test_keyword_opus_without_gemini_detected(self):
        # Models that contain "opus" but no "claude-" prefix — treated as Claude
        from akarins_gateway.converters.model_config import is_claude_model
        assert is_claude_model("opus-variant") is True

    def test_gemini_with_sonnet_keyword_is_not_claude(self):
        # Gemini models that accidentally contain a Claude keyword — "gemini" presence prevents match
        from akarins_gateway.converters.model_config import is_claude_model
        # "sonnet" keyword check requires "gemini" NOT in name, so this should be False
        assert is_claude_model("gemini-sonnet-test") is False


class TestIsGeminiModel:
    """is_gemini_model() detects Gemini models correctly."""

    def test_gemini_prefix_detected(self):
        from akarins_gateway.converters.model_config import is_gemini_model
        assert is_gemini_model("gemini-3-pro-high") is True

    def test_gemini_flash_detected(self):
        from akarins_gateway.converters.model_config import is_gemini_model
        assert is_gemini_model("gemini-3-flash") is True

    def test_gemini_2_5_flash_detected(self):
        from akarins_gateway.converters.model_config import is_gemini_model
        assert is_gemini_model("gemini-2.5-flash") is True

    def test_models_slash_gemini_prefix_detected(self):
        from akarins_gateway.converters.model_config import is_gemini_model
        assert is_gemini_model("models/gemini-pro") is True

    def test_claude_model_is_not_gemini(self):
        from akarins_gateway.converters.model_config import is_gemini_model
        assert is_gemini_model("claude-opus-4-5") is False

    def test_gpt4_is_not_gemini(self):
        from akarins_gateway.converters.model_config import is_gemini_model
        assert is_gemini_model("gpt-4") is False

    def test_empty_string_returns_false(self):
        from akarins_gateway.converters.model_config import is_gemini_model
        assert is_gemini_model("") is False

    def test_pro_high_keyword_without_claude_detected(self):
        from akarins_gateway.converters.model_config import is_gemini_model
        # "pro-high" keyword with no "claude" — treated as gemini
        assert is_gemini_model("pro-high-test") is True


class TestGetModelFamily:
    """get_model_family() returns 'claude', 'gemini', or 'other'."""

    def test_claude_opus_family(self):
        from akarins_gateway.converters.model_config import get_model_family
        assert get_model_family("claude-opus-4-5") == "claude"

    def test_claude_sonnet_family(self):
        from akarins_gateway.converters.model_config import get_model_family
        assert get_model_family("claude-sonnet-4-5-thinking") == "claude"

    def test_gemini_pro_family(self):
        from akarins_gateway.converters.model_config import get_model_family
        assert get_model_family("gemini-3-pro-high") == "gemini"

    def test_gemini_flash_family(self):
        from akarins_gateway.converters.model_config import get_model_family
        assert get_model_family("gemini-2.5-flash") == "gemini"

    def test_gpt4_is_other(self):
        from akarins_gateway.converters.model_config import get_model_family
        assert get_model_family("gpt-4") == "other"

    def test_unknown_model_is_other(self):
        from akarins_gateway.converters.model_config import get_model_family
        assert get_model_family("some-totally-unknown-llm") == "other"

    def test_empty_string_is_other(self):
        from akarins_gateway.converters.model_config import get_model_family
        assert get_model_family("") == "other"


class TestShouldPreserveThinkingForModel:
    """
    should_preserve_thinking_for_model() enforces cross-model thinking isolation.

    Rule: only Claude → Claude preserves thinking. All other combos strip it.
    """

    def test_claude_to_claude_preserves_thinking(self):
        from akarins_gateway.converters.model_config import should_preserve_thinking_for_model
        assert should_preserve_thinking_for_model("claude-opus-4-5", "claude-sonnet-4-5") is True

    def test_claude_to_claude_same_model_preserves(self):
        from akarins_gateway.converters.model_config import should_preserve_thinking_for_model
        assert should_preserve_thinking_for_model("claude-opus-4-5", "claude-opus-4-5") is True

    def test_claude_to_gemini_strips_thinking(self):
        from akarins_gateway.converters.model_config import should_preserve_thinking_for_model
        assert should_preserve_thinking_for_model("claude-opus-4-5", "gemini-3-pro-high") is False

    def test_gemini_to_claude_strips_thinking(self):
        from akarins_gateway.converters.model_config import should_preserve_thinking_for_model
        # Gemini thinking has no valid signature — must be stripped
        assert should_preserve_thinking_for_model("gemini-3-pro-high", "claude-opus-4-5") is False

    def test_gemini_to_gemini_strips_thinking(self):
        from akarins_gateway.converters.model_config import should_preserve_thinking_for_model
        assert should_preserve_thinking_for_model("gemini-3-pro-high", "gemini-3-flash") is False

    def test_other_to_claude_strips_thinking(self):
        from akarins_gateway.converters.model_config import should_preserve_thinking_for_model
        assert should_preserve_thinking_for_model("gpt-4", "claude-opus-4-5") is False

    def test_claude_to_other_strips_thinking(self):
        from akarins_gateway.converters.model_config import should_preserve_thinking_for_model
        assert should_preserve_thinking_for_model("claude-opus-4-5", "gpt-4") is False

    def test_other_to_other_strips_thinking(self):
        from akarins_gateway.converters.model_config import should_preserve_thinking_for_model
        assert should_preserve_thinking_for_model("gpt-4", "gpt-4-turbo") is False


class TestGetMaxOutputTokens:
    """get_max_output_tokens() applies prefix matching and caps requested values."""

    def test_unknown_model_returns_default(self):
        from akarins_gateway.converters.model_config import get_max_output_tokens
        assert get_max_output_tokens("some-unknown-model") == 8192

    def test_gemini_2_5_flash_returns_65536(self):
        from akarins_gateway.converters.model_config import get_max_output_tokens
        assert get_max_output_tokens("gemini-2.5-flash") == 65536

    def test_gemini_2_5_pro_returns_65536(self):
        from akarins_gateway.converters.model_config import get_max_output_tokens
        assert get_max_output_tokens("gemini-2.5-pro") == 65536

    def test_gemini_2_0_flash_returns_8192(self):
        from akarins_gateway.converters.model_config import get_max_output_tokens
        assert get_max_output_tokens("gemini-2.0-flash") == 8192

    def test_prefix_match_with_version_suffix(self):
        # "gemini-2.5-flash-preview-05-20" should match prefix "gemini-2.5-flash"
        from akarins_gateway.converters.model_config import get_max_output_tokens
        assert get_max_output_tokens("gemini-2.5-flash-preview-05-20") == 65536

    def test_requested_below_limit_returned_unchanged(self):
        from akarins_gateway.converters.model_config import get_max_output_tokens
        # Request 4096 on a model with 65536 limit — should get 4096 back
        assert get_max_output_tokens("gemini-2.5-flash", requested=4096) == 4096

    def test_requested_above_limit_capped(self):
        from akarins_gateway.converters.model_config import get_max_output_tokens
        # Request 100000 on a model with 65536 limit — capped to 65536
        assert get_max_output_tokens("gemini-2.5-flash", requested=100000) == 65536

    def test_requested_none_returns_model_limit(self):
        from akarins_gateway.converters.model_config import get_max_output_tokens
        assert get_max_output_tokens("gemini-2.5-pro", requested=None) == 65536

    def test_empty_model_name_returns_default(self):
        from akarins_gateway.converters.model_config import get_max_output_tokens
        assert get_max_output_tokens("") == 8192

    def test_gemini_3_pro_returns_65536(self):
        from akarins_gateway.converters.model_config import get_max_output_tokens
        assert get_max_output_tokens("gemini-3-pro") == 65536

    def test_requested_at_exact_limit_returned_unchanged(self):
        from akarins_gateway.converters.model_config import get_max_output_tokens
        assert get_max_output_tokens("gemini-2.0-flash", requested=8192) == 8192


# ===========================================================================
# 3. streaming_constants.py
# ===========================================================================

class TestStreamingHeaders:
    """STREAMING_HEADERS contains the three required anti-buffering fields."""

    def test_streaming_headers_is_dict(self):
        from akarins_gateway.streaming_constants import STREAMING_HEADERS
        assert isinstance(STREAMING_HEADERS, dict)

    def test_x_accel_buffering_is_no(self):
        from akarins_gateway.streaming_constants import STREAMING_HEADERS
        assert STREAMING_HEADERS.get("X-Accel-Buffering") == "no"

    def test_cache_control_is_no_cache(self):
        from akarins_gateway.streaming_constants import STREAMING_HEADERS
        assert "no-cache" in STREAMING_HEADERS.get("Cache-Control", "")

    def test_cache_control_contains_must_revalidate(self):
        from akarins_gateway.streaming_constants import STREAMING_HEADERS
        assert "must-revalidate" in STREAMING_HEADERS.get("Cache-Control", "")

    def test_connection_is_keep_alive(self):
        from akarins_gateway.streaming_constants import STREAMING_HEADERS
        assert STREAMING_HEADERS.get("Connection") == "keep-alive"

    def test_exactly_three_headers(self):
        from akarins_gateway.streaming_constants import STREAMING_HEADERS
        assert len(STREAMING_HEADERS) == 3

    def test_all_header_values_are_strings(self):
        from akarins_gateway.streaming_constants import STREAMING_HEADERS
        for k, v in STREAMING_HEADERS.items():
            assert isinstance(v, str), f"Header {k!r} value should be str, got {type(v)}"


# ===========================================================================
# 4. gateway/backends/antigravity/__init__.py
# ===========================================================================

class TestAntigravityFeatureFlag:
    """
    The antigravity __init__ reads ENABLE_ANTIGRAVITY at import time.

    When disabled (default), backends are None stubs.
    When enabled, backend classes would be imported (tested via mock).
    """

    def test_enable_antigravity_constant_is_false_by_default(self, monkeypatch):
        mod = _reload_antigravity_module(monkeypatch, enable=False)
        assert mod.ENABLE_ANTIGRAVITY is False

    def test_antigravity_backend_is_none_when_disabled(self, monkeypatch):
        mod = _reload_antigravity_module(monkeypatch, enable=False)
        assert mod.AntigravityBackend is None

    def test_enable_antigravity_constant_is_true_when_set(self, monkeypatch):
        # Patch the real backend module so the import doesn't fail
        mock_backend = MagicMock()
        backend_path = "akarins_gateway.gateway.backends.antigravity.backend"

        with patch.dict(sys.modules, {
            backend_path: mock_backend,
        }):
            mod = _reload_antigravity_module(monkeypatch, enable=True)
            assert mod.ENABLE_ANTIGRAVITY is True

    def test_backend_is_not_none_when_enabled(self, monkeypatch):
        mock_backend_mod = MagicMock()
        mock_backend_mod.AntigravityBackend = object()

        backend_path = "akarins_gateway.gateway.backends.antigravity.backend"

        with patch.dict(sys.modules, {
            backend_path: mock_backend_mod,
        }):
            mod = _reload_antigravity_module(monkeypatch, enable=True)
            assert mod.AntigravityBackend is not None

    def test_all_exports_declared_in_all(self, monkeypatch):
        mod = _reload_antigravity_module(monkeypatch, enable=False)
        assert "ENABLE_ANTIGRAVITY" in mod.__all__
        assert "AntigravityBackend" in mod.__all__

    def test_antigravity_tools_not_in_antigravity_module(self, monkeypatch):
        """AntigravityToolsBackend should NOT be in antigravity isolation module (moved out 2026-02-28)."""
        mod = _reload_antigravity_module(monkeypatch, enable=False)
        assert "AntigravityToolsBackend" not in mod.__all__

    def test_truthy_values_enable_flag(self, monkeypatch):
        """'yes' and '1' should also enable the flag (tested via config helper)."""
        for val in ("1", "yes", "on", "TRUE"):
            monkeypatch.setenv("ENABLE_ANTIGRAVITY", val)
            from akarins_gateway.core.config import get_enable_antigravity
            assert get_enable_antigravity() is True, f"Expected True for ENABLE_ANTIGRAVITY={val!r}"


# ===========================================================================
# 4b. gateway/backends/antigravity_tools.py (independent from antigravity/)
# ===========================================================================

class TestAntigravityToolsIndependence:
    """
    AntigravityToolsBackend is an independent external service proxy (port 9046).
    It is NOT controlled by ENABLE_ANTIGRAVITY and lives outside the antigravity/
    isolation folder.
    """

    def test_import_independent_of_antigravity_flag(self, monkeypatch):
        """AT should be importable regardless of ENABLE_ANTIGRAVITY setting."""
        monkeypatch.setenv("ENABLE_ANTIGRAVITY", "false")
        from akarins_gateway.gateway.backends.antigravity_tools import AntigravityToolsBackend
        assert AntigravityToolsBackend is not None

    def test_class_has_required_methods(self):
        """AT should implement GatewayBackend protocol methods."""
        from akarins_gateway.gateway.backends.antigravity_tools import AntigravityToolsBackend
        required = {"is_available", "supports_model", "handle_request", "handle_streaming_request", "close"}
        actual = {m for m in dir(AntigravityToolsBackend) if not m.startswith("_")}
        assert required.issubset(actual), f"Missing methods: {required - actual}"

    def test_backends_init_exports_antigravity_tools(self):
        """backends/__init__.py should export AntigravityToolsBackend."""
        import akarins_gateway.gateway.backends as backends_pkg
        assert "AntigravityToolsBackend" in backends_pkg.__all__


# ===========================================================================
# 5. app.py
# ===========================================================================

class TestCreateApp:
    """create_app() produces a valid, configured FastAPI instance."""

    def test_create_app_returns_fastapi_instance(self):
        from fastapi import FastAPI
        from akarins_gateway.app import create_app
        app = create_app()
        assert isinstance(app, FastAPI)

    def test_app_title_is_correct(self):
        from akarins_gateway.app import create_app
        app = create_app()
        assert app.title == "Akarin's Gateway"

    def test_app_version_is_correct(self):
        from akarins_gateway.app import create_app
        app = create_app()
        assert app.version == "1.0.0"

    def test_app_has_routes(self):
        from akarins_gateway.app import create_app
        app = create_app()
        assert len(app.routes) > 0

    def test_keepalive_route_exists(self):
        from akarins_gateway.app import create_app
        app = create_app()
        route_paths = [getattr(r, "path", "") for r in app.routes]
        assert "/keepalive" in route_paths

    def test_v1_chat_completions_route_exists(self):
        """The OpenAI-compatible chat completions endpoint must be mounted."""
        from akarins_gateway.app import create_app
        app = create_app()
        route_paths = [getattr(r, "path", "") for r in app.routes]
        assert any("/v1/chat/completions" in p for p in route_paths), (
            f"No /v1/chat/completions route found. Routes: {route_paths}"
        )

    def test_gateway_prefix_chat_completions_route_exists(self):
        """The /gateway prefix compat route must also be mounted."""
        from akarins_gateway.app import create_app
        app = create_app()
        route_paths = [getattr(r, "path", "") for r in app.routes]
        assert any(p.startswith("/gateway") for p in route_paths), (
            f"No /gateway/* route found. Routes: {route_paths}"
        )

    def test_module_level_app_is_fastapi_instance(self):
        """The module-level 'app' singleton created at import time is valid."""
        from fastapi import FastAPI
        from akarins_gateway import app as app_module
        assert isinstance(app_module.app, FastAPI)

    def test_cors_middleware_is_configured(self):
        """CORSMiddleware must be present in the middleware stack."""
        from fastapi.middleware.cors import CORSMiddleware
        from akarins_gateway.app import create_app
        app = create_app()
        middleware_types = [
            m.cls for m in app.user_middleware
            if hasattr(m, "cls")
        ]
        assert CORSMiddleware in middleware_types, (
            "CORSMiddleware not found in middleware stack"
        )
