"""
TLS 指纹伪装模块 v3.0

支持多种 TLS 指纹伪装后端，按优先级使用：
1. curl_cffi (首选) - 支持 Chrome/Electron TLS 指纹，对齐 Antigravity-Manager v4.1.20
2. tls_client (降级) - 支持 Go 语言 TLS 指纹
3. httpx (最终降级) - 原生 Python，无指纹伪装

特性:
- Chrome/Electron TLS 指纹 (chrome131 等) - 模拟真实 Antigravity 桌面应用
- Go 客户端 TLS 指纹 (go_1_21 等) - 保留作为降级方案
- 自动检测可用后端
- 环境变量配置
- Electron 客户端身份头部集 (x-client-name, x-machine-id 等)

环境变量:
- TLS_IMPERSONATE_ENABLED: 是否启用 TLS 伪装 (默认: true)
- TLS_IMPERSONATE_TARGET: 伪装目标 (默认: chrome131)
- TLS_IMPERSONATE_BACKEND: 强制使用的后端 (可选: curl_cffi, tls_client, httpx)

作者: 浮浮酱 (Claude Opus 4.6)
日期: 2026-02-17
"""

import os
import random
import uuid
import pathlib
from typing import Optional, Dict, Any, List
from .log import log

# ====================== 后端可用性检测 ======================

# 尝试导入 tls_client (首选，支持 Go 指纹)
try:
    import tls_client
    TLS_CLIENT_AVAILABLE = True
    log.info("[TLS] tls_client 库已加载 (支持 Go TLS 指纹)")
except ImportError:
    TLS_CLIENT_AVAILABLE = False
    tls_client = None
    log.debug("[TLS] tls_client 库未安装")

# 尝试导入 curl_cffi (降级选项，仅支持浏览器指纹)
try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    from curl_cffi.requests import Session as CurlSession
    CURL_CFFI_AVAILABLE = True
    log.debug("[TLS] curl_cffi 库已加载 (支持浏览器 TLS 指纹)")
except ImportError:
    CURL_CFFI_AVAILABLE = False
    CurlAsyncSession = None
    CurlSession = None
    log.debug("[TLS] curl_cffi 库未安装")


# ====================== 配置 ======================

def _get_env_bool(key: str, default: bool = True) -> bool:
    """获取布尔类型环境变量"""
    value = os.getenv(key, str(default)).lower()
    return value in ("true", "1", "yes", "on")


def _get_env_str(key: str, default: str) -> str:
    """获取字符串类型环境变量"""
    return os.getenv(key, default)


# 配置项
TLS_IMPERSONATE_ENABLED = _get_env_bool("TLS_IMPERSONATE_ENABLED", True)
TLS_IMPERSONATE_TARGET = _get_env_str("TLS_IMPERSONATE_TARGET", "chrome131")
TLS_IMPERSONATE_BACKEND = _get_env_str("TLS_IMPERSONATE_BACKEND", "auto")


# ====================== 支持的伪装目标 ======================

# tls_client 支持的 Go 指纹
TLS_CLIENT_GO_TARGETS = [
    "go_1_21",  # Go 1.21 (推荐，最新稳定版)
    "go_1_20",  # Go 1.20
    "go_1_19",  # Go 1.19
]

# tls_client 支持的浏览器指纹
TLS_CLIENT_BROWSER_TARGETS = [
    "chrome_120", "chrome_119", "chrome_117", "chrome_116",
    "chrome_115", "chrome_114", "chrome_112", "chrome_111",
    "chrome_110", "chrome_109", "chrome_108", "chrome_107",
    "firefox_120", "firefox_117", "firefox_110",
    "safari_ios_17_0", "safari_16_0", "safari_15_6_1",
]

# curl_cffi 支持的浏览器指纹 (降级使用)
CURL_CFFI_TARGETS = [
    # Chrome 系列
    "chrome99", "chrome100", "chrome101", "chrome104", "chrome107",
    "chrome110", "chrome116", "chrome119", "chrome120", "chrome123",
    "chrome124", "chrome126", "chrome127", "chrome128", "chrome129",
    "chrome130", "chrome131",
    # Chrome Android
    "chrome99_android", "chrome131_android",
    # Safari 系列
    "safari15_3", "safari15_5", "safari17_0", "safari17_2_ios",
    "safari18_0", "safari18_0_ios",
    # Edge
    "edge99", "edge101",
    # Firefox (实验性)
    "firefox",
]

# 所有支持的目标
SUPPORTED_IMPERSONATE_TARGETS = (
    TLS_CLIENT_GO_TARGETS +
    TLS_CLIENT_BROWSER_TARGETS +
    CURL_CFFI_TARGETS
)

# 随机化目标池
RANDOMIZE_GO_TARGETS = ["go_1_21", "go_1_20", "go_1_19"]
RANDOMIZE_CHROME_TARGETS = ["chrome131", "chrome130", "chrome129"]


# ====================== 后端选择逻辑 ======================

def _determine_backend() -> str:
    """
    确定使用的 TLS 后端

    优先级：curl_cffi (Chrome) > tls_client (Go) > httpx (无指纹)
    """
    if not TLS_IMPERSONATE_ENABLED:
        return "httpx"

    if TLS_IMPERSONATE_BACKEND != "auto":
        if TLS_IMPERSONATE_BACKEND == "tls_client" and TLS_CLIENT_AVAILABLE:
            return "tls_client"
        elif TLS_IMPERSONATE_BACKEND == "curl_cffi" and CURL_CFFI_AVAILABLE:
            return "curl_cffi"
        elif TLS_IMPERSONATE_BACKEND == "httpx":
            return "httpx"
        log.warning(f"[TLS] 指定的后端 {TLS_IMPERSONATE_BACKEND} 不可用，自动选择")

    if CURL_CFFI_AVAILABLE:
        return "curl_cffi"

    if TLS_CLIENT_AVAILABLE:
        return "tls_client"

    return "httpx"


# 当前使用的后端
CURRENT_BACKEND = _determine_backend()


# ====================== 公共 API ======================

def is_tls_impersonate_available() -> bool:
    """检查 TLS 伪装是否可用"""
    return TLS_IMPERSONATE_ENABLED and CURRENT_BACKEND in ("tls_client", "curl_cffi")


def is_go_fingerprint_available() -> bool:
    """检查 Go TLS 指纹是否可用"""
    return TLS_CLIENT_AVAILABLE and TLS_IMPERSONATE_ENABLED


def get_current_backend() -> str:
    """获取当前使用的 TLS 后端"""
    return CURRENT_BACKEND


def get_impersonate_target(randomize: bool = False) -> str:
    """获取伪装目标"""
    if randomize:
        if CURRENT_BACKEND == "curl_cffi":
            return random.choice(RANDOMIZE_CHROME_TARGETS)
        elif CURRENT_BACKEND == "tls_client":
            return random.choice(RANDOMIZE_GO_TARGETS)

    target = TLS_IMPERSONATE_TARGET

    if CURRENT_BACKEND == "tls_client":
        if target.startswith("chrome") and not target.startswith("chrome_"):
            target = "chrome_120"
        elif target not in TLS_CLIENT_GO_TARGETS + TLS_CLIENT_BROWSER_TARGETS:
            target = "go_1_21"
    elif CURRENT_BACKEND == "curl_cffi":
        if target.startswith("go_"):
            target = "chrome131"
        elif target.startswith("chrome_"):
            target = "chrome131"

    return target


def get_supported_targets() -> List[str]:
    """获取支持的伪装目标列表"""
    return SUPPORTED_IMPERSONATE_TARGETS.copy()


# ====================== Go 客户端风格请求头 ======================

GO_CLIENT_HEADERS = {
    "accept-encoding": "gzip",
    "x-goog-api-client": "gl-go/1.21.0 grpc-go/1.59.0 gax/2.12.0 rest/1.64.0",
    "grpc-accept-encoding": "gzip",
}

# ====================== Electron/Chrome 客户端风格请求头 ======================


def _generate_machine_id() -> str:
    """
    生成或读取持久化的机器唯一标识

    模拟真实 Antigravity 桌面应用的 machine_uid::get() 行为。
    首次生成后持久化到 ~/.akarins-gateway/machine-id。
    """
    machine_id_dir = pathlib.Path.home() / ".akarins-gateway"
    machine_id_file = machine_id_dir / "machine-id"

    try:
        if machine_id_file.exists():
            stored_id = machine_id_file.read_text(encoding="utf-8").strip()
            if stored_id:
                return stored_id
    except Exception:
        pass

    # 生成新的 machine-id 并持久化
    new_id = str(uuid.uuid4())
    try:
        machine_id_dir.mkdir(parents=True, exist_ok=True)
        machine_id_file.write_text(new_id, encoding="utf-8")
        try:
            import stat
            machine_id_file.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600: owner only
        except (OSError, NotImplementedError):
            pass  # Windows 等平台可能不支持 chmod
        log.info(f"[TLS] 生成新的 machine-id: {new_id[:8]}...")
    except Exception as e:
        log.warning(f"[TLS] 无法持久化 machine-id: {e}")

    return new_id


# 模块级变量
SESSION_ID = str(uuid.uuid4())
MACHINE_ID = _generate_machine_id()

# 从 constants 导入统一的版本号和 User-Agent
from .constants import ANTIGRAVITY_VERSION
from .constants import ANTIGRAVITY_USER_AGENT

# Google API 客户端标识常量 - Node.js/Electron 风格
_X_GOOG_API_CLIENT = "gl-node/18.18.2 fire/0.8.6 grpc/1.10.x"

# Electron/Chrome 客户端身份头部集
ELECTRON_CLIENT_HEADERS = {
    "accept-encoding": "gzip, deflate, br",
    "x-client-name": "antigravity",
    "x-client-version": ANTIGRAVITY_VERSION,
    "x-machine-id": MACHINE_ID,
    "x-vscode-sessionid": SESSION_ID,
    "x-goog-api-client": _X_GOOG_API_CLIENT,
}

# Google 特有请求头
GOOGLE_API_HEADERS = {
    "x-goog-api-client": _X_GOOG_API_CLIENT,
}

# GeminiCLI 的 User-Agent
GEMINI_CLI_USER_AGENT = "GeminiCLI/0.1.5 (Windows; AMD64)"


def get_antigravity_headers(user_agent: Optional[str] = None, include_request_id: bool = True) -> Dict[str, str]:
    """
    获取 Antigravity Electron 客户端风格的请求头

    Args:
        user_agent: 自定义 User-Agent，默认使用 Antigravity UA
        include_request_id: 是否包含 requestId 头部

    Returns:
        Electron 风格的请求头字典
    """
    headers = ELECTRON_CLIENT_HEADERS.copy()
    headers["user-agent"] = user_agent or ANTIGRAVITY_USER_AGENT

    if include_request_id:
        headers["requestid"] = f"req-{uuid.uuid4()}"

    return headers


# 向后兼容别名
get_go_style_headers = get_antigravity_headers


def generate_request_id() -> str:
    """生成 Antigravity 风格的请求 ID"""
    return f"req-{uuid.uuid4()}"


# ====================== tls_client Session 管理 ======================

def get_tls_client_session(
    timeout_seconds: int = 30,
    **kwargs
) -> Optional["tls_client.Session"]:
    """获取 tls_client 的 Session 实例"""
    if not TLS_CLIENT_AVAILABLE or CURRENT_BACKEND != "tls_client":
        return None

    target = get_impersonate_target()

    return tls_client.Session(
        client_identifier=target,
        random_tls_extension_order=False,
        **kwargs
    )


# ====================== curl_cffi 兼容层 ======================

def get_curl_async_session(**kwargs) -> Optional["CurlAsyncSession"]:
    """获取 curl_cffi 的 AsyncSession 实例"""
    if not CURL_CFFI_AVAILABLE or CURRENT_BACKEND != "curl_cffi":
        return None

    if "impersonate" not in kwargs:
        kwargs["impersonate"] = get_impersonate_target()

    return CurlAsyncSession(**kwargs)


def get_curl_session(**kwargs) -> Optional["CurlSession"]:
    """获取 curl_cffi 的同步 Session 实例"""
    if not CURL_CFFI_AVAILABLE or CURRENT_BACKEND != "curl_cffi":
        return None

    if "impersonate" not in kwargs:
        kwargs["impersonate"] = get_impersonate_target()

    return CurlSession(**kwargs)


# ====================== 状态报告 ======================

def get_tls_status() -> Dict[str, Any]:
    """获取 TLS 伪装模块状态"""
    return {
        "tls_client_installed": TLS_CLIENT_AVAILABLE,
        "curl_cffi_installed": CURL_CFFI_AVAILABLE,
        "tls_impersonate_enabled": TLS_IMPERSONATE_ENABLED,
        "current_backend": CURRENT_BACKEND,
        "is_available": is_tls_impersonate_available(),
        "is_go_fingerprint": CURRENT_BACKEND == "tls_client",
        "current_target": get_impersonate_target() if is_tls_impersonate_available() else None,
        "supported_targets_count": len(SUPPORTED_IMPERSONATE_TARGETS),
    }


# 模块加载时输出状态
if __name__ != "__main__":
    status = get_tls_status()
    if status["is_available"]:
        if status["current_backend"] == "curl_cffi":
            backend_info = "Chrome/Electron TLS 指纹"
        elif status["is_go_fingerprint"]:
            backend_info = "Go TLS 指纹 (降级)"
        else:
            backend_info = "TLS 指纹"
        log.info(f"[TLS] TLS 伪装已启用，后端: {status['current_backend']} ({backend_info})，目标: {status['current_target']}")
    else:
        if not TLS_CLIENT_AVAILABLE and not CURL_CFFI_AVAILABLE:
            log.warning("[TLS] TLS 伪装不可用: tls_client 和 curl_cffi 均未安装")
        elif not TLS_IMPERSONATE_ENABLED:
            log.info("[TLS] TLS 伪装已禁用 (TLS_IMPERSONATE_ENABLED=false)")
