"""
Unit tests and integration tests for ModelRegistry.

Covers:
  - ModelRegistry singleton + initialization
  - Layer 1 (static) loading from BACKENDS config
  - Layer 2 (dynamic) refresh from /v1/models endpoints
  - Only-Add-Never-Remove invariant
  - Cache building (OpenAI + Augment formats)
  - Stale-while-revalidate trigger
  - Feature flag (MODEL_REGISTRY_ENABLED)
  - Routing integration (_is_model_supported_by_backend fallback)
  - Endpoint integration (cache-first /v1/models)
  - Config reload hook (reload_model_routing_config -> reload_static)

All tests are isolated: no external services, no network calls.
Uses unittest.mock for HTTP mocking and monkeypatch for env vars.

Author: fufu-chan (Claude Opus 4.6)
Date: 2026-03-10
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(enabled=True):
    """Create a fresh ModelRegistry instance (bypass singleton)."""
    from akarins_gateway.gateway.model_registry import ModelRegistry
    registry = ModelRegistry()
    registry._enabled = enabled
    return registry


def _sample_backends_config():
    """Return a minimal BACKENDS-like dict for testing."""
    return {
        "zerogravity": {
            "enabled": True,
            "priority": 0,
            "base_url": "http://localhost:8080/v1",
            "models": ["claude-opus-4-6", "claude-sonnet-4-5", "gemini-2.5-pro"],
        },
        "kiro-gateway": {
            "enabled": True,
            "priority": 2,
            "base_url": "http://localhost:9090/v1",
            "models": ["claude-sonnet-4-5", "gpt-4.1"],
        },
        "gcli2api-antigravity": {
            "enabled": True,
            "priority": 1,
            "base_url": "http://localhost:7070/v1",
            "models": ["*"],
        },
        "disabled-backend": {
            "enabled": False,
            "priority": 99,
            "base_url": "http://localhost:1111/v1",
            "models": ["model-x"],
        },
    }


def _mock_models_response(model_ids: list) -> dict:
    """Build a mock /v1/models JSON response."""
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "owned_by": "test-backend"}
            for mid in model_ids
        ],
    }


# ===========================================================================
# 1. Unit Tests — ModelRegistry Core
# ===========================================================================

class TestModelRegistryInit:
    """ModelRegistry.__init__() sets correct default state."""

    def test_default_state(self):
        registry = _make_registry()
        assert registry.enabled is True
        assert registry.initialized is False
        assert registry.get_all_known_models() == set()
        assert registry.get_cached_models_response() is None
        assert registry.get_cached_augment_models_response() is None

    def test_disabled_via_flag(self):
        registry = _make_registry(enabled=False)
        assert registry.enabled is False
        assert registry.get_cached_models_response() is None


class TestModelRegistryInitialize:
    """ModelRegistry.initialize() loads Layer 1 from BACKENDS config."""

    def test_loads_static_models(self):
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        assert registry.initialized is True
        known = registry.get_all_known_models()
        assert "claude-opus-4-6" in known
        assert "claude-sonnet-4-5" in known
        assert "gemini-2.5-pro" in known
        assert "gpt-4.1" in known

    def test_wildcard_backend_detected(self):
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        # gcli2api-antigravity has models: ["*"]
        assert "gcli2api-antigravity" in registry._wildcard_backends

    def test_disabled_backend_skipped(self):
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        # disabled-backend's model should NOT be in static models
        assert "model-x" not in registry.get_all_known_models()

    def test_multiple_backends_per_model(self):
        """claude-sonnet-4-5 is in both zerogravity and kiro-gateway."""
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        backends = registry.get_backends_for_model("claude-sonnet-4-5")
        assert "zerogravity" in backends
        assert "kiro-gateway" in backends
        # Wildcard backend always included
        assert "gcli2api-antigravity" in backends

    def test_reinitialize_clears_old_data(self):
        registry = _make_registry()
        registry.initialize({"backend-a": {"enabled": True, "models": ["model-old"]}})
        assert "model-old" in registry.get_all_known_models()

        # Re-initialize with different config
        registry.initialize({"backend-b": {"enabled": True, "models": ["model-new"]}})
        assert "model-old" not in registry._static_models
        assert "model-new" in registry.get_all_known_models()

    def test_supported_models_key_fallback(self):
        """BACKENDS dict sometimes uses 'supported_models' instead of 'models'."""
        registry = _make_registry()
        registry.initialize({
            "backend-x": {
                "enabled": True,
                "supported_models": ["model-via-supported"],
            }
        })
        assert "model-via-supported" in registry.get_all_known_models()


class TestIsModelKnownToBackend:
    """ModelRegistry.is_model_known_to_backend() checks all layers."""

    def test_exact_match_static(self):
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        assert registry.is_model_known_to_backend("claude-opus-4-6", "zerogravity") is True
        assert registry.is_model_known_to_backend("gpt-4.1", "zerogravity") is False

    def test_exact_match_dynamic(self):
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        # Simulate dynamic discovery
        registry._dynamic_models["new-dynamic-model"] = {"some-backend"}

        assert registry.is_model_known_to_backend("new-dynamic-model", "some-backend") is True
        assert registry.is_model_known_to_backend("new-dynamic-model", "other-backend") is False

    def test_wildcard_backend_always_true(self):
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        # Wildcard backend accepts any model
        assert registry.is_model_known_to_backend("any-random-model", "gcli2api-antigravity") is True

    def test_unknown_model_unknown_backend(self):
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        assert registry.is_model_known_to_backend("nonexistent-model", "nonexistent-backend") is False

    def test_normalized_model_matching(self):
        """Normalized model name should find backends via normalize_model_name()."""
        registry = _make_registry()
        # Register a model under its normalized name
        registry.initialize({
            "backend-norm": {
                "enabled": True,
                "models": ["claude-sonnet-4-5"],
            }
        })

        # The exact name should match
        assert registry.is_model_known_to_backend("claude-sonnet-4-5", "backend-norm") is True

        # A variant that normalizes to the same base name should also match
        # (if normalize_model_name strips suffixes like -thinking, -preview, etc.)
        try:
            from akarins_gateway.gateway.config import normalize_model_name
            variant = "claude-sonnet-4-5-thinking"
            normalized = normalize_model_name(variant)
            if normalized == "claude-sonnet-4-5":
                # normalize_model_name does strip -thinking, so the variant should match
                assert registry.is_model_known_to_backend(variant, "backend-norm") is True
            # Also verify that a completely unrelated model does NOT match
            assert registry.is_model_known_to_backend("gpt-4.1", "backend-norm") is False
        except ImportError:
            # normalize_model_name not available in test env — skip variant check
            pass


class TestGetBackendsForModel:
    """ModelRegistry.get_backends_for_model() returns union of all layers."""

    def test_union_static_dynamic_wildcard(self):
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        # Add a dynamic discovery
        registry._dynamic_models["claude-opus-4-6"] = {"new-dynamic-backend"}

        backends = registry.get_backends_for_model("claude-opus-4-6")
        # Static: zerogravity
        assert "zerogravity" in backends
        # Dynamic: new-dynamic-backend
        assert "new-dynamic-backend" in backends
        # Wildcard: gcli2api-antigravity
        assert "gcli2api-antigravity" in backends

    def test_unknown_model_returns_only_wildcards(self):
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        backends = registry.get_backends_for_model("totally-unknown-model")
        # Only wildcard backend
        assert backends == ["gcli2api-antigravity"]


# ===========================================================================
# 2. Unit Tests — Layer 2: Dynamic Refresh
# ===========================================================================

class TestRefreshBackend:
    """ModelRegistry._refresh_backend() fetches /models from a backend."""

    @pytest.mark.asyncio
    async def test_successful_refresh(self):
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _mock_models_response(
            ["new-model-a", "new-model-b"]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.get_client.return_value = mock_client

        with patch(
            "akarins_gateway.gateway.model_registry.http_client", mock_http,
            create=True,
        ):
            # Need to patch the import inside the method
            with patch.dict("sys.modules", {
                "akarins_gateway.core.httpx_client": MagicMock(http_client=mock_http),
            }):
                result = await registry._refresh_backend(
                    "zerogravity",
                    {"base_url": "http://localhost:8080/v1", "api_key": "test-key"},
                )

        assert result is True
        assert "new-model-a" in registry._dynamic_models
        assert "new-model-b" in registry._dynamic_models
        assert "zerogravity" in registry._dynamic_models["new-model-a"]
        assert "zerogravity" in registry._last_refresh

    @pytest.mark.asyncio
    async def test_empty_response_keeps_stale_data(self):
        """Edge case #2: empty model list should not clear existing data."""
        registry = _make_registry()
        registry.initialize(_sample_backends_config())

        # Pre-populate dynamic data
        registry._dynamic_models["stale-model"] = {"zerogravity"}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"object": "list", "data": []}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.get_client.return_value = mock_client

        with patch.dict("sys.modules", {
            "akarins_gateway.core.httpx_client": MagicMock(http_client=mock_http),
        }):
            result = await registry._refresh_backend(
                "zerogravity",
                {"base_url": "http://localhost:8080/v1"},
            )

        assert result is False
        # Stale data preserved
        assert "stale-model" in registry._dynamic_models

    @pytest.mark.asyncio
    async def test_http_error_returns_false(self):
        """Edge case #1: backend returns non-200."""
        registry = _make_registry()

        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.get_client.return_value = mock_client

        with patch.dict("sys.modules", {
            "akarins_gateway.core.httpx_client": MagicMock(http_client=mock_http),
        }):
            result = await registry._refresh_backend(
                "zerogravity",
                {"base_url": "http://localhost:8080/v1"},
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_no_base_url_skipped(self):
        """Edge case: backend without base_url."""
        registry = _make_registry()
        result = await registry._refresh_backend("no-url", {"priority": 1})
        assert result is False

    @pytest.mark.asyncio
    async def test_anthropic_format_skipped(self):
        """Edge case #6: anthropic format backends skip /models refresh."""
        registry = _make_registry()
        result = await registry._refresh_backend(
            "antigravity-tools",
            {"base_url": "http://localhost:5555/v1", "api_format": "anthropic"},
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_real_api_key_used(self):
        """Edge case #4: backend with api_key uses it instead of dummy."""
        registry = _make_registry()

        captured_headers = {}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _mock_models_response(["model-x"])

        async def capture_get(url, headers=None):
            captured_headers.update(headers or {})
            return mock_response

        mock_client = AsyncMock()
        mock_client.get = capture_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.get_client.return_value = mock_client

        with patch.dict("sys.modules", {
            "akarins_gateway.core.httpx_client": MagicMock(http_client=mock_http),
        }):
            await registry._refresh_backend(
                "ruoli",
                {"base_url": "http://ruoli.example/v1", "api_key": "sk-real-key"},
            )

        assert captured_headers.get("Authorization") == "Bearer sk-real-key"

    @pytest.mark.asyncio
    async def test_api_keys_list_fallback(self):
        """Edge case #4: backend with api_keys (list) uses first key."""
        registry = _make_registry()

        captured_headers = {}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _mock_models_response(["model-y"])

        async def capture_get(url, headers=None):
            captured_headers.update(headers or {})
            return mock_response

        mock_client = AsyncMock()
        mock_client.get = capture_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.get_client.return_value = mock_client

        with patch.dict("sys.modules", {
            "akarins_gateway.core.httpx_client": MagicMock(http_client=mock_http),
        }):
            await registry._refresh_backend(
                "anyrouter",
                {
                    "base_url": "http://anyrouter.example/v1",
                    "api_keys": ["sk-first", "sk-second"],
                },
            )

        assert captured_headers.get("Authorization") == "Bearer sk-first"


class TestOnlyAddNeverRemove:
    """Core invariant: dynamic discovery only adds, never removes."""

    @pytest.mark.asyncio
    async def test_model_persists_after_disappearing_from_backend(self):
        """
        Refresh 1: backend reports [A, B]
        Refresh 2: backend reports [B, C]
        Result: registry should have [A, B, C] for that backend
        """
        registry = _make_registry()
        registry.initialize({})

        async def do_refresh(model_ids):
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = _mock_models_response(model_ids)

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            mock_http = MagicMock()
            mock_http.get_client.return_value = mock_client

            with patch.dict("sys.modules", {
                "akarins_gateway.core.httpx_client": MagicMock(http_client=mock_http),
            }):
                await registry._refresh_backend(
                    "test-backend",
                    {"base_url": "http://test/v1"},
                )

        # Refresh 1: A, B
        await do_refresh(["model-a", "model-b"])
        assert registry.is_model_known_to_backend("model-a", "test-backend")
        assert registry.is_model_known_to_backend("model-b", "test-backend")

        # Refresh 2: B, C (A disappeared from backend response)
        await do_refresh(["model-b", "model-c"])

        # A is STILL in the registry (Only-Add-Never-Remove)
        assert registry.is_model_known_to_backend("model-a", "test-backend")
        assert registry.is_model_known_to_backend("model-b", "test-backend")
        assert registry.is_model_known_to_backend("model-c", "test-backend")


# ===========================================================================
# 3. Unit Tests — Cache
# ===========================================================================

class TestCacheBuilding:
    """ModelRegistry._rebuild_cache() builds correct response formats."""

    def test_openai_format(self):
        registry = _make_registry()
        registry.initialize({
            "backend-a": {"enabled": True, "models": ["model-1", "model-2"]},
        })
        registry._rebuild_cache()

        cached = registry.get_cached_models_response()
        assert cached is not None
        assert cached["object"] == "list"
        assert len(cached["data"]) == 2
        ids = {m["id"] for m in cached["data"]}
        assert ids == {"model-1", "model-2"}
        # Each model has required fields
        for m in cached["data"]:
            assert "object" in m
            assert "owned_by" in m

    def test_augment_format(self):
        registry = _make_registry()
        registry.initialize({
            "backend-a": {"enabled": True, "models": ["model-x"]},
        })
        registry._rebuild_cache()

        cached = registry.get_cached_augment_models_response()
        assert cached is not None
        assert len(cached) == 1
        assert cached[0]["id"] == "model-x"
        assert "name" in cached[0]
        assert "displayName" in cached[0]

    def test_dynamic_model_objects_enrich_cache(self):
        """Dynamic model objects provide richer data in cache."""
        registry = _make_registry()
        registry.initialize({})

        # Simulate dynamic discovery with rich objects
        registry._dynamic_models["rich-model"] = {"backend-a"}
        registry._dynamic_model_objects["rich-model"] = {
            "id": "rich-model",
            "object": "model",
            "owned_by": "anthropic",
            "display_name": "Rich Model Display",
        }
        registry._rebuild_cache()

        cached = registry.get_cached_models_response()
        model = next(m for m in cached["data"] if m["id"] == "rich-model")
        assert model["owned_by"] == "anthropic"

        augment = registry.get_cached_augment_models_response()
        aug_model = next(m for m in augment if m["id"] == "rich-model")
        assert aug_model["displayName"] == "Rich Model Display"

    def test_cache_returns_none_when_disabled(self):
        registry = _make_registry(enabled=False)
        registry.initialize({"b": {"enabled": True, "models": ["m"]}})
        registry._rebuild_cache()

        assert registry.get_cached_models_response() is None
        assert registry.get_cached_augment_models_response() is None


class TestStaleWhileRevalidate:
    """Stale cache triggers background refresh."""

    def test_fresh_cache_not_stale(self):
        registry = _make_registry()
        registry._cache_built_at = time.monotonic()
        registry._stale_threshold = 600
        assert registry._is_cache_stale() is False

    def test_old_cache_is_stale(self):
        registry = _make_registry()
        registry._cache_built_at = time.monotonic() - 1000
        registry._stale_threshold = 600
        assert registry._is_cache_stale() is True

    def test_zero_cache_time_is_stale(self):
        registry = _make_registry()
        registry._cache_built_at = 0.0
        assert registry._is_cache_stale() is True


# ===========================================================================
# 4. Unit Tests — Singleton & Feature Flag
# ===========================================================================

class TestSingleton:
    """get_model_registry() returns the same instance."""

    def test_singleton_returns_same_instance(self):
        from akarins_gateway.gateway import model_registry as mod
        # Reset singleton for clean test
        mod._registry_instance = None

        r1 = mod.get_model_registry()
        r2 = mod.get_model_registry()
        assert r1 is r2

        # Cleanup
        mod._registry_instance = None


class TestGetStats:
    """ModelRegistry.get_stats() returns diagnostic info."""

    def test_stats_structure(self):
        registry = _make_registry()
        registry.initialize(_sample_backends_config())
        registry._rebuild_cache()

        stats = registry.get_stats()
        assert "enabled" in stats
        assert "initialized" in stats
        assert "static_models_count" in stats
        assert "dynamic_models_count" in stats
        assert "total_known_models" in stats
        assert "wildcard_backends" in stats
        assert "cache_stale" in stats
        assert stats["enabled"] is True
        assert stats["initialized"] is True
        assert stats["static_models_count"] > 0


class TestReloadStatic:
    """ModelRegistry.reload_static() re-reads from BACKENDS."""

    def test_reload_reinitializes(self):
        registry = _make_registry()
        registry.initialize({"old": {"enabled": True, "models": ["old-model"]}})
        assert "old-model" in registry.get_all_known_models()

        with patch(
            "akarins_gateway.gateway.model_registry.ModelRegistry.initialize"
        ) as mock_init:
            with patch.dict("sys.modules", {
                "akarins_gateway.gateway.config": MagicMock(
                    BACKENDS={"new": {"enabled": True, "models": ["new-model"]}}
                ),
            }):
                registry.reload_static()
                mock_init.assert_called_once()


# ===========================================================================
# 5. Integration Tests — Endpoint Cache-First
# ===========================================================================

class TestEndpointCacheIntegration:
    """Verify /v1/models uses registry cache when available."""

    @pytest.fixture(scope="class")
    def app(self):
        from akarins_gateway.app import create_app
        return create_app()

    @pytest.mark.asyncio
    async def test_models_endpoint_returns_list(self, app):
        """GET /v1/models returns valid response structure even without cache."""
        import httpx
        from httpx import ASGITransport

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/v1/models")

        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "list"
        assert "data" in body

    @pytest.mark.asyncio
    async def test_models_endpoint_uses_cache_when_available(self, app):
        """When registry has cache, endpoint returns it directly."""
        from akarins_gateway.gateway.model_registry import get_model_registry
        from akarins_gateway.gateway import model_registry as mod

        # Create and populate a registry
        old_instance = mod._registry_instance
        try:
            registry = _make_registry()
            registry.initialize({
                "test-be": {"enabled": True, "models": ["cached-model-123"]},
            })
            registry._rebuild_cache()
            mod._registry_instance = registry

            import httpx
            from httpx import ASGITransport

            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/v1/models")

            assert response.status_code == 200
            body = response.json()
            model_ids = {m["id"] for m in body["data"]}
            assert "cached-model-123" in model_ids
        finally:
            mod._registry_instance = old_instance


# ===========================================================================
# 6. Integration Tests — Routing Fallback
# ===========================================================================

class TestRoutingIntegration:
    """Verify _is_model_supported_by_backend consults registry."""

    def test_registry_fallback_finds_dynamic_model(self):
        """Model not in YAML but discovered dynamically should be routable."""
        from akarins_gateway.gateway import model_registry as mod

        old_instance = mod._registry_instance
        try:
            registry = _make_registry()
            registry.initialize({})
            registry._initialized = True
            # Simulate dynamic discovery
            registry._dynamic_models["dynamic-only-model"] = {"zerogravity"}
            mod._registry_instance = registry

            from akarins_gateway.gateway.routing import _is_model_supported_by_backend

            # This model is only in the registry (dynamic), not in YAML
            # The function should find it via the registry fallback
            result = _is_model_supported_by_backend("zerogravity", "dynamic-only-model")
            assert result is True

        finally:
            mod._registry_instance = old_instance

    def test_registry_fallback_does_not_match_wrong_backend(self):
        """Dynamic model on backend A should not match backend B via registry."""
        from akarins_gateway.gateway import model_registry as mod

        old_instance = mod._registry_instance
        try:
            registry = _make_registry()
            registry.initialize({})
            registry._initialized = True
            registry._dynamic_models["dynamic-model-x"] = {"backend-alpha"}
            mod._registry_instance = registry

            # Mock is_backend_capable to return False so we isolate the registry check.
            # (By default is_backend_capable returns True for unknown backends.)
            with patch(
                "akarins_gateway.gateway.routing.is_backend_capable",
                return_value=False,
            ):
                from akarins_gateway.gateway.routing import _is_model_supported_by_backend

                # backend-beta is NOT in registry for dynamic-model-x
                result = _is_model_supported_by_backend("backend-beta", "dynamic-model-x")
                assert result is False

                # backend-alpha IS in registry for dynamic-model-x
                result2 = _is_model_supported_by_backend("backend-alpha", "dynamic-model-x")
                assert result2 is True

        finally:
            mod._registry_instance = old_instance


# ===========================================================================
# 7. Integration Tests — Config Reload Hook
# ===========================================================================

class TestConfigReloadHook:
    """reload_model_routing_config() triggers registry.reload_static()."""

    def test_reload_calls_registry_reload(self):
        """Verify the hook in config_loader calls reload_static."""
        from akarins_gateway.gateway import model_registry as mod

        old_instance = mod._registry_instance
        try:
            registry = _make_registry()
            registry._initialized = True
            registry.reload_static = MagicMock()
            mod._registry_instance = registry

            from akarins_gateway.gateway.config_loader import reload_model_routing_config
            reload_model_routing_config()

            registry.reload_static.assert_called_once()
        finally:
            mod._registry_instance = old_instance

    def test_reload_safe_when_registry_not_initialized(self):
        """If registry is not initialized, reload should not crash."""
        from akarins_gateway.gateway import model_registry as mod

        old_instance = mod._registry_instance
        try:
            registry = _make_registry()
            registry._initialized = False
            mod._registry_instance = registry

            from akarins_gateway.gateway.config_loader import reload_model_routing_config
            # Should not raise
            reload_model_routing_config()
        finally:
            mod._registry_instance = old_instance


# ===========================================================================
# 8. Concurrent Refresh Lock
# ===========================================================================

class TestConcurrentRefreshLock:
    """asyncio.Lock prevents refresh storms."""

    @pytest.mark.asyncio
    async def test_locked_refresh_is_skipped(self):
        """If refresh is already running, second call is a no-op."""
        registry = _make_registry()
        registry.initialize({})

        # Acquire the lock externally
        await registry._refresh_lock.acquire()
        try:
            # This should return immediately (skip refresh)
            await registry.refresh_all()
            # No error, no hang — that's the expected behavior
        finally:
            registry._refresh_lock.release()
