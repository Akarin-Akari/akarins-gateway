"""
智能历史选择策略

基于消息重要性和上下文相关性的智能选择算法。
"""

from typing import List, Dict, Optional, Set
import logging
import copy

from .base import SelectionStrategy

log = logging.getLogger("gcli2api.history_cache.selector")


class SmartSelectionStrategy(SelectionStrategy):
    """
    智能历史选择策略
    
    选择原则（按优先级）：
    1. **system 消息**：必须保留所有 system 消息（系统提示词）
    2. **最近N条消息**：保证当前上下文（默认最近10条）
    3. **重要中间消息**：用户明确指令、长消息（按内容长度排序）
    4. **工具链完整性**：确保 tool_use 和 functionResponse 成对出现
    
    优化目标：
    - 控制消息数量 ≤ max_messages
    - 控制Token数量 ≤ max_tokens（可选，软限制）
    - 保留最有价值的上下文
    - 避免破坏工具调用链
    
    算法流程：
    ```
    1. 检查是否需要选择（len(history) <= max_messages 直接返回）
    2. 提取并保留所有 system 消息
    3. 保留最近N条消息（保证当前上下文）
    4. 计算中间可保留的配额
    5. 从中间部分选择重要消息（用户消息优先，按长度排序）
    6. 合并：system + 重要中间消息 + 最近消息
    7. 确保工具链完整性
    8. 返回精选消息
    ```
    
    示例：
        ```python
        # 输入：50条消息
        history = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            # ... 47 more messages
        ]
        
        # 输出：20条消息
        selector = SmartSelectionStrategy()
        selected = selector.select(history, max_messages=20)
        
        # 保证：
        # - selected[0] 是 system 消息
        # - selected[-10:] 是最近10条消息
        # - 中间是重要的用户消息
        # - len(selected) == 20
        ```
    """
    
    def __init__(self, recent_count: int = 10):
        """
        初始化智能选择策略
        
        Args:
            recent_count: 保留的最近消息数量（默认10）
                         建议值：5-15（取决于对话密度）
        """
        self.recent_count = recent_count
        log.info(f"[SMART SELECTOR] 初始化完成 - recent_count={recent_count}")
    
    def select(
        self, 
        history: List[Dict], 
        max_messages: int,
        max_tokens: Optional[int] = None
    ) -> List[Dict]:
        """
        智能选择消息
        
        Args:
            history: 完整历史消息列表
            max_messages: 最大消息数量（硬限制）
            max_tokens: 最大Token数量（可选，软限制，暂未实现）
            
        Returns:
            精选后的消息列表，长度 ≤ max_messages
        """
        # 快速路径：如果历史长度在限制内，直接返回
        if len(history) <= max_messages:
            log.debug(
                f"[SMART SELECTOR] 历史长度在限制内 "
                f"({len(history)} <= {max_messages})，直接返回"
            )
            return history
        
        log.info(
            f"[SMART SELECTOR] 开始选择 - "
            f"输入: {len(history)} 消息, 目标: {max_messages} 消息"
        )
        
        selected = []
        
        # Step 1: 提取并保留所有 system 消息
        system_msgs = [msg for msg in history if msg.get("role") == "system"]
        selected.extend(system_msgs)
        log.debug(f"[SMART SELECTOR] Step 1: 保留 {len(system_msgs)} 条 system 消息")
        
        # Step 2: 保留最近N条消息
        recent_count = min(self.recent_count, max_messages - len(system_msgs))
        recent_msgs = history[-recent_count:] if recent_count > 0 else []
        
        log.debug(f"[SMART SELECTOR] Step 2: 保留最近 {len(recent_msgs)} 条消息")
        
        # Step 3: 计算中间可保留的配额
        middle_quota = max_messages - len(system_msgs) - len(recent_msgs)
        
        if middle_quota > 0:
            # Step 4: 从中间选择重要消息
            middle_start = len(system_msgs)
            middle_end = len(history) - len(recent_msgs)
            middle_msgs = history[middle_start:middle_end]
            
            important_msgs = self._select_important_messages(
                middle_msgs, 
                middle_quota
            )
            selected.extend(important_msgs)
            
            log.debug(
                f"[SMART SELECTOR] Step 3: 从中间选择 {len(important_msgs)} 条重要消息 "
                f"(配额: {middle_quota})"
            )
        
        # Step 5: 添加最近消息
        selected.extend(recent_msgs)
        
        # Step 6: 确保工具链完整性（暂时简化，task-06 会完善）
        # TODO: 实现完整的工具链检查
        selected = self._ensure_tool_chain_integrity(selected)
        
        log.info(
            f"[SMART SELECTOR] 选择完成 - "
            f"输入: {len(history)} 消息, 输出: {len(selected)} 消息"
        )
        
        return selected
    
    def _select_important_messages(
        self, 
        messages: List[Dict], 
        quota: int
    ) -> List[Dict]:
        """
        从中间消息中选择重要消息
        
        评分标准（按优先级）：
        1. **用户消息优先**：用户消息 > assistant 消息
        2. **长度优先**：长消息 > 短消息（更详细的指令）
        3. **包含明确指令**：包含问号、感叹号等
        
        Args:
            messages: 中间消息列表
            quota: 可选择的消息数量
            
        Returns:
            选中的重要消息列表
        """
        if quota <= 0 or not messages:
            return []
        
        # 优先保留用户消息（用户指令更重要）
        user_msgs = [msg for msg in messages if msg.get("role") == "user"]
        
        # 按内容长度排序（长消息通常包含更多信息）
        user_msgs_sorted = sorted(
            user_msgs,
            key=lambda m: len(str(m.get("content", ""))),
            reverse=True
        )
        
        # 取前 quota 条
        selected = user_msgs_sorted[:quota]
        
        # 保持原始顺序（重要：不能打乱消息顺序！）
        selected_sorted = sorted(
            selected,
            key=lambda m: messages.index(m)
        )
        
        log.debug(
            f"[SMART SELECTOR] _select_important_messages - "
            f"输入: {len(messages)} 消息, "
            f"用户消息: {len(user_msgs)}, "
            f"选中: {len(selected_sorted)} 消息"
        )
        
        return selected_sorted
    
    def _ensure_tool_chain_integrity(
        self,
        messages: List[Dict]
    ) -> List[Dict]:
        """
        确保工具链完整性 [FIX 2026-02-08 - task-06 完整实现]

        检查规则：
        1. 每个 tool_use/tool_call 必须有对应的 tool_result
        2. 每个 tool_result 必须有对应的 tool_use/tool_call
        3. 移除孤儿 tool_use 和 tool_result，防止 Claude API 400 错误

        工作流程：
        1. 收集所有 tool_use 的 ID（从 assistant 消息）
        2. 收集所有 tool_result 的 tool_use_id（从 user 消息）
        3. 找出孤儿（没有配对的 tool_use 或 tool_result）
        4. 从消息中移除孤儿内容
        5. 返回清理后的消息列表

        Args:
            messages: 消息列表

        Returns:
            清理后的消息列表，保证工具链完整性
        """
        if not messages:
            return messages

        # Step 1: 收集所有 tool_use/tool_call ID
        tool_use_ids: Set[str] = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                # OpenAI 格式：assistant.tool_calls[]
                tool_calls = msg.get("tool_calls", [])
                if isinstance(tool_calls, dict):
                    def _sort_key(raw_key):
                        try:
                            return (0, int(raw_key))
                        except Exception:
                            return (1, str(raw_key))
                    tool_calls = [
                        tool_calls[k] for k in sorted(tool_calls.keys(), key=_sort_key)
                        if isinstance(tool_calls.get(k), dict)
                    ]
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            tc_id = tc.get("id")
                            if tc_id:
                                tool_use_ids.add(str(tc_id))

                # Anthropic 格式：assistant.content[].tool_use
                content = msg.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_id = item.get("id")
                            if tool_id:
                                tool_use_ids.add(tool_id)

        # Step 2: 收集所有 tool_result 的 tool_use_id/tool_call_id
        tool_result_ids: Set[str] = set()
        for msg in messages:
            # OpenAI 格式：role=tool
            if msg.get("role") == "tool":
                tool_call_id = msg.get("tool_call_id")
                if tool_call_id:
                    tool_result_ids.add(str(tool_call_id))

            # Anthropic 格式：role=user content[].tool_result
            if msg.get("role") == "user":
                content = msg.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            tool_use_id = item.get("tool_use_id")
                            if tool_use_id:
                                tool_result_ids.add(tool_use_id)

        # Step 3: 找出孤儿
        orphan_tool_use_ids = tool_use_ids - tool_result_ids  # 有 tool_use 没有 tool_result
        orphan_tool_result_ids = tool_result_ids - tool_use_ids  # 有 tool_result 没有 tool_use

        # 快速路径：如果没有孤儿，直接返回
        if not orphan_tool_use_ids and not orphan_tool_result_ids:
            log.debug(
                f"[SMART SELECTOR] _ensure_tool_chain_integrity - "
                f"工具链完整: tool_use={len(tool_use_ids)}, tool_result={len(tool_result_ids)}"
            )
            return messages

        # Step 4: 记录警告日志
        if orphan_tool_use_ids:
            log.warning(
                f"[SMART SELECTOR] 发现孤儿 tool_use (将被移除) - "
                f"IDs: {list(orphan_tool_use_ids)[:5]}..."  # 最多显示5个
            )
        if orphan_tool_result_ids:
            log.warning(
                f"[SMART SELECTOR] 发现孤儿 tool_result (将被移除) - "
                f"IDs: {list(orphan_tool_result_ids)[:5]}..."
            )

        # Step 5: 清理消息中的孤儿
        cleaned_messages: List[Dict] = []
        removed_tool_use_count = 0
        removed_tool_result_count = 0

        for msg in messages:
            cleaned_msg = self._clean_message_orphans(
                msg,
                orphan_tool_use_ids,
                orphan_tool_result_ids
            )

            if cleaned_msg is not None:
                cleaned_messages.append(cleaned_msg)

                # 统计移除数量
                original_content = msg.get("content", [])
                cleaned_content = cleaned_msg.get("content", [])
                if isinstance(original_content, list) and isinstance(cleaned_content, list):
                    removed_tool_use_count += sum(
                        1 for item in original_content
                        if isinstance(item, dict) and item.get("type") == "tool_use"
                        and item.get("id") in orphan_tool_use_ids
                    )
                    removed_tool_result_count += sum(
                        1 for item in original_content
                        if isinstance(item, dict) and item.get("type") == "tool_result"
                        and item.get("tool_use_id") in orphan_tool_result_ids
                    )

        log.info(
            f"[SMART SELECTOR] _ensure_tool_chain_integrity 完成 - "
            f"输入: {len(messages)} 消息, 输出: {len(cleaned_messages)} 消息, "
            f"移除孤儿: tool_use={removed_tool_use_count}, tool_result={removed_tool_result_count}"
        )

        return cleaned_messages

    def _clean_message_orphans(
        self,
        msg: Dict,
        orphan_tool_use_ids: Set[str],
        orphan_tool_result_ids: Set[str]
    ) -> Optional[Dict]:
        """
        清理单个消息中的孤儿 tool_use 和 tool_result

        Args:
            msg: 原始消息
            orphan_tool_use_ids: 孤儿 tool_use ID 集合
            orphan_tool_result_ids: 孤儿 tool_result ID 集合

        Returns:
            清理后的消息，如果消息内容为空则返回 None
        """
        role = msg.get("role")

        # 深拷贝消息，避免修改原始数据
        cleaned_msg = copy.deepcopy(msg)

        # OpenAI 格式：role=tool 消息，若为孤儿 tool_result 直接移除整条
        if role == "tool":
            tool_call_id = cleaned_msg.get("tool_call_id")
            if tool_call_id and str(tool_call_id) in orphan_tool_result_ids:
                log.debug(f"[SMART SELECTOR] 移除孤儿 tool 消息: {tool_call_id}")
                return None
            return cleaned_msg

        # OpenAI 格式：assistant.tool_calls，移除孤儿 tool_call
        if role == "assistant":
            tool_calls = cleaned_msg.get("tool_calls")
            if isinstance(tool_calls, dict):
                def _sort_key(raw_key):
                    try:
                        return (0, int(raw_key))
                    except Exception:
                        return (1, str(raw_key))
                tool_calls = [
                    tool_calls[k] for k in sorted(tool_calls.keys(), key=_sort_key)
                    if isinstance(tool_calls.get(k), dict)
                ]
                cleaned_msg["tool_calls"] = tool_calls
            if isinstance(tool_calls, list):
                filtered_tool_calls = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        filtered_tool_calls.append(tc)
                        continue
                    tc_id = tc.get("id")
                    if tc_id and str(tc_id) in orphan_tool_use_ids:
                        log.debug(f"[SMART SELECTOR] 移除孤儿 assistant.tool_calls: {tc_id}")
                        continue
                    filtered_tool_calls.append(tc)

                cleaned_msg["tool_calls"] = filtered_tool_calls
                if not filtered_tool_calls:
                    cleaned_msg.pop("tool_calls", None)

        content = cleaned_msg.get("content")
        if not isinstance(content, list):
            # content 不是列表时，只做了 tool_calls/tool 消息清理
            # 若 assistant 既无 content 又无 tool_calls，则可安全移除
            if role == "assistant":
                has_tool_calls = isinstance(cleaned_msg.get("tool_calls"), list) and len(cleaned_msg.get("tool_calls")) > 0
                content_val = cleaned_msg.get("content")
                if not has_tool_calls and (content_val is None or str(content_val).strip() == ""):
                    return None
            return cleaned_msg

        cleaned_content: List[Dict] = []

        for item in content:
            if not isinstance(item, dict):
                cleaned_content.append(item)
                continue

            item_type = item.get("type")

            # 处理 assistant 消息中的 tool_use
            if role == "assistant" and item_type == "tool_use":
                tool_id = item.get("id")
                if tool_id and str(tool_id) in orphan_tool_use_ids:
                    # 跳过孤儿 tool_use
                    log.debug(f"[SMART SELECTOR] 移除孤儿 tool_use: {tool_id}")
                    continue

            # 处理 user 消息中的 tool_result
            if role == "user" and item_type == "tool_result":
                tool_use_id = item.get("tool_use_id")
                if tool_use_id and str(tool_use_id) in orphan_tool_result_ids:
                    # 跳过孤儿 tool_result
                    log.debug(f"[SMART SELECTOR] 移除孤儿 tool_result: {tool_use_id}")
                    continue

            # 保留非孤儿内容
            cleaned_content.append(item)

        # 如果清理后内容为空
        if not cleaned_content:
            # 对于 assistant 消息，如果只剩空内容，可能需要保留（添加占位文本）
            # 对于 user 消息，如果只剩空内容，可以移除
            if role == "user":
                log.debug(f"[SMART SELECTOR] 移除空 user 消息（tool_result 全部是孤儿）")
                return None
            elif role == "assistant":
                has_tool_calls = isinstance(cleaned_msg.get("tool_calls"), list) and len(cleaned_msg.get("tool_calls")) > 0
                if has_tool_calls:
                    # assistant 仍有 tool_calls 时不强行加占位文本
                    cleaned_msg["content"] = []
                    return cleaned_msg
                # assistant 消息既无 tool_calls 又无内容，移除
                return None

        cleaned_msg["content"] = cleaned_content
        return cleaned_msg
