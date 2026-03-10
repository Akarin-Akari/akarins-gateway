"""
Gateway 后端模块

包含后端接口定义和具体实现。

作者: 浮浮酱 (Claude Opus 4.5)
创建日期: 2026-01-18
"""

__all__ = [
    "GatewayBackend",
    "BackendConfig",
    "GatewayRegistry",
    "AntigravityBackend",
    "AntigravityToolsBackend",  # 🆕 [2026-02-06] 独立外部服务后端（不受 ENABLE_ANTIGRAVITY 控制）
    "CopilotBackend",
    "ZeroGravityBackend",  # 🆕 [2026-02-22] ZeroGravity MITM Proxy
]


# 延迟导入避免循环依赖
def __getattr__(name: str):
    if name == "GatewayBackend":
        from .interface import GatewayBackend
        return GatewayBackend
    elif name == "BackendConfig":
        from .interface import BackendConfig
        return BackendConfig
    elif name == "GatewayRegistry":
        from .registry import GatewayRegistry
        return GatewayRegistry
    elif name == "AntigravityBackend":
        from .antigravity import AntigravityBackend
        return AntigravityBackend
    elif name == "AntigravityToolsBackend":
        # [REFACTOR 2026-02-28] AT 已从 antigravity/ 隔离区独立出来
        # 它是外部服务代理（端口 9046），不依赖 gcli2api，与 copilot/kiro 同级
        from .antigravity_tools import AntigravityToolsBackend
        return AntigravityToolsBackend
    elif name == "CopilotBackend":
        from .copilot import CopilotBackend
        return CopilotBackend
    elif name == "ZeroGravityBackend":  # 🆕 [2026-02-22]
        from .zerogravity import ZeroGravityBackend
        return ZeroGravityBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
