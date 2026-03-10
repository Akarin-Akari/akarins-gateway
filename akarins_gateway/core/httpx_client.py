"""
通用的HTTP客户端模块 v3.2

为所有需要使用HTTP请求的模块提供统一的客户端配置和方法。
支持多种 TLS 指纹伪装后端，按优先级使用：
1. curl_cffi (首选) - 支持 Chrome/Electron TLS 指纹，对齐 Antigravity-Manager v4.1.20
2. tls_client (降级) - 支持 Go 语言 TLS 指纹
3. httpx (最终降级) - 原生 Python，无指纹伪装

特性:
- Chrome/Electron TLS 指纹伪装：使用 curl_cffi 模拟真实 Antigravity 桌面应用
- Go TLS 指纹伪装：使用 tls_client 模拟 Go net/http 客户端（降级选项）
- Electron 客户端身份头部：x-client-name, x-machine-id, x-vscode-sessionid 等
- 优雅降级：所有库不可用时回退到原生 httpx
- 统一接口：无论使用哪个后端，API 保持一致
- 代理支持：支持动态代理配置
- 流式请求：支持 SSE 流式响应
- 连接池优化：减少 TLS 握手开销，提高高并发性能

版本历史:
- v1.0: 原始版本，使用原生 httpx
- v2.0 (2026-01-21): 添加 curl_cffi TLS 指纹伪装支持
- v3.0 (2026-02-02): 添加 tls_client 支持，优先使用 Go TLS 指纹
- v3.1 (2026-02-03): 连接池参数优化，减少 TLS 握手开销
- v3.2 (2026-02-17): 反转优先级，Chrome 指纹优先；支持 Electron 客户端身份头部

作者: 浮浮酱 (Claude Opus 4.6)
日期: 2026-02-17
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional, Union
import asyncio
from functools import partial, wraps
import time
import random
import ssl

import httpx

from .config import get_proxy_config
from .log import log


# ====================== 网络层鲁棒性增强 (Phase 1) ======================
# [FIX 2026-02-03] 增强网络异常处理，防止 WinError 10054 导致 event loop 崩溃

# OS 级网络重置异常（Windows 10054, Linux ECONNRESET 等）
NETWORK_RESET_ERRORS = (
    ConnectionResetError,      # WinError 10054 / ECONNRESET
    BrokenPipeError,           # EPIPE
    ConnectionAbortedError,    # ECONNABORTED
    ConnectionRefusedError,    # ECONNREFUSED
)

# 网络层重试配置
NETWORK_RETRY_CONFIG = {
    "max_retries": 3,           # 最大重试次数
    "base_delay": 1.0,          # 基础延迟（秒）
    "max_delay": 30.0,          # 最大延迟（秒）
    "jitter": 0.5,              # 随机抖动系数 (0-1)
}


def _calculate_retry_delay(attempt: int, config: dict = None) -> float:
    """
    计算指数退避延迟（带随机抖动）

    公式: delay = min(base * 2^attempt + random_jitter, max_delay)
    """
    cfg = config or NETWORK_RETRY_CONFIG
    base_delay = cfg["base_delay"] * (2 ** attempt)
    jitter = random.uniform(0, cfg["jitter"] * base_delay)
    return min(base_delay + jitter, cfg["max_delay"])


# ====================== 连接池优化配置 (Phase 4) ======================
# [FIX 2026-02-03] 优化连接池参数，减少 TLS 握手开销
# 参考: https://www.python-httpx.org/advanced/#pool-limit-configuration

# 全局连接池限制配置
CONNECTION_POOL_LIMITS = httpx.Limits(
    max_keepalive_connections=20,   # 最大保持活跃的连接数（复用连接）
    max_connections=100,            # 最大总连接数
    keepalive_expiry=120.0,         # 连接保持活跃的时间（秒）- 增加到 2 分钟
)

# 默认超时配置（区分连接超时和读取超时）
DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0,   # 连接超时：10 秒
    read=30.0,      # 读取超时：30 秒
    write=30.0,     # 写入超时：30 秒
    pool=30.0,      # 连接池等待超时：30 秒
)

# 流式请求的超时配置（更长的读取超时）
STREAMING_TIMEOUT = httpx.Timeout(
    connect=15.0,   # 连接超时：15 秒
    read=600.0,     # 读取超时：10 分钟（适合 thinking 模型）
    write=30.0,     # 写入超时：30 秒
    pool=30.0,      # 连接池等待超时：30 秒
)

# 连接池状态监控
_pool_stats = {
    "created_at": time.time(),
    "total_requests": 0,
    "reused_connections": 0,
    "new_connections": 0,
    "last_log_time": 0,
}

# 导入 TLS 伪装模块
from .tls_impersonate import (
    is_tls_impersonate_available,
    is_go_fingerprint_available,
    get_current_backend,
    get_impersonate_target,
    get_antigravity_headers,
    get_go_style_headers,  # 向后兼容别名
    get_tls_client_session,
    CURL_CFFI_AVAILABLE,
    TLS_CLIENT_AVAILABLE,
)

# 条件导入 tls_client
if TLS_CLIENT_AVAILABLE:
    import tls_client
else:
    tls_client = None

# 条件导入 curl_cffi
if CURL_CFFI_AVAILABLE:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
else:
    CurlAsyncSession = None


# ====================== tls_client 异步包装器 ======================

class TlsClientResponseWrapper:
    """
    tls_client Response 的兼容性包装器

    tls_client 的 Response 对象缺少 httpx.Response 的一些方法，
    这个包装器添加缺失的方法以保持 API 兼容性。
    """

    def __init__(self, response):
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def text(self) -> str:
        return self._response.text

    @property
    def content(self) -> bytes:
        return self._response.content

    @property
    def headers(self):
        return self._response.headers

    def json(self):
        return self._response.json()

    def raise_for_status(self):
        """
        兼容 httpx.Response.raise_for_status()

        如果状态码表示错误 (4xx 或 5xx)，抛出异常
        """
        if 400 <= self.status_code < 600:
            raise httpx.HTTPStatusError(
                message=f"HTTP {self.status_code}",
                request=None,
                response=self,
            )

    def __getattr__(self, name):
        """代理其他属性到原始 Response"""
        return getattr(self._response, name)


class TlsClientAsyncWrapper:
    """
    tls_client 的异步包装器

    tls_client 是同步库，这个包装器使用 run_in_executor 将同步调用
    转换为异步调用，使其可以在异步上下文中使用。

    [FIX 2026-02-03] 增强版：
    - 添加硬超时保护，防止 executor 线程池耗尽
    - 捕获网络重置异常，防止 event loop 崩溃
    """

    # 默认硬超时（秒）- 防止 executor 线程池耗尽
    DEFAULT_HARD_TIMEOUT = 120.0

    def __init__(self, session: "tls_client.Session"):
        self._session = session
        self._loop = None

    def _get_loop(self):
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.get_event_loop()
        return self._loop

    async def _execute_with_timeout(self, method: str, url: str, **kwargs) -> TlsClientResponseWrapper:
        """
        带超时保护的请求执行

        [FIX 2026-02-03] 防止 executor 线程池耗尽导致 event loop 阻塞
        """
        loop = self._get_loop()
        session_method = getattr(self._session, method)

        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(None, partial(session_method, url, **kwargs)),
                timeout=self.DEFAULT_HARD_TIMEOUT
            )
            return TlsClientResponseWrapper(response)

        except asyncio.TimeoutError:
            log.error(
                f"[TLS_CLIENT] 请求超时 ({self.DEFAULT_HARD_TIMEOUT}s)，"
                f"可能是 executor 线程池耗尽: {method.upper()} {url}"
            )
            raise

        except NETWORK_RESET_ERRORS as e:
            # [FIX 2026-02-03] 捕获网络重置异常，记录日志但继续抛出
            log.warning(f"[TLS_CLIENT] 网络连接重置 ({type(e).__name__}): {url}")
            raise

    async def get(self, url: str, **kwargs) -> TlsClientResponseWrapper:
        """异步 GET 请求"""
        return await self._execute_with_timeout("get", url, **kwargs)

    async def post(self, url: str, **kwargs) -> TlsClientResponseWrapper:
        """异步 POST 请求"""
        return await self._execute_with_timeout("post", url, **kwargs)

    async def put(self, url: str, **kwargs) -> TlsClientResponseWrapper:
        """异步 PUT 请求"""
        return await self._execute_with_timeout("put", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> TlsClientResponseWrapper:
        """异步 DELETE 请求"""
        return await self._execute_with_timeout("delete", url, **kwargs)

    async def patch(self, url: str, **kwargs) -> TlsClientResponseWrapper:
        """异步 PATCH 请求"""
        return await self._execute_with_timeout("patch", url, **kwargs)

    @property
    def headers(self):
        return self._session.headers

    @headers.setter
    def headers(self, value):
        self._session.headers = value


class HttpxClientManager:
    """
    通用HTTP客户端管理器 v3.2

    支持多种 TLS 指纹伪装后端和优雅降级。

    [FIX 2026-02-17] 优先级反转：Chrome/Electron 指纹优先，对齐 Antigravity-Manager v4.1.20

    使用优先级:
    1. curl_cffi (首选) - 提供 Chrome/Electron TLS 指纹，对齐上游 Antigravity 桌面应用
    2. tls_client (降级) - 提供 Go TLS 指纹伪装
    3. httpx (最终降级) - 原生 Python HTTP 客户端
    """

    def __init__(self):
        """初始化客户端管理器"""
        self._current_backend = get_current_backend()
        self._use_tls_client = self._current_backend == "tls_client"
        self._use_curl_cffi = self._current_backend == "curl_cffi"
        self._logged_init = False

    def _log_init_once(self):
        """只在第一次使用时记录初始化日志"""
        if not self._logged_init:
            if self._use_curl_cffi:
                target = get_impersonate_target()
                log.info(f"[HttpxClient] Chrome/Electron TLS 伪装已启用，后端: curl_cffi，目标: {target}")
            elif self._use_tls_client:
                target = get_impersonate_target()
                log.info(f"[HttpxClient] Go TLS 伪装已启用 (降级)，后端: tls_client，目标: {target}")
            else:
                log.debug("[HttpxClient] 使用原生 httpx（TLS 伪装不可用）")
            self._logged_init = True

    async def get_client_kwargs(self, timeout: float = 30.0, **kwargs) -> Dict[str, Any]:
        """
        获取httpx客户端的通用配置参数

        Args:
            timeout: 请求超时时间（秒）
            **kwargs: 其他参数

        Returns:
            客户端配置字典
        """
        # [FIX 2026-02-03] 使用优化的超时配置
        # 如果传入的是 float，转换为 httpx.Timeout 对象
        if isinstance(timeout, (int, float)):
            timeout_config = httpx.Timeout(
                connect=min(timeout, 15.0),  # 连接超时不超过 15 秒
                read=timeout,
                write=30.0,
                pool=30.0,
            )
        else:
            timeout_config = timeout  # 已经是 Timeout 对象

        client_kwargs = {
            "timeout": timeout_config,
            "limits": CONNECTION_POOL_LIMITS,  # [FIX 2026-02-03] 使用全局连接池限制
            **kwargs
        }

        # 同步读取代理配置（纯 ENV，无需 await）
        current_proxy_config = get_proxy_config()
        if current_proxy_config:
            client_kwargs["proxy"] = current_proxy_config

        return client_kwargs

    @asynccontextmanager
    async def get_client(
        self, timeout: float = 30.0, use_electron_headers: bool = False,
        use_go_headers: bool = False, **kwargs
    ) -> AsyncGenerator[Union[httpx.AsyncClient, "CurlAsyncSession", "tls_client.Session"], None]:
        """
        获取配置好的异步HTTP客户端

        [FIX 2026-02-17] 新增 use_electron_headers 参数，注入 Electron 客户端身份头部
        use_go_headers 保留为向后兼容别名，功能与 use_electron_headers 相同

        Args:
            timeout: 请求超时时间（秒）
            use_electron_headers: 是否注入 Electron 客户端身份头部（Antigravity 路由使用）
            use_go_headers: 向后兼容别名，等同于 use_electron_headers
            **kwargs: 其他参数

        Yields:
            HTTP 客户端实例（tls_client.Session、curl_cffi.AsyncSession 或 httpx.AsyncClient）
        """
        # use_go_headers 是向后兼容别名
        inject_headers = use_electron_headers or use_go_headers

        self._log_init_once()

        if self._use_tls_client:
            # 使用 tls_client (Go TLS 指纹 - 降级)
            async for client in self._get_tls_client(timeout, inject_headers, **kwargs):
                yield client
        elif self._use_curl_cffi:
            # 使用 curl_cffi 的 AsyncSession (Chrome/Electron TLS 指纹)
            async for client in self._get_curl_client(timeout, inject_headers, **kwargs):
                yield client
        else:
            # 降级到原生 httpx
            async for client in self._get_httpx_client(timeout, **kwargs):
                yield client

    async def _get_tls_client(
        self, timeout: float, inject_headers: bool, **kwargs
    ) -> AsyncGenerator[TlsClientAsyncWrapper, None]:
        """
        获取 tls_client 客户端 (Go TLS 指纹 - 降级方案)

        注意：tls_client 是同步库，通过 TlsClientAsyncWrapper 包装为异步接口
        [FIX 2026-02-17] inject_headers 现在注入 Electron 客户端身份头部（而非旧的 Go 风格头部）
        """
        # 同步读取代理配置（纯 ENV，无需 await）
        proxy_config = get_proxy_config()

        # 准备请求头
        headers = kwargs.pop("headers", {})
        if inject_headers:
            # [FIX 2026-02-17] 注入 Electron 客户端身份头部（get_go_style_headers 已是别名）
            electron_headers = get_antigravity_headers()
            # Electron 头部优先级较低，允许被调用方覆盖
            headers = {**electron_headers, **headers}

        # 创建 tls_client session
        session = tls_client.Session(
            client_identifier=get_impersonate_target(),
            random_tls_extension_order=False,  # 保持一致性，更像真实客户端
        )

        # 设置代理
        if proxy_config:
            session.proxies = {"http": proxy_config, "https": proxy_config}

        # 设置默认超时
        # [FIX 2026-02-03] 修复 Timeout 对象类型转换错误
        # timeout 可能是 float、int 或 httpx.Timeout 对象
        if hasattr(timeout, 'connect'):
            # httpx.Timeout 对象，取其中最大的超时值
            timeout_val = max(
                getattr(timeout, 'connect', 30) or 30,
                getattr(timeout, 'read', 30) or 30,
                getattr(timeout, 'write', 30) or 30,
                getattr(timeout, 'pool', 30) or 30,
            )
            session.timeout_seconds = int(timeout_val)
        else:
            session.timeout_seconds = int(timeout)

        # 设置默认请求头
        if headers:
            session.headers.update(headers)

        # 使用异步包装器
        wrapper = TlsClientAsyncWrapper(session)

        try:
            yield wrapper
        finally:
            # tls_client 没有 close 方法，但我们可以清理
            pass

    async def _get_curl_client(
        self, timeout: float, inject_headers: bool, **kwargs
    ) -> AsyncGenerator["CurlAsyncSession", None]:
        """
        获取 curl_cffi 客户端 (Chrome/Electron TLS 指纹 - 首选方案)

        [FIX 2026-02-17] 更新为 Electron 客户端身份头部注入
        """
        # 同步读取代理配置（纯 ENV，无需 await）
        proxy_config = get_proxy_config()

        # curl_cffi 的代理格式
        proxies = None
        if proxy_config:
            proxies = {"http": proxy_config, "https": proxy_config}

        # 准备请求头
        headers = kwargs.pop("headers", {})
        if inject_headers:
            # [FIX 2026-02-17] 注入 Electron 客户端身份头部
            electron_headers = get_antigravity_headers()
            # Electron 头部优先级较低，允许被调用方覆盖
            headers = {**electron_headers, **headers}

        async with CurlAsyncSession(
            impersonate=get_impersonate_target(),
            timeout=timeout,
            proxies=proxies,
            headers=headers,
            **kwargs
        ) as session:
            yield session

    async def _get_httpx_client(
        self, timeout: float, **kwargs
    ) -> AsyncGenerator[httpx.AsyncClient, None]:
        """获取原生 httpx 客户端（降级模式）"""
        client_kwargs = await self.get_client_kwargs(timeout=timeout, **kwargs)

        async with httpx.AsyncClient(**client_kwargs) as client:
            yield client

    @asynccontextmanager
    async def get_streaming_client(
        self, timeout: float = 600.0, **kwargs
    ) -> AsyncGenerator[Union[httpx.AsyncClient, "CurlAsyncSession"], None]:
        """
        获取用于流式请求的HTTP客户端

        默认超时 600 秒（10分钟），适合 thinking 模型的长时间思考。
        如果需要无限等待，可以显式传入 timeout=None

        Args:
            timeout: 请求超时时间（秒），默认 600 秒
            **kwargs: 其他参数

        Yields:
            HTTP 客户端实例
        """
        self._log_init_once()

        if self._use_curl_cffi:
            # curl_cffi 流式客户端
            # 同步读取代理配置（纯 ENV，无需 await）
            proxy_config = get_proxy_config()
            proxies = None
            if proxy_config:
                proxies = {"http": proxy_config, "https": proxy_config}

            session = CurlAsyncSession(
                impersonate=get_impersonate_target(),
                timeout=timeout,
                proxies=proxies,
                **kwargs
            )
            try:
                yield session
            finally:
                await session.close()
        else:
            # httpx 流式客户端
            client_kwargs = await self.get_client_kwargs(timeout=timeout, **kwargs)
            client = httpx.AsyncClient(**client_kwargs)
            try:
                yield client
            finally:
                await safe_close_client(client)


# 全局HTTP客户端管理器实例
http_client = HttpxClientManager()


# ====================== 通用的异步方法 ======================

async def get_async(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0, **kwargs
) -> httpx.Response:
    """
    通用异步GET请求

    注意：当使用 curl_cffi 时，返回的是 curl_cffi.Response，
    但 API 兼容 httpx.Response
    """
    async with http_client.get_client(timeout=timeout, **kwargs) as client:
        return await client.get(url, headers=headers)


async def post_async(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
    **kwargs,
) -> httpx.Response:
    """通用异步POST请求"""
    async with http_client.get_client(timeout=timeout, **kwargs) as client:
        return await client.post(url, data=data, json=json, headers=headers)


async def put_async(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
    **kwargs,
) -> httpx.Response:
    """通用异步PUT请求"""
    async with http_client.get_client(timeout=timeout, **kwargs) as client:
        return await client.put(url, data=data, json=json, headers=headers)


async def delete_async(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0, **kwargs
) -> httpx.Response:
    """通用异步DELETE请求"""
    async with http_client.get_client(timeout=timeout, **kwargs) as client:
        return await client.delete(url, headers=headers)


# ====================== 错误处理装饰器 ======================

def handle_http_errors(func):
    """
    HTTP错误处理装饰器（增强版）

    [FIX 2026-02-03] 增强网络层鲁棒性：
    - 捕获 ConnectionResetError (WinError 10054) 等 OS 级网络异常
    - 自动指数退避重试
    - 防止单个连接错误导致 event loop 崩溃
    """

    @wraps(func)
    async def wrapper(*args, **kwargs):
        max_retries = NETWORK_RETRY_CONFIG["max_retries"]

        for attempt in range(max_retries + 1):
            try:
                response = await func(*args, **kwargs)
                response.raise_for_status()
                return response

            except NETWORK_RESET_ERRORS as e:
                # [FIX 2026-02-03] 网络连接重置，进行指数退避重试
                if attempt >= max_retries:
                    log.error(
                        f"[HTTPX] 网络连接重置，已达最大重试次数 ({max_retries}): {type(e).__name__}: {e}"
                    )
                    raise

                delay = _calculate_retry_delay(attempt)
                log.warning(
                    f"[HTTPX] 检测到连接重置 ({type(e).__name__}), "
                    f"第 {attempt + 1}/{max_retries} 次重试, 延迟 {delay:.1f}s"
                )
                await asyncio.sleep(delay)
                continue

            except httpx.RemoteProtocolError as e:
                # [FIX 2026-02-03] 服务器返回非标准 HTTP 流
                if attempt >= max_retries:
                    log.error(f"[HTTPX] 协议错误，已达最大重试次数: {e}")
                    raise

                delay = _calculate_retry_delay(attempt)
                log.warning(
                    f"[HTTPX] 协议错误 (RemoteProtocolError), "
                    f"第 {attempt + 1}/{max_retries} 次重试, 延迟 {delay:.1f}s: {e}"
                )
                await asyncio.sleep(delay)
                continue

            except ssl.SSLError as e:
                # [FIX 2026-02-03] SSL/TLS 错误（证书问题等）
                log.error(f"[HTTPX] SSL 错误（不重试）: {e}")
                raise

            except asyncio.TimeoutError as e:
                # [FIX 2026-02-03] 超时错误
                if attempt >= max_retries:
                    log.error(f"[HTTPX] 请求超时，已达最大重试次数: {e}")
                    raise

                delay = _calculate_retry_delay(attempt)
                log.warning(
                    f"[HTTPX] 请求超时, 第 {attempt + 1}/{max_retries} 次重试, 延迟 {delay:.1f}s"
                )
                await asyncio.sleep(delay)
                continue

            except httpx.HTTPStatusError as e:
                log.error(f"HTTP错误: {e.response.status_code} - {e.response.text}")
                raise

            except httpx.RequestError as e:
                # 其他请求错误（DNS 解析失败、连接失败等）
                if attempt >= max_retries:
                    log.error(f"[HTTPX] 请求错误，已达最大重试次数: {e}")
                    raise

                delay = _calculate_retry_delay(attempt)
                log.warning(
                    f"[HTTPX] 请求错误 ({type(e).__name__}), "
                    f"第 {attempt + 1}/{max_retries} 次重试, 延迟 {delay:.1f}s"
                )
                await asyncio.sleep(delay)
                continue

            except Exception as e:
                # curl_cffi 的错误处理
                error_msg = str(e)
                if "status_code" in error_msg or "HTTP" in error_msg:
                    log.error(f"HTTP错误: {e}")
                else:
                    log.error(f"未知错误: {e}")
                raise

        # 不应该到达这里，但为了安全
        raise RuntimeError("Unexpected retry loop exit")

    return wrapper


# ====================== 应用错误处理的安全方法 ======================

@handle_http_errors
async def safe_get_async(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0, **kwargs
) -> httpx.Response:
    """安全的异步GET请求（自动错误处理）"""
    return await get_async(url, headers=headers, timeout=timeout, **kwargs)


@handle_http_errors
async def safe_post_async(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
    **kwargs,
) -> httpx.Response:
    """安全的异步POST请求（自动错误处理）"""
    return await post_async(url, data=data, json=json, headers=headers, timeout=timeout, **kwargs)


@handle_http_errors
async def safe_put_async(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
    **kwargs,
) -> httpx.Response:
    """安全的异步PUT请求（自动错误处理）"""
    return await put_async(url, data=data, json=json, headers=headers, timeout=timeout, **kwargs)


@handle_http_errors
async def safe_delete_async(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0, **kwargs
) -> httpx.Response:
    """安全的异步DELETE请求（自动错误处理）"""
    return await delete_async(url, headers=headers, timeout=timeout, **kwargs)


# ====================== 流式请求支持 ======================

class StreamingContext:
    """流式请求上下文管理器"""

    def __init__(self, client: httpx.AsyncClient, stream_context):
        self.client = client
        self.stream_context = stream_context
        self.response = None

    async def __aenter__(self):
        self.response = await self.stream_context.__aenter__()
        return self.response

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.stream_context:
                await self.stream_context.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            if self.client:
                await safe_close_client(self.client)


@asynccontextmanager
async def get_streaming_post_context(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 600.0,
    **kwargs,
) -> AsyncGenerator[StreamingContext, None]:
    """
    获取流式POST请求的上下文管理器

    默认超时 600 秒（10分钟），适合 thinking 模型的长时间思考

    注意：当使用 curl_cffi 时，流式处理方式略有不同
    """
    async with http_client.get_streaming_client(timeout=timeout, **kwargs) as client:
        stream_ctx = client.stream("POST", url, data=data, json=json, headers=headers)
        streaming_context = StreamingContext(client, stream_ctx)
        yield streaming_context


async def create_streaming_client_with_kwargs(**kwargs) -> Union[httpx.AsyncClient, "CurlAsyncSession"]:
    """
    创建用于流式处理的独立客户端实例（手动管理生命周期）

    警告：调用者必须确保调用 safe_close_client() 来释放资源
    建议使用 get_streaming_client() 上下文管理器代替此方法

    默认超时 600 秒（10分钟），适合 thinking 模型的长时间思考
    如果调用方需要无限等待，可以显式传入 timeout=None
    """
    timeout = kwargs.pop('timeout', 600.0)

    if http_client._use_curl_cffi:
        # curl_cffi 客户端
        # 同步读取代理配置（纯 ENV，无需 await）
        proxy_config = get_proxy_config()
        proxies = None
        if proxy_config:
            proxies = {"http": proxy_config, "https": proxy_config}

        return CurlAsyncSession(
            impersonate=get_impersonate_target(),
            timeout=timeout,
            proxies=proxies,
            **kwargs
        )
    else:
        # httpx 客户端
        client_kwargs = await http_client.get_client_kwargs(timeout=timeout, **kwargs)
        return httpx.AsyncClient(**client_kwargs)


async def safe_close_client(client: Union[httpx.AsyncClient, "CurlAsyncSession", Any]) -> None:
    """
    [FIX 2026-01-22] 安全地关闭 HTTP 客户端

    兼容两种客户端类型：
    - httpx.AsyncClient: 使用 aclose() 方法
    - curl_cffi.AsyncSession: 使用 close() 方法

    [FIX 2026-01-23] 增强错误处理：
    - 检查客户端状态，避免重复关闭
    - 捕获 curl_cffi 的 C 类型错误（客户端已关闭时）

    Args:
        client: HTTP 客户端实例（可能是 httpx.AsyncClient 或 CurlAsyncSession）
    """
    if client is None:
        return

    try:
        # 检查是否是 curl_cffi 的 AsyncSession
        if CURL_CFFI_AVAILABLE and isinstance(client, CurlAsyncSession):
            # curl_cffi 使用 close() 方法
            # [FIX 2026-01-23] 检查客户端是否已关闭
            if hasattr(client, "close"):
                # 检查客户端内部状态，避免关闭已关闭的客户端
                try:
                    # 尝试访问客户端属性来判断是否已关闭
                    # curl_cffi 客户端关闭后，某些内部属性会变为 None
                    if hasattr(client, "_session") and client._session is None:
                        return  # 客户端已关闭，无需再次关闭
                except (AttributeError, TypeError):
                    pass  # 无法检查状态，继续尝试关闭

                try:
                    await client.close()
                except (TypeError, AttributeError, ValueError) as e:
                    # [FIX 2026-01-23] 捕获 curl_cffi 的 C 类型错误
                    # 错误信息通常包含 "cdata pointer" 或 "NoneType" 或 "initializer"
                    error_str = str(e).lower()
                    if any(keyword in error_str for keyword in ["cdata", "nonetype", "initializer", "void *"]):
                        # 客户端已关闭或处于无效状态，忽略错误
                        log.debug(f"[HttpxClient] Client already closed or invalid: {e}")
                        return
                    raise  # 其他错误继续抛出
                except Exception as e:
                    # [FIX 2026-01-23] 捕获其他可能的 curl_cffi 错误
                    error_str = str(e).lower()
                    if any(keyword in error_str for keyword in ["cdata", "nonetype", "initializer", "void *", "closed"]):
                        log.debug(f"[HttpxClient] Client already closed or invalid: {e}")
                        return
                    # 其他未知错误也忽略，避免影响主流程
                    log.debug(f"[HttpxClient] Ignoring error during client close: {e}")
                    return
        # 检查是否是 httpx 的 AsyncClient
        elif isinstance(client, httpx.AsyncClient):
            # httpx 使用 aclose() 方法
            if hasattr(client, "aclose"):
                # [FIX 2026-01-23] 检查 httpx 客户端是否已关闭
                try:
                    # httpx 客户端关闭后，_transport 会变为 None
                    if hasattr(client, "_transport") and client._transport is None:
                        return  # 客户端已关闭，无需再次关闭
                except (AttributeError, TypeError):
                    pass  # 无法检查状态，继续尝试关闭

                try:
                    await client.aclose()
                except (RuntimeError, AttributeError) as e:
                    # httpx 客户端已关闭时会抛出 RuntimeError
                    error_str = str(e).lower()
                    if "closed" in error_str or "not open" in error_str:
                        log.debug(f"[HttpxClient] Client already closed: {e}")
                        return
                    raise  # 其他错误继续抛出
        else:
            # 尝试通用方法：先尝试 aclose，再尝试 close
            if hasattr(client, "aclose"):
                try:
                    await client.aclose()
                except Exception:
                    # 如果 aclose 失败，尝试 close
                    if hasattr(client, "close"):
                        await client.close()
            elif hasattr(client, "close"):
                await client.close()
    except Exception as e:
        # [FIX 2026-01-23] 更详细的错误处理
        error_str = str(e).lower()
        # 忽略常见的"已关闭"错误，包括 curl_cffi 的 C 类型错误
        if any(keyword in error_str for keyword in ["closed", "cdata", "nonetype", "initializer", "void *", "not open"]):
            log.debug(f"[HttpxClient] Client already closed or invalid: {e}")
        else:
            # [FIX 2026-01-23] 将警告降级为调试，避免日志噪音
            # 这些错误通常是客户端已关闭或处于无效状态，不影响主流程
            log.debug(f"[HttpxClient] Error closing client (ignored): {e}")


# ====================== TLS 状态查询 ======================

def get_http_client_status() -> Dict[str, Any]:
    """
    获取 HTTP 客户端状态

    [FIX 2026-02-17] 修复：正确报告 tls_client 后端（之前仅区分 curl_cffi/httpx）

    Returns:
        状态信息字典
    """
    from .tls_impersonate import get_tls_status
    tls_status = get_tls_status()

    # 准确报告当前后端
    if http_client._use_curl_cffi:
        backend = "curl_cffi"
    elif http_client._use_tls_client:
        backend = "tls_client"
    else:
        backend = "httpx"

    return {
        "backend": backend,
        "tls_impersonate": tls_status,
    }
