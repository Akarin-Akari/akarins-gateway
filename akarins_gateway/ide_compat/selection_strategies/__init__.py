"""
历史选择策略模块

提供不同的历史消息选择策略：
- Smart Selection: 智能选择策略（system消息+最近N条+重要中间消息）
- Recent Only: 仅保留最近N条消息（简单策略）
- Token Based: 基于Token数量的选择策略（预留）
"""

from .base import SelectionStrategy
from .smart_selector import SmartSelectionStrategy

__all__ = [
    "SelectionStrategy",
    "SmartSelectionStrategy",
]
