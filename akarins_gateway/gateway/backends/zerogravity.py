"""
ZeroGravity Gateway 后端实现

实现 GatewayBackend Protocol，提供 ZeroGravity MITM Proxy 的代理功能。

ZeroGravity 是基于 Rust 的 MITM 代理，通过 Antigravity 官方 LS Binary 提供 API：
- 支持模型：Claude Opus/Sonnet 4.6, Gemini 3.1 Pro, Gemini 3 Flash
- 优先级：0（最高优先级 — 流量指纹与真实 Antigravity 几乎不可区分）
- 默认端口：8880
- 接口格式：OpenAI 兼容（/v1/chat/completions）

作者: 浮浮酱 (Claude Opus 4.6)
创建日期: 2026-02-20
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional
import httpx
import json

from .interface import GatewayBackend, BackendConfig
from akarins_gateway.gateway.config import BACKENDS
from akarins_gateway.core.log import log
from akarins_gateway.core.httpx_client import safe_close_client

__all__ = ["ZeroGravityBackend"]


# ==================== Model Mapping ====================
# gcli2api model name -> ZeroGravity model name
# ZG uses simplified names (e.g., "opus-4.6" instead of "claude-opus-4.6")

ZEROGRAVITY_MODEL_MAPPING: Dict[str, str] = {
    # Claude 4.6 series
    "claude-opus-4.6": "opus-4.6",
    "claude-opus-4-6": "opus-4.6",
    "claude-sonnet-4.6": "sonnet-4.6",
    "claude-sonnet-4-6": "sonnet-4.6",
    # Gemini 3.1 Pro series (upstream v1.2.0 — replaces sunset gemini-3-pro)
    "gemini-3.1-pro": "gemini-3.1-pro",
    "gemini-3.1-pro-high": "gemini-3.1-pro-high",
    "gemini-3.1-pro-low": "gemini-3.1-pro-low",
    # Gemini 3 Flash
    "gemini-3-flash": "gemini-3-flash",
}

# All models supported by ZeroGravity
ZEROGRAVITY_SUPPORTED_MODELS: List[str] = list(ZEROGRAVITY_MODEL_MAPPING.keys())


def is_zerogravity_supported(model: str) -> bool:
    """Check if a model is supported by ZeroGravity."""
    return model in ZEROGRAVITY_MODEL_MAPPING


def map_model_for_zerogravity(model: str) -> str:
    """Map a gcli2api model name to ZeroGravity's model name."""
    return ZEROGRAVITY_MODEL_MAPPING.get(model, model)


class ZeroGravityBackend:
    """
    ZeroGravity MITM Proxy 后端实现

    特性:
    1. 最高优先级（priority: 0）— 流量指纹不可区分
    2. 支持 Claude 4.6/4.5 + Gemini 3 全系列
    3. OpenAI 兼容接口（/v1/chat/completions）
    4. 自动模型名映射（claude-opus-4.6 -> opus-4.6）
    5. 健康检查：/health 端点
    6. 通过 HTTPS_PROXY + 官方 LS Binary 实现 MITM
    """

    def __init__(self, config: Optional[BackendConfig] = None):
        """
        Initialize ZeroGravity backend.

        Args:
            config: Backend configuration. If None, loads from BACKENDS.
        """
        if config is None:
            backend_cfg = BACKENDS.get("zerogravity", {})
            config = BackendConfig(
                name=backend_cfg.get("name", "ZeroGravity"),
                base_url=backend_cfg.get("base_url", "http://127.0.0.1:8880/v1"),
                priority=backend_cfg.get("priority", 0),
                models=ZEROGRAVITY_SUPPORTED_MODELS,
                enabled=backend_cfg.get("enabled", True),
                timeout=backend_cfg.get("timeout", 120.0),
                max_retries=backend_cfg.get("max_retries", 2),
            )

        self._config = config
        self._http_client: Optional[httpx.AsyncClient] = None
        self._stream_timeout = BACKENDS.get("zerogravity", {}).get("stream_timeout", 600.0)

    @property
    def name(self) -> str:
        """Backend name."""
        return self._config.name

    @property
    def config(self) -> BackendConfig:
        """Backend configuration."""
        return self._config

    async def is_available(self) -> bool:
        """
        Check if ZeroGravity is available via health endpoint.

        Returns:
            True if ZG is running and healthy.
        """
        if not self._config.enabled:
            return False

        if not self._config.models:
            log.debug("ZeroGravity has no models configured", tag="GATEWAY")
            return False

        try:
            client = await self._get_http_client()
            # ZeroGravity exposes /health at the root (not under /v1)
            base = self._config.base_url.rstrip("/v1").rstrip("/")
            health_url = f"{base}/health"
            response = await client.get(health_url, timeout=5.0)
            return response.status_code == 200
        except Exception:
            # Fallback: try /v1/models
            try:
                client = await self._get_http_client()
                models_url = f"{self._config.base_url}/models"
                response = await client.get(models_url, timeout=5.0)
                return response.status_code == 200
            except Exception as e:
                log.warning(f"ZeroGravity health check failed: {e}", tag="GATEWAY")
                return False

    async def supports_model(self, model: str) -> bool:
        """
        Check if the given model is supported.

        Args:
            model: Model name (gcli2api format).

        Returns:
            True if supported.
        """
        return is_zerogravity_supported(model)

    async def handle_request(
        self,
        endpoint: str,
        body: dict,
        headers: dict,
        stream: bool = False
    ) -> Any:
        """
        Handle a non-streaming request.

        Args:
            endpoint: API endpoint (e.g., "/chat/completions").
            body: Request body.
            headers: Request headers.
            stream: Whether streaming is requested.

        Returns:
            Response JSON.
        """
        if stream:
            raise ValueError("Stream requests should use handle_streaming_request")

        return await self._proxy_request(endpoint, body, headers)

    async def handle_streaming_request(
        self,
        endpoint: str,
        body: dict,
        headers: dict
    ) -> AsyncIterator[bytes]:
        """
        Handle a streaming request.

        Args:
            endpoint: API endpoint.
            body: Request body.
            headers: Request headers.

        Yields:
            Response data chunks.
        """
        async for chunk in self._proxy_streaming_request(endpoint, body, headers):
            yield chunk

    # ─── Internal helpers ─────────────────────────────────────────────

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.timeout),
                follow_redirects=True,
            )
        return self._http_client

    def _build_headers(self, headers: dict) -> dict:
        """Build proxy request headers."""
        request_headers = {
            "Content-Type": "application/json",
        }

        # ZeroGravity doesn't require auth (local service),
        # but forward it if present for compatibility
        auth = headers.get("authorization") or headers.get("Authorization")
        if auth:
            request_headers["Authorization"] = auth

        # Forward User-Agent
        user_agent = headers.get("user-agent") or headers.get("User-Agent")
        if user_agent:
            request_headers["User-Agent"] = user_agent

        # Forward gateway control headers
        for h in (
            "x-augment-client",
            "x-bugment-client",
            "x-augment-request",
            "x-bugment-request",
            "x-signature-version",
            "x-signature-timestamp",
            "x-signature-signature",
            "x-signature-vector",
            "x-disable-thinking-signature",
            "x-request-id",
        ):
            value = headers.get(h) or headers.get(h.title())
            if value:
                request_headers[h] = value

        return request_headers

    def _map_request_body(self, body: dict) -> dict:
        """
        Map the request body for ZeroGravity.

        - Translates model names (claude-opus-4.6 -> opus-4.6)
        - Preserves all other fields as-is (OpenAI compatible)
        """
        mapped = {**body}  # Immutable: create new dict

        # Map model name
        model = mapped.get("model", "")
        mapped["model"] = map_model_for_zerogravity(model)

        return mapped

    async def _proxy_request(
        self,
        endpoint: str,
        body: dict,
        headers: dict,
    ) -> Any:
        """Proxy a non-streaming request to ZeroGravity."""
        client = await self._get_http_client()
        url = f"{self._config.base_url}{endpoint}"
        request_headers = self._build_headers(headers)
        mapped_body = self._map_request_body(body)

        try:
            log.info(
                f"[ZG BACKEND] Request: url={url}, "
                f"model={body.get('model')} -> {mapped_body.get('model')}, "
                f"messages={len(body.get('messages', []))}",
                tag="GATEWAY"
            )

            response = await client.post(
                url,
                json=mapped_body,
                headers=request_headers,
                timeout=self._config.timeout,
            )
            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            log.error(
                f"[ZG BACKEND] Request failed: {e.response.status_code}",
                tag="GATEWAY"
            )
            raise
        except Exception as e:
            log.error(f"[ZG BACKEND] Request failed: {e}", tag="GATEWAY")
            raise

    async def _proxy_streaming_request(
        self,
        endpoint: str,
        body: dict,
        headers: dict,
    ) -> AsyncIterator[bytes]:
        """Proxy a streaming request to ZeroGravity."""
        client = await self._get_http_client()
        url = f"{self._config.base_url}{endpoint}"
        request_headers = self._build_headers(headers)
        mapped_body = self._map_request_body(body)

        # Ensure stream=true in the body
        mapped_body["stream"] = True

        try:
            log.info(
                f"[ZG BACKEND] Streaming: url={url}, "
                f"model={body.get('model')} -> {mapped_body.get('model')}, "
                f"messages={len(body.get('messages', []))}",
                tag="GATEWAY"
            )

            chunk_count = 0
            total_bytes = 0

            async with client.stream(
                "POST",
                url,
                json=mapped_body,
                headers=request_headers,
                timeout=self._stream_timeout,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    chunk_count += 1
                    total_bytes += len(chunk)
                    yield chunk

            log.info(
                f"[ZG BACKEND] Stream complete: "
                f"chunks={chunk_count}, bytes={total_bytes}",
                tag="GATEWAY"
            )

        except httpx.HTTPStatusError as e:
            log.error(
                f"[ZG BACKEND] Stream failed: {e.response.status_code}",
                tag="GATEWAY"
            )
            raise
        except Exception as e:
            log.error(f"[ZG BACKEND] Stream failed: {e}", tag="GATEWAY")
            raise

    async def close(self):
        """Close the HTTP client."""
        if self._http_client is not None:
            await safe_close_client(self._http_client)
            self._http_client = None
