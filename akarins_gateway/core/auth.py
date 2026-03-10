"""
Authentication functions for the gateway.

Extracted from gcli2api/src/utils.py — contains:
- Bearer token authentication
- Gemini flexible authentication (x-goog-api-key / Bearer / URL key)
- SD-WebUI flexible authentication (Basic / Bearer)
- Panel token verification

All auth functions use SYNCHRONOUS config access (pure ENV, no database).
"""

import base64
import hmac
from typing import Optional

from fastapi import Depends, HTTPException, Header, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_api_password, get_panel_password
from .log import log

# HTTP Bearer security scheme
security = HTTPBearer()


# ====================== Authentication Functions ======================

def authenticate_bearer(
    authorization: Optional[str] = Header(None)
) -> str:
    """
    Bearer Token 认证

    此函数可以直接用作 FastAPI 的 Depends 依赖

    Args:
        authorization: Authorization 头部值（自动注入）

    Returns:
        验证通过的token

    Raises:
        HTTPException: 认证失败时抛出401或403异常

    使用示例:
        @router.post("/endpoint")
        async def endpoint(token: str = Depends(authenticate_bearer)):
            # token 已验证通过
            pass
    """

    password = get_api_password()

    # 检查是否提供了 Authorization 头
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 检查是否是 Bearer token
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme. Use 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 提取 token
    token = authorization[7:]  # 移除 "Bearer " 前缀

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 验证 token
    if not hmac.compare_digest(token, password):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="密码错误"
        )

    return token


def _is_local_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def authenticate_bearer_allow_local_dummy(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> str:
    """
    Bearer Token 认证（兼容本地 Bugment/Augment）。

    背景：部分客户端固定发送 `Authorization: Bearer dummy`，而网关口令可配置，
    重构后会导致入口直接 403。
    策略：仅对 localhost 请求放行 dummy token，其它保持原有严格校验。
    """

    password = get_api_password()

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme. Use 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[7:]
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = token.strip()

    if hmac.compare_digest(token, password):
        return token

    if _is_local_request(request):
        # Some hosts (e.g. Cursor) can end up sending placeholder strings if secret storage fails.
        # We only tolerate these placeholders for localhost requests.
        if token in ("dummy", "undefined", "null"):
            return "dummy"
        # IDE-integrated clients (VSCode/Cursor) run locally and may provide their own per-host token.
        # To avoid brittle coupling between the IDE's token storage and the gateway's API_PASSWORD,
        # accept any non-empty Bearer token for localhost requests coming from the Augment extension UA.
        user_agent_lower = (request.headers.get("user-agent") or request.headers.get("User-Agent") or "").lower()
        if "augment.vscode-augment/" in user_agent_lower:
            return token

    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent") or request.headers.get("User-Agent") or ""
    if token in ("dummy", "undefined", "null"):
        masked = token
    elif len(token) <= 8:
        masked = f"{token[:2]}***{token[-2:]}" if len(token) > 3 else "***"
    else:
        masked = f"{token[:4]}***{token[-4:]}"
    log.warning(
        f"[AUTH] Rejecting bearer token (client_ip={client_ip}, ua={user_agent}, token={masked})",
        tag="AUTH",
    )

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="密码错误")


def authenticate_gemini_flexible(
    request: Request,
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    key: Optional[str] = Query(None)
) -> str:
    """
    Gemini 灵活认证：支持 x-goog-api-key 头部、URL 参数 key 或 Authorization Bearer

    此函数可以直接用作 FastAPI 的 Depends 依赖

    Args:
        request: FastAPI Request 对象
        x_goog_api_key: x-goog-api-key 头部值（自动注入）
        key: URL 参数 key（自动注入）

    Returns:
        验证通过的API密钥

    Raises:
        HTTPException: 认证失败时抛出400异常
    """

    password = get_api_password()

    # 尝试从URL参数key获取（Google官方标准方式）
    if key:
        log.debug("Using URL parameter key authentication")
        if hmac.compare_digest(key, password):
            return key

    # 尝试从Authorization头获取（兼容旧方式）
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]  # 移除 "Bearer " 前缀
        log.debug("Using Bearer token authentication")
        if hmac.compare_digest(token, password):
            return token

    # 尝试从x-goog-api-key头获取（新标准方式）
    if x_goog_api_key:
        log.debug("Using x-goog-api-key authentication")
        if hmac.compare_digest(x_goog_api_key, password):
            return x_goog_api_key

    _safe_key = f"{key[:4]}***" if key and len(key) > 4 else "***" if key else "None"
    log.error(f"Authentication failed. client_ip={request.client.host if request.client else 'unknown'}, key={_safe_key}")
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Missing or invalid authentication. Use 'key' URL parameter, 'x-goog-api-key' header, or 'Authorization: Bearer <token>'",
    )


def authenticate_sdwebui_flexible(request: Request) -> str:
    """
    SD-WebUI 灵活认证：支持 Authorization Basic/Bearer

    此函数可以直接用作 FastAPI 的 Depends 依赖

    Args:
        request: FastAPI Request 对象

    Returns:
        验证通过的密码

    Raises:
        HTTPException: 认证失败时抛出403异常
    """

    password = get_api_password()

    # 尝试从 Authorization 头获取
    auth_header = request.headers.get("authorization")

    if auth_header:
        # 支持 Bearer token 认证
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]  # 移除 "Bearer " 前缀
            log.debug("Using Bearer token authentication")
            if hmac.compare_digest(token, password):
                return token

        # 支持 Basic 认证
        elif auth_header.startswith("Basic "):
            try:
                # 解码 Base64
                encoded_credentials = auth_header[6:]  # 移除 "Basic " 前缀
                decoded_bytes = base64.b64decode(encoded_credentials)
                decoded_str = decoded_bytes.decode('utf-8')

                # Basic 认证格式: username:password 或者只有 password
                if ':' in decoded_str:
                    _, pwd = decoded_str.split(':', 1)
                else:
                    pwd = decoded_str

                log.debug("Using Basic authentication, credentials decoded successfully")
                if hmac.compare_digest(pwd, password):
                    return pwd
            except Exception as e:
                log.error(f"Failed to decode Basic auth: {e}")

    log.error(f"SD-WebUI authentication failed. client_ip={request.client.host if request.client else 'unknown'}")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Missing or invalid authentication. Use 'Authorization: Basic <base64>' or 'Bearer <token>'",
    )


# ====================== Panel Authentication Functions ======================

def verify_panel_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    简化的控制面板密码验证函数

    直接验证Bearer token是否等于控制面板密码

    Args:
        credentials: HTTPAuthorizationCredentials 自动注入

    Returns:
        验证通过的token

    Raises:
        HTTPException: 密码错误时抛出401异常
    """

    password = get_panel_password()
    if not hmac.compare_digest(credentials.credentials, password):
        raise HTTPException(status_code=401, detail="密码错误")
    return credentials.credentials
