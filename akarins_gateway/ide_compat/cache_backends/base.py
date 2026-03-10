"""
CacheBackend 抽象基类

定义所有缓存后端必须实现的接口，遵循依赖倒置原则(DIP)
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Optional


class CacheBackend(ABC):
    """
    缓存后端抽象基类
    
    所有缓存实现（LRU、Redis等）必须实现此接口。
    
    设计原则：
    - 单一职责原则(SRP): 只负责数据存储和检索
    - 接口隔离原则(ISP): 最小化接口，只包含必要方法
    - 里氏替换原则(LSP): 所有实现类可无缝替换
    """
    
    @abstractmethod
    def store(self, scid: str, history: List[Dict]) -> None:
        """
        存储完整历史
        
        Args:
            scid: 会话ID (Session Context ID)
            history: 完整历史消息列表，格式为 [{"role": "user", "content": "..."}]
            
        注意:
            - 存储的是未压缩的完整历史
            - 实现类应确保线程安全
            - 可能会覆盖已存在的SCID数据
        """
        pass
    
    @abstractmethod
    def get(self, scid: str) -> Optional[List[Dict]]:
        """
        获取完整历史
        
        Args:
            scid: 会话ID
            
        Returns:
            完整历史消息列表，如果不存在返回 None
            
        注意:
            - 返回的是未压缩的完整历史
            - 实现类应确保线程安全
        """
        pass
    
    @abstractmethod
    def delete(self, scid: str) -> bool:
        """
        删除指定 SCID 的缓存
        
        Args:
            scid: 会话ID
            
        Returns:
            是否删除成功 (True: 删除成功, False: SCID不存在或删除失败)
            
        注意:
            - 实现类应确保线程安全
        """
        pass
    
    @abstractmethod
    def clear_all(self) -> int:
        """
        清空所有缓存
        
        Returns:
            清除的条目数量
            
        注意:
            - 危险操作，应谨慎使用
            - 实现类应确保线程安全
        """
        pass
    
    @abstractmethod
    def get_stats(self) -> Dict:
        """
        获取缓存统计信息
        
        Returns:
            统计信息字典，至少包含：
            - backend: str 后端类型 ("lru", "redis" 等)
            - total_entries: int 总条目数
            - total_messages: int 总消息数（所有条目的消息总和）
            
        注意:
            - 用于监控和调试
            - 实现类应确保线程安全
        """
        pass
