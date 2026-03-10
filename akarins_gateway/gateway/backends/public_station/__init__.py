"""
Public Station Module - Unified abstraction for all public API stations.

This module provides a declarative configuration system for public stations
(Ruoli, dkapi, cifang, AnyRouter, etc.), eliminating scattered hardcoded
logic across proxy.py, routing.py, and config.py.

Adding a new public station:
    1. Add a PublicStationConfig entry in stations.py
    2. Add env vars to .env.example
    3. Add model routing entries in gateway.yaml
    Done! No proxy.py/routing.py/config.py changes needed.

Author: 浮浮酱 (Claude Opus 4.6)
Created: 2026-02-14
"""

from .base import PublicStationConfig, PublicStationBackend
from .manager import PublicStationManager

__all__ = [
    "PublicStationConfig",
    "PublicStationBackend",
    "PublicStationManager",
    "get_public_station_manager",
]

# Singleton instance
_manager: PublicStationManager | None = None


def get_public_station_manager() -> PublicStationManager:
    """Get the singleton PublicStationManager, initializing on first call."""
    global _manager
    if _manager is None:
        from .stations import PUBLIC_STATIONS
        _manager = PublicStationManager()
        for config in PUBLIC_STATIONS.values():
            _manager.register(config)
    return _manager
