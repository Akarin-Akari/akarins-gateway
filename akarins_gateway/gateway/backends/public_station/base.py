"""
Public Station base classes and configuration dataclass.

Defines PublicStationConfig (declarative station definition) and
PublicStationBackend (runtime handler with auth, format conversion,
model support checks, etc.).

Author: 浮浮酱 (Claude Opus 4.6)
Created: 2026-02-14
"""

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Set, Tuple


@dataclass
class PublicStationConfig:
    """
    Declarative configuration for a public station.

    To add a new public station, create a PublicStationConfig instance
    in stations.py — that's all the Python code you need to write.
    """

    # ===== Identity =====
    name: str
    """Unique backend key, e.g., "ruoli", "dkapi". Must match gateway.yaml backend key."""

    display_name: str
    """Human-readable name, e.g., "Ruoli API", "DKAPI (newAPI)"."""

    # ===== Connection =====
    base_url_env: str
    """Environment variable name for the base URL, e.g., "RUOLI_ENDPOINT"."""

    base_url_default: str
    """Default base URL if env var is not set."""

    api_key_env: str
    """Environment variable name for the API key, e.g., "RUOLI_API_KEY"."""

    api_key_default: str = ""
    """Default API key if env var is not set."""

    # ===== Priority =====
    priority: float = 3.0
    """Routing priority (lower = higher priority)."""

    # ===== Authentication =====
    auth_type: Literal["bearer", "x-api-key"] = "bearer"
    """Authentication header type."""

    remove_original_authorization: bool = False
    """Whether to remove the original Authorization header (AnyRouter needs this)."""

    # ===== API Format =====
    api_format: Literal["openai", "anthropic"] = "openai"
    """API format: "openai" for /chat/completions, "anthropic" for /v1/messages."""

    needs_response_conversion: bool = False
    """Whether to convert response from Anthropic to OpenAI format (when original endpoint is /chat/completions)."""

    needs_request_conversion: bool = False
    """Whether to convert request from OpenAI to Anthropic format."""

    # ===== Model Support =====
    supported_models: Set[str] = field(default_factory=set)
    """Exact model names supported (for precise matching)."""

    fuzzy_claude_only: bool = True
    """Whether fuzzy matching is restricted to Claude models only."""

    fuzzy_match_pattern: str = ""
    """
    Regex pattern for fuzzy model matching.
    Example: r'4[.\\-][0-9]' matches "claude-*-4.5", "claude-*-4-5", etc.
    Empty string disables fuzzy matching.
    """

    fuzzy_require_variant: Optional[List[str]] = None
    """
    If set, fuzzy match also requires one of these variant names in the model.
    Example: ["opus", "sonnet"] means only opus/sonnet variants pass fuzzy matching.
    None means all variants pass.
    """

    extra_fuzzy_checks: Optional[List[Dict[str, str]]] = None
    """
    Additional fuzzy checks beyond Claude.
    List of {"prefix": "gemini", "pattern": "2\\.5"} dicts.
    """

    # ===== Behavioral Flags =====
    strip_thinking_suffix: bool = True
    """Whether to remove -thinking suffix from model name before sending."""

    override_user_agent: bool = True
    """Whether to replace User-Agent for anonymity."""

    inject_anthropic_version: bool = True
    """Whether to inject anthropic-version header."""

    anthropic_version: str = "2025-04-01"
    """Default anthropic-version value (if not already in request headers)."""

    # ===== URL Rotation (for multi-endpoint stations) =====
    enable_url_rotation: bool = False
    """Whether to enable URL/key rotation (AnyRouter-style)."""

    base_urls_env: str = ""
    """Environment variable name for comma-separated base URLs."""

    base_urls_fallback_env: str = ""
    """Fallback env var for single URL (if base_urls_env is empty)."""

    base_urls_default: str = ""
    """Default comma-separated base URLs."""

    api_keys_env: str = ""
    """Environment variable name for comma-separated API keys."""

    api_keys_fallback_env: str = ""
    """Fallback env var for single key (if api_keys_env is empty)."""

    api_keys_default: str = ""
    """Default comma-separated API keys."""

    # ===== Timeouts =====
    timeout: float = 120.0
    """Normal request timeout (seconds)."""

    stream_timeout: float = 600.0
    """Streaming request timeout (seconds)."""

    max_retries: int = 2
    """Maximum retry count."""

    # ===== [NEW 2026-02-25] Circuit Breaker =====
    failure_threshold: int = 5
    """
    Consecutive HTTP failure count before auto-freezing this backend.
    Lower = stricter (freeze sooner), higher = more lenient.
    Default 5 is a balanced middle ground.
    """

    # ===== Enabled Flag =====
    enabled_env: str = ""
    """Environment variable name for the enabled flag, e.g., "RUOLI_ENABLED"."""

    enabled_default: bool = True
    """Default enabled value if env var is not set."""

    # ===== Runtime State (managed by PublicStationBackend, do NOT set manually) =====

    def get_base_url(self) -> str:
        """Resolve the base URL from environment variables."""
        url = os.getenv(self.base_url_env, "").strip()
        if not url:
            url = self.base_url_default
        return url.rstrip("/")

    def get_api_key(self) -> str:
        """Resolve the API key from environment variables."""
        key = os.getenv(self.api_key_env, "").strip()
        if not key:
            key = self.api_key_default
        return key

    def is_enabled(self) -> bool:
        """Check if this station is enabled via environment variable."""
        if not self.enabled_env:
            return self.enabled_default
        val = os.getenv(self.enabled_env, "").strip().lower()
        if not val:
            return self.enabled_default
        return val in ("true", "1", "yes")


class PublicStationBackend:
    """
    Runtime handler for a public station.

    Encapsulates all station-specific behavior: authentication, header injection,
    model support checks, request/response format conversion, URL rotation, etc.
    """

    def __init__(self, config: PublicStationConfig):
        self._config = config
        # URL rotation state (only for multi-endpoint stations)
        self._current_url_index: int = 0
        self._current_key_index: int = 0
        # Parse rotation URLs/keys on init
        self._rotation_urls: List[str] = []
        self._rotation_keys: List[str] = []
        if config.enable_url_rotation:
            self._init_rotation()

    @property
    def config(self) -> PublicStationConfig:
        return self._config

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def display_name(self) -> str:
        return self._config.display_name

    # ===== Availability =====

    def is_available(self) -> bool:
        """Check if this station is enabled and has valid credentials."""
        if not self._config.is_enabled():
            return False
        if self._config.enable_url_rotation:
            return bool(self._rotation_urls) and bool(self._rotation_keys)
        return bool(self._config.get_base_url()) and bool(self._config.get_api_key())

    # ===== Model Support =====

    def supports_model(self, model: str) -> bool:
        """
        Unified model support check with exact + fuzzy matching.

        Consolidates is_ruoli_supported, is_newapi_public_supported,
        is_anyrouter_supported into a single configurable check.
        """
        if not model:
            return False

        model_lower = model.lower()

        # Normalize model name (remove -thinking etc. suffixes)
        normalized = self._normalize_model_name(model)

        # 1. Exact match against supported_models
        if normalized in self._config.supported_models:
            return True

        # 2. Fuzzy matching (if configured)
        if self._config.fuzzy_match_pattern:
            if self._config.fuzzy_claude_only and "claude" not in model_lower:
                pass  # Skip fuzzy for non-Claude when claude_only is set
            elif "claude" in model_lower:
                # Check fuzzy pattern
                if re.search(self._config.fuzzy_match_pattern, normalized):
                    # Check variant restriction
                    if self._config.fuzzy_require_variant is not None:
                        if any(v in normalized for v in self._config.fuzzy_require_variant):
                            return True
                    else:
                        return True

        # 3. Extra fuzzy checks (for non-Claude models like Gemini, GPT)
        if self._config.extra_fuzzy_checks:
            for check in self._config.extra_fuzzy_checks:
                prefix = check.get("prefix", "")
                pattern = check.get("pattern", "")
                if prefix and prefix in model_lower:
                    if not pattern or re.search(pattern, model_lower):
                        return True

        return False

    # ===== Request Preparation =====

    def prepare_headers(
        self,
        request_headers: Dict[str, str],
        backend_config: Dict[str, Any],
    ) -> Dict[str, str]:
        """
        Apply all station-specific header transformations.

        Replaces the scattered if/elif chains in proxy.py for:
        - Authentication (Bearer / x-api-key)
        - anthropic-version injection
        - User-Agent override
        """
        headers = dict(request_headers)

        # --- Authentication ---
        if self._config.auth_type == "bearer":
            api_key = self._config.get_api_key()
            if not api_key:
                api_key = backend_config.get("api_key", "")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        elif self._config.auth_type == "x-api-key":
            api_key = self._get_rotation_key() if self._config.enable_url_rotation else self._config.get_api_key()
            if api_key:
                headers["x-api-key"] = api_key
            # Remove OpenAI-format Authorization when using x-api-key
            if self._config.remove_original_authorization and "Authorization" in headers:
                del headers["Authorization"]

        # --- anthropic-version ---
        if self._config.inject_anthropic_version:
            if "anthropic-version" not in headers:
                headers["anthropic-version"] = self._config.anthropic_version

        # --- User-Agent override ---
        if self._config.override_user_agent:
            headers["User-Agent"] = "gcli2api-gateway/1.0 (OpenAI-API-Compatible)"

        return headers

    def prepare_body(self, body: Any) -> Any:
        """
        Apply station-specific body transformations.

        Currently handles:
        - Stripping -thinking suffix from model name
        """
        if not self._config.strip_thinking_suffix:
            return body
        if not isinstance(body, dict) or "model" not in body:
            return body

        original_model = body.get("model", "")
        if original_model and "-thinking" in original_model.lower():
            sanitized = re.sub(r'-thinking$', '', original_model, flags=re.IGNORECASE)
            # Return new dict (immutability)
            return {**body, "model": sanitized}
        return body

    def get_effective_url(self, base_url: str, endpoint: str) -> str:
        """
        Resolve the effective URL for this request.

        For rotation-enabled stations, uses the current rotation URL.
        """
        if self._config.enable_url_rotation:
            rotation_url = self._get_rotation_url()
            if rotation_url:
                # Handle /v1 deduplication
                if rotation_url.endswith("/v1") and endpoint.startswith("/v1/"):
                    endpoint = endpoint[len("/v1"):]
                return f"{rotation_url}{endpoint}"
        return f"{base_url}{endpoint}"

    def get_rotation_endpoint(self) -> Tuple[str, str]:
        """Get current rotation URL and key (for rotation-enabled stations)."""
        if not self._config.enable_url_rotation:
            return self._config.get_base_url(), self._config.get_api_key()
        return self._get_rotation_url(), self._get_rotation_key()

    # ===== Failure Handling =====

    def on_failure(self) -> None:
        """Handle request failure — rotate URL if rotation is enabled."""
        if self._config.enable_url_rotation and self._rotation_urls:
            self._current_url_index = (self._current_url_index + 1) % len(self._rotation_urls)

    # ===== BACKENDS Dict Generation =====

    def to_backends_entry(self) -> Dict[str, Any]:
        """
        Generate a BACKENDS dict entry compatible with the existing config.py format.

        This allows PublicStationManager to auto-register stations into BACKENDS.
        """
        entry: Dict[str, Any] = {
            "name": self._config.display_name,
            "base_url": self._config.get_base_url(),
            "api_key": self._config.get_api_key(),
            "priority": self._config.priority,
            "timeout": self._config.timeout,
            "stream_timeout": self._config.stream_timeout,
            "max_retries": self._config.max_retries,
            "enabled": self._config.is_enabled(),
            "supported_models": list(self._config.supported_models),
            "api_format": self._config.api_format,
            # [NEW 2026-02-25] Circuit breaker threshold
            "circuit_breaker_threshold": self._config.failure_threshold,
        }

        # For rotation-enabled stations, add extra fields
        if self._config.enable_url_rotation:
            entry["base_urls"] = list(self._rotation_urls)
            entry["api_keys"] = list(self._rotation_keys)
            entry["_current_url_index"] = 0
            entry["_current_key_index"] = 0

        return entry

    # ===== Private Helpers =====

    def _normalize_model_name(self, model: str) -> str:
        """Normalize model name by removing variant suffixes."""
        model_lower = model.lower()

        suffixes = [
            "-thinking", "-think", "-extended", "-preview", "-latest",
            "-high", "-low", "-medium",
            "-20241022", "-20240620", "-20250101", "-20250514",
        ]
        for suffix in suffixes:
            model_lower = model_lower.replace(suffix, "")

        # Remove date suffixes
        model_lower = re.sub(r'-\d{8}$', '', model_lower)
        return model_lower.strip("-")

    def _init_rotation(self) -> None:
        """Initialize URL/key rotation lists from environment variables."""
        cfg = self._config

        # Parse URLs
        urls_raw = os.getenv(cfg.base_urls_env, "").strip() if cfg.base_urls_env else ""
        if not urls_raw and cfg.base_urls_fallback_env:
            urls_raw = os.getenv(cfg.base_urls_fallback_env, "").strip()
        if not urls_raw:
            urls_raw = cfg.base_urls_default
        self._rotation_urls = [
            url.strip().rstrip("/") for url in urls_raw.split(",") if url.strip()
        ]

        # Parse keys
        keys_raw = os.getenv(cfg.api_keys_env, "").strip() if cfg.api_keys_env else ""
        if not keys_raw and cfg.api_keys_fallback_env:
            keys_raw = os.getenv(cfg.api_keys_fallback_env, "").strip()
        if not keys_raw:
            keys_raw = cfg.api_keys_default
        self._rotation_keys = [
            key.strip() for key in keys_raw.split(",") if key.strip()
        ]

    def _get_rotation_url(self) -> str:
        """Get the current rotation URL."""
        if not self._rotation_urls:
            return ""
        return self._rotation_urls[self._current_url_index % len(self._rotation_urls)]

    def _get_rotation_key(self) -> str:
        """Get the current rotation API key."""
        if not self._rotation_keys:
            return ""
        return self._rotation_keys[self._current_key_index % len(self._rotation_keys)]
