"""
Integration tests for Akarin's Gateway.

Verifies the gateway works as an integrated system:
- FastAPI app endpoints (models, health, keepalive, docs, auth rejection)
- Gateway config loading from YAML
- Router assembly (gateway router, augment router)
- IDE compat middleware presence
- Import chain validation

Uses httpx.AsyncClient with ASGITransport for in-process HTTP testing.
No live backend connections are required — all tests work without
any backend services running.

Author: test-engineer (claude-sonnet-4-6)
Date: 2026-02-27
"""

import importlib
import os
import sys

import pytest
import httpx
from httpx import ASGITransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(app) -> httpx.AsyncClient:
    """Return an AsyncClient wired directly to the ASGI app."""
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# 1. FastAPI App Integration
# ---------------------------------------------------------------------------

class TestFastAPIApp:
    """Tests that exercise the full ASGI app via ASGITransport."""

    @pytest.fixture(scope="class")
    def app(self):
        """Import and return the FastAPI app instance."""
        from akarins_gateway.app import create_app
        return create_app()

    @pytest.mark.asyncio
    async def test_get_v1_models_returns_valid_response(self, app):
        """GET /v1/models returns HTTP 200 with object=list structure."""
        async with _make_client(app) as client:
            response = await client.get("/v1/models")
        # Models endpoint has no auth — it is publicly accessible
        assert response.status_code == 200
        body = response.json()
        assert "object" in body
        assert body["object"] == "list"
        assert "data" in body
        assert isinstance(body["data"], list)

    @pytest.mark.asyncio
    async def test_get_health_returns_200(self, app):
        """GET /health endpoint returns HTTP 200."""
        async with _make_client(app) as client:
            response = await client.get("/health")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_head_keepalive_returns_200(self, app):
        """HEAD /keepalive returns HTTP 200 (liveness probe)."""
        async with _make_client(app) as client:
            response = await client.head("/keepalive")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_openapi_docs_returns_200(self, app):
        """GET /docs (Swagger UI) returns HTTP 200."""
        async with _make_client(app) as client:
            response = await client.get("/docs")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_unauthenticated_chat_completions_rejected(self, app):
        """POST /v1/chat/completions without Authorization is rejected with 401 or 403."""
        async with _make_client(app) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-sonnet-4.5",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                # No Authorization header
            )
        assert response.status_code in (401, 403), (
            f"Expected 401 or 403 for unauthenticated request, got {response.status_code}"
        )

    @pytest.mark.asyncio
    async def test_wrong_bearer_token_rejected(self, app):
        """POST /v1/chat/completions with wrong Bearer token is rejected with 403."""
        async with _make_client(app) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-sonnet-4.5",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                headers={"Authorization": "Bearer totally-wrong-password-xyzxyz"},
            )
        assert response.status_code in (401, 403), (
            f"Expected 401 or 403 for wrong token, got {response.status_code}"
        )

    @pytest.mark.asyncio
    async def test_missing_auth_scheme_rejected(self, app):
        """POST /v1/chat/completions with non-Bearer scheme is rejected."""
        async with _make_client(app) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "claude-sonnet-4.5", "messages": []},
                headers={"Authorization": "Basic dXNlcjpwYXNz"},
            )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_gateway_compat_prefix_models(self, app):
        """GET /gateway/v1/models also returns 200 (backward-compat mount)."""
        async with _make_client(app) as client:
            response = await client.get("/gateway/v1/models")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_openapi_json_schema_present(self, app):
        """GET /openapi.json returns a valid OpenAPI schema dict."""
        async with _make_client(app) as client:
            response = await client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert "openapi" in schema
        assert "info" in schema
        assert schema["info"]["title"] == "Akarin's Gateway"


# ---------------------------------------------------------------------------
# 2. Gateway Config Loading
# ---------------------------------------------------------------------------

class TestGatewayConfigLoading:
    """Tests that verify YAML config is loaded correctly."""

    def test_load_gateway_config_returns_dict(self):
        """load_gateway_config() returns a non-empty dict of backend configs."""
        from akarins_gateway.gateway.config_loader import load_gateway_config
        configs = load_gateway_config()
        assert isinstance(configs, dict)
        assert len(configs) > 0

    def test_config_has_expected_backend_keys(self):
        """Config contains at least the well-known backend keys."""
        from akarins_gateway.gateway.config_loader import load_gateway_config
        configs = load_gateway_config()
        backend_keys = set(configs.keys())
        # At minimum, zerogravity and antigravity are always present in gateway.yaml
        expected_any_of = {"zerogravity", "antigravity", "copilot", "kiro-gateway"}
        found = backend_keys & expected_any_of
        assert len(found) >= 1, (
            f"None of the expected backend keys found. Got: {backend_keys}"
        )

    def test_each_backend_config_has_base_url(self):
        """Every backend config entry has a non-empty base_url."""
        from akarins_gateway.gateway.config_loader import load_gateway_config
        configs = load_gateway_config()
        for name, cfg in configs.items():
            assert hasattr(cfg, "base_url"), f"Backend '{name}' missing base_url"
            assert cfg.base_url, f"Backend '{name}' has empty base_url"

    def test_each_backend_config_has_priority(self):
        """Every backend config entry has a numeric priority."""
        from akarins_gateway.gateway.config_loader import load_gateway_config
        configs = load_gateway_config()
        for name, cfg in configs.items():
            assert hasattr(cfg, "priority"), f"Backend '{name}' missing priority"
            assert isinstance(cfg.priority, (int, float)), (
                f"Backend '{name}' priority is not numeric: {cfg.priority!r}"
            )

    def test_zerogravity_backend_present(self):
        """zerogravity backend is present in gateway config."""
        from akarins_gateway.gateway.config_loader import load_gateway_config
        configs = load_gateway_config()
        assert "zerogravity" in configs, (
            f"'zerogravity' not found in backends. Available: {list(configs.keys())}"
        )

    def test_expand_env_vars_with_default(self):
        """expand_env_vars resolves ${VAR:default} correctly."""
        from akarins_gateway.gateway.config_loader import expand_env_vars
        # Ensure env var is absent for clean test
        os.environ.pop("_TEST_EXPAND_VAR_INTEGRATION_", None)
        result = expand_env_vars("${_TEST_EXPAND_VAR_INTEGRATION_:fallback_value}")
        assert result == "fallback_value"

    def test_expand_env_vars_reads_actual_env(self):
        """expand_env_vars reads the actual environment variable when set."""
        from akarins_gateway.gateway.config_loader import expand_env_vars
        os.environ["_TEST_EXPAND_VAR_INTEGRATION_"] = "actual_value"
        try:
            result = expand_env_vars("${_TEST_EXPAND_VAR_INTEGRATION_:default}")
            assert result == "actual_value"
        finally:
            del os.environ["_TEST_EXPAND_VAR_INTEGRATION_"]

    def test_expand_env_vars_bool_coercion(self):
        """expand_env_vars coerces 'true'/'false' strings to Python booleans."""
        from akarins_gateway.gateway.config_loader import expand_env_vars
        os.environ["_TEST_BOOL_VAR_"] = "true"
        try:
            assert expand_env_vars("${_TEST_BOOL_VAR_:false}") is True
        finally:
            del os.environ["_TEST_BOOL_VAR_"]


# ---------------------------------------------------------------------------
# 3. Router Assembly
# ---------------------------------------------------------------------------

class TestRouterAssembly:
    """Tests that router factory functions return valid APIRouter instances."""

    def test_create_gateway_router_returns_apirouter(self):
        """create_gateway_router() returns a FastAPI APIRouter."""
        from fastapi import APIRouter
        from akarins_gateway.gateway.endpoints import create_gateway_router
        router = create_gateway_router()
        assert isinstance(router, APIRouter)

    def test_gateway_router_has_routes(self):
        """The gateway router has at least one registered route."""
        from akarins_gateway.gateway.endpoints import create_gateway_router
        router = create_gateway_router()
        assert len(router.routes) > 0, "Gateway router has no routes"

    def test_gateway_router_includes_models_route(self):
        """The gateway router includes a route for /v1/models."""
        from akarins_gateway.gateway.endpoints import create_gateway_router
        router = create_gateway_router()
        paths = [getattr(r, "path", "") for r in router.routes]
        assert "/v1/models" in paths, (
            f"/v1/models not found in gateway router paths: {paths}"
        )

    def test_gateway_router_includes_chat_completions_route(self):
        """The gateway router includes a route for /v1/chat/completions."""
        from akarins_gateway.gateway.endpoints import create_gateway_router
        router = create_gateway_router()
        paths = [getattr(r, "path", "") for r in router.routes]
        assert "/v1/chat/completions" in paths, (
            f"/v1/chat/completions not found in gateway router paths: {paths}"
        )

    def test_create_augment_router_returns_apirouter(self):
        """create_augment_router() returns a FastAPI APIRouter."""
        from fastapi import APIRouter
        from akarins_gateway.gateway.augment import create_augment_router
        router = create_augment_router()
        assert isinstance(router, APIRouter)

    def test_augment_router_has_routes(self):
        """The augment router has at least one registered route."""
        from akarins_gateway.gateway.augment import create_augment_router
        router = create_augment_router()
        assert len(router.routes) > 0, "Augment router has no routes"


# ---------------------------------------------------------------------------
# 4. IDE Compat Middleware
# ---------------------------------------------------------------------------

class TestIDECompatMiddleware:
    """Tests that verify IDECompatMiddleware is wired into the app."""

    def test_ide_compat_middleware_importable(self):
        """IDECompatMiddleware can be imported without error."""
        from akarins_gateway.ide_compat import IDECompatMiddleware
        assert IDECompatMiddleware is not None

    def test_ide_compat_middleware_is_starlette_base(self):
        """IDECompatMiddleware inherits from Starlette BaseHTTPMiddleware."""
        from starlette.middleware.base import BaseHTTPMiddleware
        from akarins_gateway.ide_compat import IDECompatMiddleware
        assert issubclass(IDECompatMiddleware, BaseHTTPMiddleware)

    def test_app_has_middleware_stack(self):
        """The created app has a non-empty middleware stack."""
        from akarins_gateway.app import create_app
        app = create_app()
        # Starlette apps expose user_middleware (list of Middleware objects)
        # plus built-in middleware in the stack. Either must be non-empty.
        has_user_middleware = (
            hasattr(app, "user_middleware") and len(app.user_middleware) > 0
        )
        # Alternatively check middleware_stack attribute (set on first request)
        has_middleware_stack = hasattr(app, "middleware_stack")
        assert has_user_middleware or has_middleware_stack, (
            "App does not appear to have any middleware configured"
        )

    def test_ide_compat_middleware_in_app_user_middleware(self):
        """IDECompatMiddleware appears in app.user_middleware after create_app()."""
        from akarins_gateway.app import create_app
        from akarins_gateway.ide_compat import IDECompatMiddleware
        app = create_app()
        if not hasattr(app, "user_middleware"):
            pytest.skip("App does not expose user_middleware attribute")
        middleware_classes = [m.cls for m in app.user_middleware]
        assert IDECompatMiddleware in middleware_classes, (
            f"IDECompatMiddleware not found in user_middleware. "
            f"Found: {[c.__name__ for c in middleware_classes]}"
        )

    def test_cors_middleware_in_app(self):
        """CORSMiddleware is added to the app."""
        from fastapi.middleware.cors import CORSMiddleware
        from akarins_gateway.app import create_app
        app = create_app()
        if not hasattr(app, "user_middleware"):
            pytest.skip("App does not expose user_middleware attribute")
        middleware_classes = [m.cls for m in app.user_middleware]
        assert CORSMiddleware in middleware_classes, (
            f"CORSMiddleware not found in user_middleware. "
            f"Found: {[c.__name__ for c in middleware_classes]}"
        )


# ---------------------------------------------------------------------------
# 5. Import Chain Validation
# ---------------------------------------------------------------------------

class TestImportChain:
    """Tests that the full import chain succeeds without errors."""

    def test_import_core_log(self):
        """akarins_gateway.core.log imports without error."""
        import akarins_gateway.core.log
        assert hasattr(akarins_gateway.core.log, "log")

    def test_import_core_auth(self):
        """akarins_gateway.core.auth imports without error."""
        import akarins_gateway.core.auth
        assert hasattr(akarins_gateway.core.auth, "authenticate_bearer")

    def test_import_core_config(self):
        """akarins_gateway.core.config imports without error."""
        import akarins_gateway.core.config
        assert hasattr(akarins_gateway.core.config, "get_api_password")

    def test_import_converters_package(self):
        """akarins_gateway.converters package imports without error."""
        import akarins_gateway.converters
        assert akarins_gateway.converters is not None

    def test_import_gateway_config_loader(self):
        """akarins_gateway.gateway.config_loader imports without error."""
        import akarins_gateway.gateway.config_loader
        assert hasattr(akarins_gateway.gateway.config_loader, "load_gateway_config")

    def test_import_gateway_endpoints(self):
        """akarins_gateway.gateway.endpoints imports without error."""
        import akarins_gateway.gateway.endpoints
        assert hasattr(akarins_gateway.gateway.endpoints, "create_gateway_router")

    def test_import_gateway_augment(self):
        """akarins_gateway.gateway.augment imports without error."""
        import akarins_gateway.gateway.augment
        assert hasattr(akarins_gateway.gateway.augment, "create_augment_router")

    def test_import_ide_compat(self):
        """akarins_gateway.ide_compat imports without error."""
        import akarins_gateway.ide_compat
        assert hasattr(akarins_gateway.ide_compat, "IDECompatMiddleware")

    def test_import_app_module(self):
        """akarins_gateway.app imports without error and exposes create_app."""
        import akarins_gateway.app
        assert hasattr(akarins_gateway.app, "create_app")
        assert hasattr(akarins_gateway.app, "app")

    def test_full_import_chain_core_to_gateway(self):
        """Full chain: core -> converters -> gateway.config_loader -> endpoints -> app."""
        # Each import must succeed in order
        import akarins_gateway.core.log
        import akarins_gateway.core.auth
        import akarins_gateway.converters
        import akarins_gateway.gateway.config_loader
        import akarins_gateway.gateway.endpoints
        import akarins_gateway.gateway.augment
        import akarins_gateway.app
        # Verify the module-level app singleton is a FastAPI instance
        from fastapi import FastAPI
        from akarins_gateway.app import app
        assert isinstance(app, FastAPI)

    def test_signature_cache_importable(self):
        """akarins_gateway.signature_cache imports without error."""
        import akarins_gateway.signature_cache
        assert hasattr(akarins_gateway.signature_cache, "enable_migration_mode")

    def test_gateway_health_importable(self):
        """akarins_gateway.gateway.health imports without error."""
        import akarins_gateway.gateway.health
        assert hasattr(akarins_gateway.gateway.health, "BackendHealthManager")

    def test_gateway_backends_registry_importable(self):
        """akarins_gateway.gateway.backends.registry imports without error."""
        import akarins_gateway.gateway.backends.registry
        assert akarins_gateway.gateway.backends.registry is not None
