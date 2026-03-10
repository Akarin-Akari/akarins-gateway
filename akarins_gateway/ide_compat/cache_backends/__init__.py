"""
缓存后端模块

提供不同的缓存后端实现：
- LRU (Least Recently Used) 内存缓存
- Redis 持久化缓存（预留）
"""

from .base import CacheBackend
from .lru_backend import LRUCacheBackend

__all__ = [
    "CacheBackend",
    "LRUCacheBackend",
]
