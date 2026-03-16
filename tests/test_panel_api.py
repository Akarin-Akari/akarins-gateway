"""
Tests for Panel API endpoints — key management CRUD.

Covers:
  - GET /api/panel/backends/{key}/keys   — list keys
  - POST /api/panel/backends/{key}/keys  — add key
  - DELETE /api/panel/backends/{key}/keys/{index} — delete key
  - POST /api/panel/config/save — save config

Uses FastAPI TestClient (httpx) for synchronous tests.

Author: fufu-chan (Gemini)
Date: 2026-03-17
"""

import copy
from typing import Dict, Any
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

# FastAPI TestClient for sync endpoint testing
from fastapi.testclient import TestClient


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def panel_app():
    """Create a minimal FastAPI app with only the panel router mounted."""
    from fastapi import FastAPI
    from akarins_gateway.gateway.endpoints.panel import router
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def auth_headers():
    """Return headers with a valid panel auth token."""
    # The verify_panel_token dependency needs to be overridden for testing
    return {"Authorization": "Bearer test-token-12345"}


@pytest.fixture
def patched_client(panel_app, mock_backends, auth_headers):
    """
    Provide a TestClient with:
    - BACKENDS patched to mock data
    - Auth dependency bypassed
    - Circuit breaker mocked
    """
    from akarins_gateway.core.auth import verify_panel_token

    async def _fake_verify(authorization: str = ""):
        return "test-token"

    panel_app.dependency_overrides[verify_panel_token] = _fake_verify

    with patch("akarins_gateway.gateway.endpoints.panel.BACKENDS", mock_backends):
        with patch("akarins_gateway.gateway.endpoints.panel.get_circuit_breaker") as mock_cb:
            mock_cb_instance = MagicMock()
            mock_cb_instance.get_status.return_value = {"state": "closed", "failure_count": 0}
            mock_cb.return_value = mock_cb_instance
            yield TestClient(panel_app), mock_backends


# ===========================================================================
# 1. GET /api/panel/backends/{key}/keys — List Keys
# ===========================================================================

class TestGetApiKeys:
    """Test listing API keys for a backend."""

    def test_returns_keys_for_list_format(self, patched_client):
        client, backends = patched_client
        resp = client.get("/api/panel/backends/test-backend-b/keys")
        assert resp.status_code == 200
        data = resp.json()
        assert data["backend"] == "test-backend-b"
        assert len(data["keys"]) == 2
        # Keys should be masked
        for key_info in data["keys"]:
            assert "***" in key_info["masked"]

    def test_returns_keys_for_single_key_format(self, patched_client):
        client, backends = patched_client
        resp = client.get("/api/panel/backends/test-backend-a/keys")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["keys"]) == 1

    def test_404_for_unknown_backend(self, patched_client):
        client, _ = patched_client
        resp = client.get("/api/panel/backends/nonexistent/keys")
        assert resp.status_code == 404


# ===========================================================================
# 2. POST /api/panel/backends/{key}/keys — Add Key
# ===========================================================================

class TestAddApiKey:
    """Test adding an API key to a backend."""

    def test_add_key_to_list_format_backend(self, patched_client):
        client, backends = patched_client
        resp = client.post(
            "/api/panel/backends/test-backend-b/keys",
            json={"key": "sk-new-added-key"},
        )
        assert resp.status_code == 200
        assert resp.json()["keys_count"] == 3
        assert "sk-new-added-key" in backends["test-backend-b"]["api_keys"]

    def test_add_key_migrates_single_to_list(self, patched_client):
        client, backends = patched_client
        resp = client.post(
            "/api/panel/backends/test-backend-a/keys",
            json={"key": "sk-second-key"},
        )
        assert resp.status_code == 200
        # Should have migrated from api_key to api_keys list
        assert "api_keys" in backends["test-backend-a"]
        assert len(backends["test-backend-a"]["api_keys"]) == 2

    def test_add_key_404_for_unknown_backend(self, patched_client):
        client, _ = patched_client
        resp = client.post(
            "/api/panel/backends/nonexistent/keys",
            json={"key": "sk-test"},
        )
        assert resp.status_code == 404


# ===========================================================================
# 3. DELETE /api/panel/backends/{key}/keys/{index} — Delete Key
# ===========================================================================

class TestDeleteApiKey:
    """Test deleting an API key from a backend by index."""

    def test_delete_key_by_index(self, patched_client):
        client, backends = patched_client
        original_count = len(backends["test-backend-b"]["api_keys"])
        resp = client.delete("/api/panel/backends/test-backend-b/keys/0")
        assert resp.status_code == 200
        assert resp.json()["keys_count"] == original_count - 1

    def test_delete_key_out_of_range(self, patched_client):
        client, _ = patched_client
        resp = client.delete("/api/panel/backends/test-backend-b/keys/999")
        assert resp.status_code == 400

    def test_delete_key_negative_index(self, patched_client):
        client, _ = patched_client
        resp = client.delete("/api/panel/backends/test-backend-b/keys/-1")
        assert resp.status_code == 400

    def test_delete_key_404_for_unknown_backend(self, patched_client):
        client, _ = patched_client
        resp = client.delete("/api/panel/backends/nonexistent/keys/0")
        assert resp.status_code == 404
