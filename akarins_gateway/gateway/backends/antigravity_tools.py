"""
Antigravity Tools 后端实现

实现 GatewayBackend Protocol，提供 Antigravity Tools 服务的代理功能。

Antigravity Tools 是一个独立的外部服务（端口 9046），不属于 gcli2api 也不属于
Antigravity 隔离区。它是一个纯粹的 httpx 代理后端，与 copilot.py / kiro.py 同级。

- 运行端口：9046
- API 格式：Anthropic Messages (/v1/messages?beta=true)
- 优先级：1.5（在 Antigravity 之后，Kiro Gateway 之前）
- 支持模型：Claude 全系列
- 独立于 ENABLE_ANTIGRAVITY 功能开关

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-02-06
重构日期: 2026-02-28 — 从 antigravity/ 隔离区移出，成为独立后端
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional, Set
import httpx
import json

from .interface import GatewayBackend, BackendConfig
from akarins_gateway.gateway.config import BACKENDS
from akarins_gateway.core.log import log
from akarins_gateway.core.httpx_client import safe_close_client

__all__ = ["AntigravityToolsBackend"]

# Antigravity Tools 支持的模型列表（Claude 全系列）
# ✅ [FIX 2026-02-08] 添加 Opus 4.6 系列支持
ANTIGRAVITY_TOOLS_SUPPORTED_MODELS: Set[str] = {
    "claude-sonnet-4.5", "claude-opus-4.5", "claude-haiku-4.5",
    "claude-sonnet-4", "claude-opus-4",
    # Opus 4.6 系列（新增）
    "claude-opus-4.6", "claude-opus-4-6", "claude-opus-4-6-thinking",
    # 带后缀的变体
    "claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5",
    "claude-sonnet-4-5-thinking", "claude-opus-4-5-thinking",
    # 带日期的变体
    "claude-opus-4-5-20251101", "claude-sonnet-4-5-20250514",
}


def is_antigravity_tools_supported(model: str) -> bool:
    """
    检查模型是否被 Antigravity Tools 支持

    使用模糊匹配逻辑，支持各种模型名称变体

    Args:
        model: 模型名称

    Returns:
        是否支持
    """
    if not model:
        return False

    model_lower = model.lower()

    # 精确匹配
    if model_lower in ANTIGRAVITY_TOOLS_SUPPORTED_MODELS:
        return True

    # 模糊匹配：检查是否包含 Claude 关键词
    if "claude" in model_lower:
        # 支持所有 Claude 模型
        return True

    return False


class AntigravityToolsBackend:
    """
    Antigravity Tools 后端实现

    这是一个独立的外部服务代理，不属于 Antigravity 隔离区。

    特性:
    1. 运行在 9046 端口
    2. 使用 Anthropic Messages 格式 (/v1/messages?beta=true)
    3. 作为自研 Antigravity 的 fallback
    4. 优先级：1.5（在 Antigravity 之后，Kiro Gateway 之前）
    5. 使用 httpx 代理到配置的 base_url
    6. 超时时间：timeout=120s, stream_timeout=600s
    7. 实现健康检查
    8. 从环境变量读取 ANTIGRAVITY_TOOLS_ENABLED 判断是否启用
    """

    def __init__(self, config: Optional[BackendConfig] = None):
        """
        初始化 Antigravity Tools 后端

        Args:
            config: 后端配置，如果为 None 则从 BACKENDS 加载
        """
        if config is None:
            backend_cfg = BACKENDS.get("antigravity-tools", {})
            config = BackendConfig(
                name=backend_cfg.get("name", "Antigravity Tools"),
                base_url=backend_cfg.get("base_url", "http://127.0.0.1:9046/v1"),
                priority=backend_cfg.get("priority", 1.5),  # 在 Antigravity (1) 之后
                models=list(ANTIGRAVITY_TOOLS_SUPPORTED_MODELS),
                enabled=backend_cfg.get("enabled", True),
                timeout=backend_cfg.get("timeout", 120.0),
                max_retries=backend_cfg.get("max_retries", 2),
            )

        self._config = config
        self._http_client: Optional[httpx.AsyncClient] = None
        self._stream_timeout = BACKENDS.get("antigravity-tools", {}).get("stream_timeout", 600.0)
        # Anthropic Messages 端点后缀
        self._endpoint_suffix = BACKENDS.get("antigravity-tools", {}).get("endpoint_suffix", "?beta=true")

    @property
    def name(self) -> str:
        """后端名称"""
        return self._config.name

    @property
    def config(self) -> BackendConfig:
        """后端配置"""
        return self._config

    async def is_available(self) -> bool:
        """
        检查后端是否可用

        通过健康检查端点验证服务状态

        Returns:
            是否可用
        """
        if not self._config.enabled:
            return False

        try:
            client = await self._get_http_client()
            # 尝试访问健康检查端点
            health_url = f"{self._config.base_url.rstrip('/v1')}/health"
            response = await client.get(health_url, timeout=5.0)
            return response.status_code == 200
        except Exception:
            # 如果 /health 端点不存在，尝试 /v1/models
            try:
                client = await self._get_http_client()
                models_url = f"{self._config.base_url}/models"
                response = await client.get(models_url, timeout=5.0)
                return response.status_code == 200
            except Exception as e:
                log.warning(f"Antigravity Tools backend health check failed: {e}", tag="GATEWAY")
                return False

    async def supports_model(self, model: str) -> bool:
        """
        检查是否支持指定模型

        使用 is_antigravity_tools_supported 函数进行智能匹配

        Args:
            model: 模型名称

        Returns:
            是否支持
        """
        return is_antigravity_tools_supported(model)

    async def handle_request(
        self,
        endpoint: str,
        body: dict,
        headers: dict,
        stream: bool = False
    ) -> Any:
        """
        处理请求

        使用 httpx 代理到 Antigravity Tools

        Args:
            endpoint: API 端点
            body: 请求体
            headers: 请求头
            stream: 是否流式响应

        Returns:
            响应对象
        """
        return await self._handle_proxy_request(endpoint, body, headers, stream)

    async def handle_streaming_request(
        self,
        endpoint: str,
        body: dict,
        headers: dict
    ) -> AsyncIterator[bytes]:
        """
        处理流式请求

        Args:
            endpoint: API 端点
            body: 请求体
            headers: 请求头

        Yields:
            响应数据块
        """
        async for chunk in self._handle_proxy_streaming_request(endpoint, body, headers):
            yield chunk

    async def _get_http_client(self) -> httpx.AsyncClient:
        """
        获取或创建 HTTP 客户端

        Returns:
            httpx.AsyncClient 实例
        """
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.timeout),
                follow_redirects=True,
            )
        return self._http_client

    def _build_url(self, endpoint: str) -> str:
        """
        构建请求 URL

        Antigravity Tools 使用 Anthropic Messages 格式:
        - /chat/completions -> /messages?beta=true
        - /messages -> /messages?beta=true

        Args:
            endpoint: 原始端点

        Returns:
            完整的请求 URL
        """
        base_url = self._config.base_url.rstrip("/")

        # 转换端点格式
        if endpoint == "/chat/completions":
            # OpenAI 格式转换为 Anthropic Messages 格式
            final_endpoint = f"/messages{self._endpoint_suffix}"
            log.info(
                f"[ANTIGRAVITY TOOLS] Converting endpoint: {endpoint} -> {final_endpoint}",
                tag="GATEWAY"
            )
        elif endpoint == "/messages" or endpoint.startswith("/messages"):
            # 已经是 Messages 格式，添加 beta 参数
            if "?" not in endpoint:
                final_endpoint = f"{endpoint}{self._endpoint_suffix}"
            else:
                final_endpoint = f"{endpoint}&beta=true"
        else:
            # 其他端点保持原样
            final_endpoint = endpoint

        return f"{base_url}{final_endpoint}"

    async def _handle_proxy_request(
        self,
        endpoint: str,
        body: dict,
        headers: dict,
        stream: bool
    ) -> Any:
        """
        使用 httpx 代理处理请求

        Args:
            endpoint: API 端点
            body: 请求体
            headers: 请求头
            stream: 是否流式响应

        Returns:
            响应对象
        """
        # 流式请求应该调用 handle_streaming_request
        if stream:
            raise ValueError("Stream requests should use handle_streaming_request")

        client = await self._get_http_client()
        url = self._build_url(endpoint)

        # 构建请求头（Anthropic 格式）
        request_headers = self._build_request_headers(headers)

        try:
            # 非流式请求
            response = await client.post(
                url,
                json=body,
                headers=request_headers,
                timeout=self._config.timeout,
            )
            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            log.error(f"Antigravity Tools proxy request failed: {e.response.status_code}", tag="GATEWAY")
            raise
        except Exception as e:
            log.error(f"Antigravity Tools proxy request failed: {e}", tag="GATEWAY")
            raise

    async def _handle_proxy_streaming_request(
        self,
        endpoint: str,
        body: dict,
        headers: dict
    ) -> AsyncIterator[bytes]:
        """
        使用 httpx 代理处理流式请求

        Args:
            endpoint: API 端点
            body: 请求体
            headers: 请求头

        Yields:
            响应数据块
        """
        client = await self._get_http_client()
        url = self._build_url(endpoint)

        # 构建请求头（Anthropic 格式）
        request_headers = self._build_request_headers(headers)

        try:
            # [DEBUG] 记录请求信息
            log.info(
                f"[ANTIGRAVITY TOOLS] Sending streaming request: url={url}, "
                f"messages_count={len(body.get('messages', []))}, "
                f"max_tokens={body.get('max_tokens', 'not_set')}",
                tag="GATEWAY"
            )

            chunk_count = 0
            total_bytes = 0

            async with client.stream(
                "POST",
                url,
                json=body,
                headers=request_headers,
                timeout=self._stream_timeout,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    chunk_count += 1
                    total_bytes += len(chunk)
                    yield chunk

            # [DEBUG] 记录响应统计
            log.info(
                f"[ANTIGRAVITY TOOLS] Streaming completed: chunk_count={chunk_count}, "
                f"total_bytes={total_bytes}",
                tag="GATEWAY"
            )

        except httpx.HTTPStatusError as e:
            log.error(f"Antigravity Tools proxy streaming request failed: {e.response.status_code}", tag="GATEWAY")
            error_msg = json.dumps({"error": f"Backend error: {e.response.status_code}"})
            yield f"data: {error_msg}\n\n".encode("utf-8")

        except Exception as e:
            log.error(f"Antigravity Tools proxy streaming request failed: {e}", tag="GATEWAY")
            error_msg = json.dumps({"error": str(e)})
            yield f"data: {error_msg}\n\n".encode("utf-8")

    def _build_request_headers(self, headers: dict) -> dict:
        """
        构建 Anthropic 格式的请求头

        Args:
            headers: 原始请求头

        Returns:
            处理后的请求头
        """
        # Anthropic 格式请求头
        request_headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",  # Anthropic API 版本
        }

        # 处理 Authorization
        auth = headers.get("authorization") or headers.get("Authorization", "")
        if auth:
            # 如果是 Bearer 格式，提取 API key
            if auth.startswith("Bearer "):
                api_key = auth[7:]
                request_headers["x-api-key"] = api_key
            else:
                request_headers["x-api-key"] = auth

        # 保留 User-Agent
        user_agent = headers.get("user-agent") or headers.get("User-Agent")
        if user_agent:
            request_headers["User-Agent"] = user_agent
            request_headers["X-Forwarded-User-Agent"] = user_agent

        # 转发特定的控制头
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
            "anthropic-beta",  # Anthropic beta 特性
        ):
            value = headers.get(h) or headers.get(h.title())
            if value:
                request_headers[h] = value

        return request_headers

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        if self._http_client is not None:
            await safe_close_client(self._http_client)
            self._http_client = None

    def __del__(self):
        """析构函数"""
        if self._http_client is not None:
            # 注意：在析构函数中无法使用 await
            # 建议显式调用 close() 方法
            pass
