"""
Antigravity Backend — Isolated Module

All Antigravity-related backend code (gcli2api 依赖) is consolidated here
for easy removal in the future. Controlled by ENABLE_ANTIGRAVITY feature flag.

NOTE: AntigravityToolsBackend 已从此隔离区移出，成为独立后端
(backends/antigravity_tools.py)。它是一个外部服务代理（端口 9046），
不依赖 gcli2api，无需受 ENABLE_ANTIGRAVITY 控制。

When ENABLE_ANTIGRAVITY=false (default), AntigravityBackend is replaced with
a None stub so that the rest of the gateway can operate without any
Antigravity/gcli2api dependencies.

To completely remove Antigravity support:
  1. Delete this entire directory (backends/antigravity/)
  2. Remove AntigravityBackend from backends/__init__.py
  3. Remove any ENABLE_ANTIGRAVITY references in config

Author: fufu-chan (Claude Opus 4.6)
Date: 2026-02-27
Refactored: 2026-02-28 — AntigravityToolsBackend 独立化
"""

import os

__all__ = [
    "ENABLE_ANTIGRAVITY",
    "AntigravityBackend",
]

# Feature flag — default OFF
ENABLE_ANTIGRAVITY = os.getenv("ENABLE_ANTIGRAVITY", "false").lower() in ("true", "1", "yes", "on")

if ENABLE_ANTIGRAVITY:
    from .backend import AntigravityBackend
else:
    # Stub when Antigravity is disabled
    AntigravityBackend = None  # type: ignore[assignment,misc]
