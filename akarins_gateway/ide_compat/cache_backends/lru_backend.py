"""
LRU (Least Recently Used) 缓存后端实现

使用 OrderedDict 实现高性能的 LRU 缓存，支持线程安全操作。
"""

from collections import OrderedDict
from typing import List, Dict, Optional
import time
import logging
import threading

from .base import CacheBackend

log = logging.getLogger("gcli2api.history_cache.lru")


class LRUCacheBackend(CacheBackend):
    """
    LRU (Least Recently Used) 内存缓存实现
    
    特性：
    - 内存存储（快速访问，O(1)查找）
    - LRU淘汰策略（自动清理最久未使用的条目）
    - 线程安全（使用 threading.Lock）
    - 无需外部依赖
    
    适用场景：
    - 单实例部署
    - 开发/测试环境
    - 快速迭代
    
    性能指标：
    - 查找时间：O(1)
    - 存储时间：O(1)
    - 内存占用：取决于 max_size 和消息大小
    
    注意事项：
    - 服务重启后数据会丢失
    - 不适合多实例部署（缓存不共享）
    - 内存占用需要监控
    """
    
    def __init__(self, max_size: int = 1000):
        """
        初始化 LRU 缓存
        
        Args:
            max_size: 最大缓存条目数（默认1000）
                     建议值：100-1000（取决于可用内存）
        """
        self.cache: OrderedDict[str, Dict] = OrderedDict()
        self.max_size = max_size
        self._lock = threading.Lock()
        
        log.info(f"[LRU CACHE] 初始化完成 - max_size={max_size}")
    
    def store(self, scid: str, history: List[Dict]) -> None:
        """
        存储完整历史
        
        LRU策略：
        1. 如果缓存已满，删除最久未使用的条目
        2. 添加新条目到末尾（标记为最近使用）
        3. 如果SCID已存在，更新并移动到末尾
        """
        with self._lock:
            # LRU淘汰：如果满了，删除最旧的
            if scid not in self.cache and len(self.cache) >= self.max_size:
                oldest_scid, oldest_entry = self.cache.popitem(last=False)
                log.debug(
                    f"[LRU CACHE] LRU淘汰 - SCID: {oldest_scid[:8]}... "
                    f"({oldest_entry['message_count']} 消息)"
                )
            
            # 存储条目（包含元数据）
            entry = {
                "history": history,
                "message_count": len(history),
                "updated_at": time.time(),
                "created_at": time.time() if scid not in self.cache else self.cache[scid].get("created_at", time.time())
            }
            
            self.cache[scid] = entry
            
            # 移动到最后（标记为最近使用）
            self.cache.move_to_end(scid)
            
            log.debug(
                f"[LRU CACHE] 存储成功 - SCID: {scid[:8]}... "
                f"({len(history)} 消息, 缓存数: {len(self.cache)}/{self.max_size})"
            )
    
    def get(self, scid: str) -> Optional[List[Dict]]:
        """
        获取完整历史
        
        LRU策略：
        - 访问时将条目移动到末尾（更新"最近使用"时间）
        """
        with self._lock:
            entry = self.cache.get(scid)
            
            if not entry:
                log.debug(f"[LRU CACHE] 缓存未命中 - SCID: {scid[:8]}...")
                return None
            
            # 移动到最后（LRU更新）
            self.cache.move_to_end(scid)
            
            log.debug(
                f"[LRU CACHE] 缓存命中 - SCID: {scid[:8]}... "
                f"({entry['message_count']} 消息)"
            )
            
            return entry["history"]
    
    def delete(self, scid: str) -> bool:
        """
        删除指定 SCID 的缓存
        
        Returns:
            True: 删除成功
            False: SCID不存在
        """
        with self._lock:
            if scid in self.cache:
                entry = self.cache[scid]
                del self.cache[scid]
                
                log.info(
                    f"[LRU CACHE] 删除成功 - SCID: {scid[:8]}... "
                    f"({entry['message_count']} 消息)"
                )
                return True
            
            log.debug(f"[LRU CACHE] 删除失败（不存在） - SCID: {scid[:8]}...")
            return False
    
    def clear_all(self) -> int:
        """
        清空所有缓存
        
        警告：这是危险操作！会清空所有会话的历史缓存。
        """
        with self._lock:
            count = len(self.cache)
            self.cache.clear()
            
            log.warning(f"[LRU CACHE] 清空所有缓存 - 已清除 {count} 个条目")
            return count
    
    def get_stats(self) -> Dict:
        """
        获取缓存统计信息
        
        Returns:
            统计信息字典：
            - backend: "lru"
            - total_entries: 总条目数
            - max_size: 最大容量
            - total_messages: 总消息数
            - avg_messages_per_entry: 平均每个条目的消息数
            - usage_ratio: 使用率 (total_entries / max_size)
        """
        with self._lock:
            total_messages = sum(
                entry["message_count"] 
                for entry in self.cache.values()
            )
            
            total_entries = len(self.cache)
            usage_ratio = total_entries / self.max_size if self.max_size > 0 else 0
            
            stats = {
                "backend": "lru",
                "total_entries": total_entries,
                "max_size": self.max_size,
                "total_messages": total_messages,
                "avg_messages_per_entry": (
                    total_messages / total_entries 
                    if total_entries > 0 else 0
                ),
                "usage_ratio": round(usage_ratio, 2)
            }
            
            log.debug(
                f"[LRU CACHE] 统计信息 - "
                f"条目: {stats['total_entries']}/{stats['max_size']}, "
                f"消息: {stats['total_messages']}, "
                f"使用率: {stats['usage_ratio']*100:.1f}%"
            )
            
            return stats
