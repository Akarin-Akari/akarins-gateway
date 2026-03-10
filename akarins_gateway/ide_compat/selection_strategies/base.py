"""
SelectionStrategy 抽象基类

定义历史消息选择策略的接口，遵循策略模式(Strategy Pattern)
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Optional


class SelectionStrategy(ABC):
    """
    历史选择策略抽象基类
    
    定义如何从完整历史中选择消息发送给后端API。
    
    设计原则：
    - 单一职责原则(SRP): 只负责消息选择逻辑
    - 开放封闭原则(OCP): 易于扩展新的选择策略
    - 策略模式: 不同策略可以无缝切换
    
    选择目标：
    - 控制消息数量 ≤ max_messages
    - 控制Token数量 ≤ max_tokens（可选）
    - 保留最有价值的上下文
    - 确保工具链完整性
    """
    
    @abstractmethod
    def select(
        self, 
        history: List[Dict], 
        max_messages: int,
        max_tokens: Optional[int] = None
    ) -> List[Dict]:
        """
        从完整历史中智能选择消息
        
        Args:
            history: 完整历史消息列表，格式为 [{"role": "user", "content": "..."}]
            max_messages: 最大消息数量（硬限制）
            max_tokens: 最大Token数量（可选，软限制）
            
        Returns:
            精选后的消息列表，长度 ≤ max_messages
            
        注意:
            - 如果 len(history) <= max_messages，应直接返回完整历史
            - 必须保留消息的原始顺序
            - 应优先保留：
              1. system 消息
              2. 最近的消息（当前上下文）
              3. 重要的中间消息（用户指令、长消息等）
            - 需要确保工具链完整性（tool_use 和 functionResponse 成对）
            - max_tokens 是软限制，可以超出但应尽量控制
        
        示例：
            ```python
            history = [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
                # ... 50 more messages
            ]
            
            selected = strategy.select(history, max_messages=20)
            assert len(selected) <= 20
            assert selected[0]["role"] == "system"  # 保留 system 消息
            assert selected[-1] == history[-1]  # 保留最后一条消息
            ```
        """
        pass
