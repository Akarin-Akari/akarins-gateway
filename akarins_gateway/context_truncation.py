"""
Context Truncation - 智能对话历史截断模块
用于处理长对话导致的 token 超限问题

这是自定义功能模块，原版 gcli2api 不包含此功能

核心策略：
1. 保留 system 消息（必须）
2. 保留最近的工具调用上下文（保持工具调用连贯性）
3. 保留最近 N 条消息（基于 token 预算动态计算）
4. 对中间的对话历史进行摘要或删除
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from akarins_gateway.core.log import log
# STUB: context_calibrator not extracted to akarins-gateway
try:
    from akarins_gateway.context_calibrator import get_global_calibrator
except ImportError:
    get_global_calibrator = None  # graceful degradation

# [FIX 2026-01-10] 使用 tiktoken 精确计算 token
try:
    import tiktoken
    # 使用 cl100k_base 编码器（GPT-4/Claude 使用的编码器）
    _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
    TIKTOKEN_AVAILABLE = True
    log.info("[CONTEXT TRUNCATION] tiktoken 已加载，使用精确 token 计算")
except ImportError:
    _TIKTOKEN_ENCODER = None
    TIKTOKEN_AVAILABLE = False
    log.warning("[CONTEXT TRUNCATION] tiktoken 未安装，使用字符估算模式")


# ====================== 配置常量 ======================

# Token 限制配置
# 注意：这些值需要根据实际 API 限制调整

# [FIX 2026-01-10] 动态阈值调整：根据模型类型设置不同的上下文限制
# [FIX 2026-01-11] 降低安全边际，为思考模式大量输出预留更多空间
# 模型系列 -> (上下文限制, 安全边际系数)
MODEL_CONTEXT_LIMITS = {
    # Claude 系列：200K 上下文
    # 思考模式输出可能高达 40K+，需要预留更多输出空间
    "claude": (200000, 0.55),      # 200K * 0.55 = 110K 安全限制，预留 90K 给输出
    "claude-opus": (200000, 0.50), # Opus thinking 需要更多输出空间
    "claude-sonnet": (200000, 0.55),
    "claude-haiku": (200000, 0.65),  # Haiku 输出较少，可以更激进
    
    # Gemini 3 系列：1M 上下文
    "gemini-3": (1000000, 0.70),   # 1M * 0.70 = 700K 安全限制
    "gemini-3-flash": (1000000, 0.75),
    "gemini-3-pro": (1000000, 0.70),
    
    # Gemini 2.5 系列：1M 上下文（与 Gemini 2.0/3.0 一致）
    "gemini-2.5": (1000000, 0.70),  # 1M * 0.70 = 700K 安全限制  # 128K * 0.80 = 102K 安全限制
    "gemini-2.5-flash": (1000000, 0.75),  # 1M * 0.75 = 750K 安全限制
    "gemini-2.5-pro": (1000000, 0.70),  # 1M * 0.70 = 700K 安全限制
    
    # GPT 系列：128K 上下文
    "gpt": (128000, 0.80),
    "gpt-4": (128000, 0.80),
    "gpt-oss": (128000, 0.80),
    
    # 默认值（保守估计）
    "default": (100000, 0.60),     # 100K * 0.60 = 60K 安全限制
}

def get_model_context_limit(model_name: str) -> Tuple[int, float]:
    """
    根据模型名称获取上下文限制和安全边际系数
    
    Args:
        model_name: 模型名称（如 "claude-sonnet-4-5", "gemini-3-flash"）
        
    Returns:
        (context_limit, safety_margin)
    """
    if not model_name:
        return MODEL_CONTEXT_LIMITS["default"]
    
    model_lower = model_name.lower()
    
    # 按优先级匹配（更具体的优先）
    for prefix in ["gemini-3-flash", "gemini-3-pro", "gemini-3",
                   "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5",
                   "claude-opus", "claude-sonnet", "claude-haiku", "claude",
                   "gpt-4", "gpt-oss", "gpt"]:
        if prefix in model_lower:
            return MODEL_CONTEXT_LIMITS[prefix]
    
    return MODEL_CONTEXT_LIMITS["default"]


def get_dynamic_target_limit(model_name: str, max_output_tokens: int = 16384) -> int:
    """
    动态计算目标 token 限制
    
    Args:
        model_name: 模型名称
        max_output_tokens: 预期的最大输出 token 数
        
    Returns:
        目标 token 限制
    """
    context_limit, safety_margin = get_model_context_limit(model_name)
    
    # 计算安全的输入限制
    # 公式：safe_input = (context_limit * safety_margin) - max_output_tokens
    safe_input = int(context_limit * safety_margin) - max_output_tokens
    
    # 确保至少有 10K tokens
    safe_input = max(safe_input, 10000)
    
    log.debug(f"[DYNAMIC LIMIT] model={model_name}, context={context_limit:,}, "
             f"margin={safety_margin}, output={max_output_tokens:,}, safe_input={safe_input:,}")
    
    return safe_input


# 目标 token 数量（截断后的目标值，留出足够的输出空间）
# 注意：这是默认值，实际使用时应该调用 get_dynamic_target_limit()
TARGET_TOKEN_LIMIT = 60000  # 60K tokens - 保守值，确保有足够输出空间

# 最小保留消息数（即使 token 超限也至少保留这些消息）
MIN_KEEP_MESSAGES = 4  # 至少保留最近 4 条消息

# 工具调用上下文保护：保留最近 N 轮工具调用相关的消息
TOOL_CONTEXT_PROTECT_ROUNDS = 3

# 每条消息的默认 token 估算系数
# 实际上不同类型的消息 token 密度不同，这里使用保守估算
CHARS_PER_TOKEN = 4  # 1 token ≈ 4 字符（英文），中文约 2-3 字符

# 工具结果的 token 估算系数（工具结果通常更密集）
TOOL_RESULT_CHARS_PER_TOKEN = 3

# ====================== [FIX 2026-01-29] AM 三层渐进式压缩配置 ======================
# 同步自 Antigravity-Manager/src-tauri/src/proxy/mappers/context_manager.rs

# Layer 1: 工具轮次裁剪配置
# [FIX 2026-02-05] 从 5 提升到 8，IDE 工具调用频繁，5 轮太少容易丢失上下文
LAYER1_KEEP_TOOL_ROUNDS = 8  # 保留最近 8 轮工具调用
LAYER1_PRESSURE_THRESHOLD = 0.60  # 60% 压力时触发 Layer 1

# Layer 2: Thinking 签名保留压缩配置
LAYER2_PROTECTED_LAST_N = 4  # 保护最后 4 条消息的 thinking 不被压缩
LAYER2_PRESSURE_THRESHOLD = 0.75  # 75% 压力时触发 Layer 2

# Layer 3: XML 摘要 Fork 配置
LAYER3_PRESSURE_THRESHOLD = 0.90  # 90% 压力时触发 Layer 3
LAYER3_SUMMARY_MODEL = "gemini-2.5-flash-lite"  # 用于生成摘要的轻量模型

# ====================== [FIX 2026-01-15] AM兼容工具结果压缩配置 ======================
# 同步自 Antigravity-Manager/src-tauri/src/proxy/mappers/tool_result_compressor.rs

# 最大工具结果字符数 (约 20 万,防止 prompt 超长)
MAX_TOOL_RESULT_CHARS = 200_000

# 浏览器快照检测阈值
SNAPSHOT_DETECTION_THRESHOLD = 20_000

# 浏览器快照压缩后的最大字符数
SNAPSHOT_MAX_CHARS = 16_000

# 浏览器快照头部保留比例
SNAPSHOT_HEAD_RATIO = 0.7

# 浏览器快照尾部保留比例
# [FIX 2026-02-05] 修正为 0.3，避免 头70%+尾40%=110% 重叠问题
SNAPSHOT_TAIL_RATIO = 0.3  # AM 原值，头70%+尾30%=100%

# 普通压缩头部保留比例
COMPRESS_HEAD_RATIO = 0.7  # 用户要求同步为 70%（原为 40%）

# 普通压缩尾部保留比例
# [FIX 2026-02-05] 修正为 0.3，与 AM 保持一致，避免比例重叠
COMPRESS_TAIL_RATIO = 0.3  # AM 原值，头70%+尾30%=100%


# ====================== Token 估算 ======================

def _count_tokens_tiktoken(text: str) -> int:
    """使用 tiktoken 精确计算 token 数量"""
    if not text or not TIKTOKEN_AVAILABLE or _TIKTOKEN_ENCODER is None:
        return 0
    try:
        return len(_TIKTOKEN_ENCODER.encode(text))
    except Exception:
        # 编码失败时回退到字符估算
        return len(text) // CHARS_PER_TOKEN


def _count_tokens_fallback(text: str, is_tool_result: bool = False) -> int:
    """字符估算模式（tiktoken 不可用时的回退方案）"""
    if not text:
        return 0
    chars_per_token = TOOL_RESULT_CHARS_PER_TOKEN if is_tool_result else CHARS_PER_TOKEN
    return max(1, len(text) // chars_per_token)


def estimate_message_tokens(message: Any) -> int:
    """
    估算单条消息的 token 数量
    
    [FIX 2026-01-10] 优先使用 tiktoken 精确计算，不可用时回退到字符估算
    
    Args:
        message: OpenAI 格式的消息对象或字典
        
    Returns:
        估算的 token 数量
    """
    total_tokens = 0
    use_tiktoken = TIKTOKEN_AVAILABLE and _TIKTOKEN_ENCODER is not None
    
    # 提取 role
    if hasattr(message, "role"):
        role = getattr(message, "role", "user")
        content = getattr(message, "content", "")
        tool_calls = getattr(message, "tool_calls", None)
        tool_call_id = getattr(message, "tool_call_id", None)
    elif isinstance(message, dict):
        role = message.get("role", "user")
        content = message.get("content", "")
        tool_calls = message.get("tool_calls")
        tool_call_id = message.get("tool_call_id")
    else:
        return 10  # 未知格式，返回最小估算值
    
    is_tool_result = (role == "tool" or tool_call_id is not None)
    
    # 计算 content 的 token 数
    if isinstance(content, str):
        if use_tiktoken:
            total_tokens += _count_tokens_tiktoken(content)
        else:
            total_tokens += _count_tokens_fallback(content, is_tool_result)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    text = item.get("text", "")
                    if use_tiktoken:
                        total_tokens += _count_tokens_tiktoken(text)
                    else:
                        total_tokens += _count_tokens_fallback(text, is_tool_result)
                elif item.get("type") == "thinking":
                    thinking = item.get("thinking", "")
                    if use_tiktoken:
                        total_tokens += _count_tokens_tiktoken(thinking)
                    else:
                        total_tokens += _count_tokens_fallback(thinking, is_tool_result)
                elif item.get("type") == "image_url":
                    # 图片 token 估算：每张图片约 1K tokens
                    total_tokens += 1000
            elif isinstance(item, str):
                if use_tiktoken:
                    total_tokens += _count_tokens_tiktoken(item)
                else:
                    total_tokens += _count_tokens_fallback(item, is_tool_result)
    
    # 计算 tool_calls 的 token 数
    if tool_calls:
        for tc in tool_calls:
            if hasattr(tc, "function"):
                func = getattr(tc, "function", None)
                if func:
                    name = getattr(func, "name", "") or ""
                    args = getattr(func, "arguments", "") or ""
                    if use_tiktoken:
                        total_tokens += _count_tokens_tiktoken(name)
                        total_tokens += _count_tokens_tiktoken(args)
                    else:
                        total_tokens += _count_tokens_fallback(name)
                        total_tokens += _count_tokens_fallback(args)
            elif isinstance(tc, dict):
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", "")
                if use_tiktoken:
                    total_tokens += _count_tokens_tiktoken(name)
                    total_tokens += _count_tokens_tiktoken(args)
                else:
                    total_tokens += _count_tokens_fallback(name)
                    total_tokens += _count_tokens_fallback(args)
    
    # 返回计算的 token 数（至少为 1）
    return max(1, total_tokens)


def estimate_messages_tokens(messages: List[Any]) -> int:
    """
    估算消息列表的总 token 数量
    
    Args:
        messages: OpenAI 格式的消息列表
        
    Returns:
        估算的总 token 数量
    """
    total = 0
    for msg in messages:
        total += estimate_message_tokens(msg)
    return total


def estimate_messages_tokens_calibrated(messages: List[Any]) -> Tuple[int, int, float]:
    """
    使用全局校准器估算消息列表 token 数量
    
    Returns:
        (raw_estimated, calibrated_estimated, factor)
    """
    raw = estimate_messages_tokens(messages)
    calibrator = get_global_calibrator()
    calibrated = calibrator.calibrate(raw)
    factor = calibrator.get_factor()
    return raw, calibrated, factor


# ====================== 消息分类 ======================

from dataclasses import dataclass

# ====================== [FIX 2026-01-29] Layer 1: 工具轮次智能裁剪 ======================
# 同步自 Antigravity-Manager/src-tauri/src/proxy/mappers/context_manager.rs

@dataclass
class ToolRound:
    """
    表示一个工具调用轮次 (assistant tool_use + user tool_result(s))

    同步自 AM context_manager.rs:ToolRound

    [FIX 2026-02-01] 扩展支持并行工具调用：
    - expected_results: 期望的 tool_result 数量（等于 tool_use 数量）
    - collected_results: 已收集的 tool_result 数量
    """
    assistant_index: int  # 包含 tool_use 的 assistant 消息索引
    tool_result_indices: List[int]  # 对应的 tool_result 消息索引列表
    indices: List[int]  # 该轮次中所有消息的索引
    expected_results: int = 1  # [FIX 2026-02-01] 期望的 tool_result 数量
    collected_results: int = 0  # [FIX 2026-02-01] 已收集的 tool_result 数量

    def is_complete(self) -> bool:
        """检查轮次是否完整（收集到所有期望的 tool_result）"""
        return self.collected_results >= self.expected_results


def _has_tool_use(message: Any) -> bool:
    """
    检查消息是否包含 tool_use（工具调用）

    支持两种格式：
    1. OpenAI 格式：message.tool_calls 或 message["tool_calls"]
    2. Anthropic 格式：content 中包含 type="tool_use" 的块

    Args:
        message: 消息对象

    Returns:
        是否包含 tool_use
    """
    # 提取属性
    if hasattr(message, "tool_calls"):
        tool_calls = getattr(message, "tool_calls", None)
    elif isinstance(message, dict):
        tool_calls = message.get("tool_calls")
    else:
        tool_calls = None

    # OpenAI 格式检查
    if tool_calls:
        return True

    # Anthropic 格式检查：content 数组中包含 tool_use 块
    if hasattr(message, "content"):
        content = getattr(message, "content", None)
    elif isinstance(message, dict):
        content = message.get("content")
    else:
        content = None

    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True

    return False


# [FIX 2026-02-01] 新增：计算消息中 tool_use 的数量，支持并行工具调用
def _count_tool_uses(message: Any) -> int:
    """
    计算消息中 tool_use 的数量

    支持两种格式：
    1. OpenAI 格式：message.tool_calls 或 message["tool_calls"]
    2. Anthropic 格式：content 中包含 type="tool_use" 的块

    Args:
        message: 消息对象

    Returns:
        tool_use 的数量
    """
    count = 0

    # 提取 tool_calls
    if hasattr(message, "tool_calls"):
        tool_calls = getattr(message, "tool_calls", None)
    elif isinstance(message, dict):
        tool_calls = message.get("tool_calls")
    else:
        tool_calls = None

    # OpenAI 格式计数
    if tool_calls and isinstance(tool_calls, list):
        count += len(tool_calls)

    # Anthropic 格式检查：content 数组中包含 tool_use 块
    if hasattr(message, "content"):
        content = getattr(message, "content", None)
    elif isinstance(message, dict):
        content = message.get("content")
    else:
        content = None

    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                count += 1

    return count


# [FIX 2026-02-01] 新增：计算消息中 tool_result 的数量
def _count_tool_results(message: Any) -> int:
    """
    计算消息中 tool_result 的数量

    支持两种格式：
    1. OpenAI 格式：role="tool" 的消息（每条消息算1个）
    2. Anthropic 格式：content 中包含 type="tool_result" 的块

    Args:
        message: 消息对象

    Returns:
        tool_result 的数量
    """
    count = 0

    # 获取 role
    if hasattr(message, "role"):
        role = getattr(message, "role", "")
    elif isinstance(message, dict):
        role = message.get("role", "")
    else:
        role = ""

    # OpenAI 格式：role="tool" 的消息
    if role == "tool":
        return 1

    # Anthropic 格式检查：content 数组中包含 tool_result 块
    if hasattr(message, "content"):
        content = getattr(message, "content", None)
    elif isinstance(message, dict):
        content = message.get("content")
    else:
        content = None

    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                count += 1

    return count


def _has_tool_result(message: Any) -> bool:
    """
    检查消息是否包含 tool_result（工具结果）

    支持两种格式：
    1. OpenAI 格式：role="tool" 或有 tool_call_id
    2. Anthropic 格式：content 中包含 type="tool_result" 的块

    Args:
        message: 消息对象

    Returns:
        是否包含 tool_result
    """
    # 提取属性
    if hasattr(message, "role"):
        role = getattr(message, "role", "")
        tool_call_id = getattr(message, "tool_call_id", None)
        content = getattr(message, "content", None)
    elif isinstance(message, dict):
        role = message.get("role", "")
        tool_call_id = message.get("tool_call_id")
        content = message.get("content")
    else:
        return False

    # OpenAI 格式检查
    if role == "tool" or tool_call_id:
        return True

    # Anthropic 格式检查：content 数组中包含 tool_result 块
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return True

    return False


def identify_tool_rounds(messages: List[Any]) -> List[ToolRound]:
    """
    [FIX 2026-02-01] 识别消息历史中的工具调用轮次
    支持并行工具调用 (Parallel Tool Use)

    一个轮次包含：
    - 一个包含 tool_use 的 assistant 消息（可能包含多个并行调用）
    - 对应数量的包含 tool_result 的消息

    Args:
        messages: 消息列表

    Returns:
        工具轮次列表
    """
    rounds: List[ToolRound] = []
    current_round: Optional[ToolRound] = None

    for i, msg in enumerate(messages):
        # 获取 role
        if hasattr(msg, "role"):
            role = getattr(msg, "role", "")
        elif isinstance(msg, dict):
            role = msg.get("role", "")
        else:
            continue

        if role == "assistant":
            tool_use_count = _count_tool_uses(msg)
            if tool_use_count > 0:
                # 如果当前已有未完成的轮次，先关闭它（虽然不符合规范，但为了鲁棒性）
                if current_round is not None:
                    rounds.append(current_round)

                # 开始新轮次，记录期望的 tool_result 数量
                current_round = ToolRound(
                    assistant_index=i,
                    tool_result_indices=[],
                    indices=[i],
                    expected_results=tool_use_count,
                    collected_results=0
                )
            elif _has_tool_use(msg): # 兜底逻辑
                 if current_round is not None:
                    rounds.append(current_round)
                 current_round = ToolRound(
                    assistant_index=i,
                    tool_result_indices=[],
                    indices=[i]
                )

        elif role == "user" or role == "tool":
            if current_round is not None:
                # 计算该消息包含多少个 tool_result
                result_count = _count_tool_results(msg)

                if result_count > 0:
                    current_round.tool_result_indices.append(i)
                    current_round.indices.append(i)
                    current_round.collected_results += result_count

                    # 如果该轮次已完成，关闭它
                    if current_round.is_complete():
                        rounds.append(current_round)
                        current_round = None
                elif role == "user":
                    # 普通 user 消息且不包含 tool_result，直接结束当前轮次
                    # 这可能发生在某些异常流程中
                    rounds.append(current_round)
                    current_round = None

    # 保存最后一个轮次（如果存在）
    if current_round is not None:
        rounds.append(current_round)

    log.info(f"[TOOL ROUNDS] Identified {len(rounds)} tool rounds in {len(messages)} messages")

    return rounds


def _ensure_tool_chain_atomic(messages: List[Any]) -> List[Any]:
    """
    [FIX 2026-02-05] 确保 tool_use 和 tool_result 的原子性配对。

    问题场景：
    PCC 压缩可能拆散工具链配对，导致：
    - 孤立的 tool_use（没有对应的 tool_result）
    - 孤立的 tool_result（没有对应的 tool_use）

    这会导致后端 API 返回 400 错误：
    messages.2: `tool_use` ids were found without `tool_result` blocks

    解决方案：
    1. 收集所有 tool_use_id 和 tool_result_id
    2. 找出不匹配的 ID
    3. 清理孤立的节点

    Args:
        messages: 消息列表

    Returns:
        清理后的消息列表（保证工具链配对完整）
    """
    if not messages:
        return messages

    # Step 1: 收集所有 tool_use_id
    tool_use_ids: Dict[str, int] = {}  # id -> message_index
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "")

        if role == "assistant":
            # OpenAI 格式: tool_calls
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict) and tc.get("id"):
                    tool_use_ids[tc["id"]] = i

            # Anthropic 格式: content 中的 tool_use 块
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("id"):
                            tool_use_ids[block["id"]] = i

    # Step 2: 收集所有 tool_result_id
    tool_result_ids: Dict[str, int] = {}  # id -> message_index
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "")

        # OpenAI 格式: role=tool
        if role == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id:
                tool_result_ids[tc_id] = i

        # Anthropic 格式: role=user, content 中的 tool_result 块
        if role == "user":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tu_id = block.get("tool_use_id")
                        if tu_id:
                            tool_result_ids[tu_id] = i

    # Step 3: 找出不匹配的 ID
    orphan_use_ids = set(tool_use_ids.keys()) - set(tool_result_ids.keys())
    orphan_result_ids = set(tool_result_ids.keys()) - set(tool_use_ids.keys())

    if not orphan_use_ids and not orphan_result_ids:
        # 没有孤立节点，直接返回
        return messages

    log.warning(
        f"[TOOL_CHAIN_ATOMIC] Found orphan nodes: "
        f"orphan_uses={len(orphan_use_ids)}, orphan_results={len(orphan_result_ids)}"
    )

    # Step 4: 清理孤立节点
    # 收集需要移除或修改的消息索引
    indices_to_remove: set = set()

    # 4a: 处理孤立的 tool_result 消息
    for orphan_id in orphan_result_ids:
        msg_idx = tool_result_ids[orphan_id]
        msg = messages[msg_idx]

        if msg.get("role") == "tool":
            # OpenAI 格式：整个消息就是 tool result，直接标记移除
            indices_to_remove.add(msg_idx)
            log.debug(f"[TOOL_CHAIN_ATOMIC] Marking orphan tool message for removal: idx={msg_idx}, id={orphan_id}")

    # 4b: 处理孤立的 tool_use - 需要从 assistant 消息中移除对应的 tool_call
    messages_to_modify: Dict[int, List[str]] = {}  # msg_idx -> list of tool_call_ids to remove
    for orphan_id in orphan_use_ids:
        msg_idx = tool_use_ids[orphan_id]
        if msg_idx not in messages_to_modify:
            messages_to_modify[msg_idx] = []
        messages_to_modify[msg_idx].append(orphan_id)

    # 构建新的消息列表
    new_messages = []
    for i, msg in enumerate(messages):
        if i in indices_to_remove:
            continue

        if i in messages_to_modify:
            # 需要从此消息中移除孤立的 tool_calls
            ids_to_remove = set(messages_to_modify[i])
            new_msg = dict(msg)

            # 处理 OpenAI 格式
            if "tool_calls" in new_msg:
                original_count = len(new_msg["tool_calls"])
                new_msg["tool_calls"] = [
                    tc for tc in new_msg["tool_calls"]
                    if not (isinstance(tc, dict) and tc.get("id") in ids_to_remove)
                ]
                removed_count = original_count - len(new_msg["tool_calls"])
                if removed_count > 0:
                    log.debug(f"[TOOL_CHAIN_ATOMIC] Removed {removed_count} orphan tool_calls from msg idx={i}")

                # 如果移除后没有 tool_calls 了，检查是否还有 content
                if not new_msg["tool_calls"]:
                    del new_msg["tool_calls"]
                    # 如果也没有 content，跳过这条消息
                    if not new_msg.get("content"):
                        continue

            # 处理 Anthropic 格式
            if isinstance(new_msg.get("content"), list):
                original_count = len(new_msg["content"])
                new_msg["content"] = [
                    block for block in new_msg["content"]
                    if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") in ids_to_remove)
                ]
                removed_count = original_count - len(new_msg["content"])
                if removed_count > 0:
                    log.debug(f"[TOOL_CHAIN_ATOMIC] Removed {removed_count} orphan tool_use blocks from msg idx={i}")

            new_messages.append(new_msg)
        else:
            new_messages.append(msg)

    removed_count = len(messages) - len(new_messages)
    if removed_count > 0:
        log.info(
            f"[TOOL_CHAIN_ATOMIC] Cleaned {removed_count} messages with orphan tool nodes. "
            f"Before: {len(messages)}, After: {len(new_messages)}"
        )

    return new_messages


def trim_tool_messages(
    messages: List[Any],
    keep_last_n_rounds: int = LAYER1_KEEP_TOOL_ROUNDS,
) -> Tuple[List[Any], bool, Dict[str, Any]]:
    """
    [FIX 2026-01-29] Layer 1: 工具轮次智能裁剪

    同步自 AM context_manager.rs:trim_tool_messages

    只保留最近 N 轮工具调用，删除更早的工具调用消息。
    这是 cache-friendly 的操作，因为只删除消息而不修改内容。

    Args:
        messages: 消息列表
        keep_last_n_rounds: 保留最近 N 轮工具调用

    Returns:
        (trimmed_messages, was_trimmed, stats)
        - trimmed_messages: 裁剪后的消息列表
        - was_trimmed: 是否进行了裁剪
        - stats: 裁剪统计信息
    """
    tool_rounds = identify_tool_rounds(messages)

    stats = {
        "total_rounds": len(tool_rounds),
        "keep_rounds": keep_last_n_rounds,
        "removed_rounds": 0,
        "removed_messages": 0,
    }

    if len(tool_rounds) <= keep_last_n_rounds:
        return messages, False, stats  # 无需裁剪

    # 计算需要移除的轮次数
    rounds_to_remove = len(tool_rounds) - keep_last_n_rounds
    stats["removed_rounds"] = rounds_to_remove

    # 收集需要移除的消息索引
    indices_to_remove = set()
    for round_obj in tool_rounds[:rounds_to_remove]:
        for idx in round_obj.indices:
            indices_to_remove.add(idx)

    stats["removed_messages"] = len(indices_to_remove)

    # 构建裁剪后的消息列表（保持原始顺序）
    trimmed = [msg for i, msg in enumerate(messages) if i not in indices_to_remove]

    log.info(
        f"[LAYER-1] Trimmed {len(indices_to_remove)} tool messages, "
        f"kept last {keep_last_n_rounds} rounds (removed {rounds_to_remove} rounds)"
    )

    return trimmed, True, stats


# ====================== [FIX 2026-01-29] Layer 2: Thinking 签名保留压缩 ======================
# 同步自 Antigravity-Manager/src-tauri/src/proxy/mappers/context_manager.rs

def _extract_thinking_signature(block: Dict[str, Any]) -> Optional[str]:
    """
    从 thinking 块中提取签名

    支持两种格式：
    1. Anthropic 格式：type="thinking", signature 字段
    2. Gemini 格式：thought=true, 可能没有签名

    Args:
        block: 内容块

    Returns:
        签名字符串，如果不存在则返回 None
    """
    if not isinstance(block, dict):
        return None

    # Anthropic 格式
    if block.get("type") in ("thinking", "redacted_thinking"):
        return block.get("signature")

    # Gemini 格式（通常没有签名）
    if block.get("thought") is True:
        return block.get("signature")

    return None


def _is_thinking_block(block: Any) -> bool:
    """
    检查是否是 thinking 块

    支持两种格式：
    1. Anthropic 格式：type="thinking" 或 type="redacted_thinking"
    2. Gemini 格式：thought=true

    Args:
        block: 内容块

    Returns:
        是否是 thinking 块
    """
    if not isinstance(block, dict):
        return False

    return (
        block.get("type") in ("thinking", "redacted_thinking")
        or block.get("thought") is True
    )


def _get_thinking_text(block: Dict[str, Any]) -> Optional[str]:
    """
    从 thinking 块中提取思考文本

    Args:
        block: 内容块

    Returns:
        思考文本，如果不存在则返回 None
    """
    if not isinstance(block, dict):
        return None

    # Anthropic 格式
    if block.get("type") == "thinking":
        return block.get("thinking")

    # Gemini 格式
    if block.get("thought") is True:
        return block.get("text")

    return None


def compress_thinking_preserve_signature(
    messages: List[Any],
    protected_last_n: int = LAYER2_PROTECTED_LAST_N,
) -> Tuple[List[Any], bool, Dict[str, Any]]:
    """
    [FIX 2026-01-29] Layer 2: Thinking 签名保留压缩

    同步自 AM context_manager.rs:compress_thinking_preserve_signature

    压缩 thinking 块的文本内容为 "..."，但保留签名。
    这样可以大幅减少 token 数量，同时保持签名链的完整性。

    注意：此操作会修改消息内容，可能会破坏 Prompt Cache。

    Args:
        messages: 消息列表
        protected_last_n: 保护最后 N 条消息不被压缩

    Returns:
        (compressed_messages, was_compressed, stats)
        - compressed_messages: 压缩后的消息列表（深拷贝）
        - was_compressed: 是否进行了压缩
        - stats: 压缩统计信息
    """
    import copy

    total_msgs = len(messages)
    stats = {
        "total_messages": total_msgs,
        "protected_last_n": protected_last_n,
        "compressed_blocks": 0,
        "chars_saved": 0,
        "estimated_tokens_saved": 0,
    }

    if total_msgs == 0:
        return messages, False, stats

    # 计算保护起始索引
    start_protection_idx = max(0, total_msgs - protected_last_n)

    # 深拷贝消息列表以避免修改原始数据
    compressed_messages = copy.deepcopy(messages)
    compressed_count = 0
    total_chars_saved = 0

    for i, msg in enumerate(compressed_messages):
        # 跳过受保护的消息
        if i >= start_protection_idx:
            continue

        # 获取 role
        if hasattr(msg, "role"):
            role = getattr(msg, "role", "")
        elif isinstance(msg, dict):
            role = msg.get("role", "")
        else:
            continue

        # 只处理 assistant 消息
        if role != "assistant":
            continue

        # 获取 content
        if hasattr(msg, "content"):
            content = getattr(msg, "content", None)
        elif isinstance(msg, dict):
            content = msg.get("content")
        else:
            continue

        # 只处理数组格式的 content
        if not isinstance(content, list):
            continue

        for block in content:
            if not _is_thinking_block(block):
                continue

            # 获取签名
            signature = _extract_thinking_signature(block)

            # 关键逻辑：只有存在签名时才压缩
            # 这确保我们不会丢失未签名的 thinking 块
            if signature is None:
                continue

            # 获取原始思考文本
            thinking_text = _get_thinking_text(block)
            if thinking_text is None or len(thinking_text) <= 10:
                continue

            original_len = len(thinking_text)

            # 压缩：将思考文本替换为 "..."
            if block.get("type") == "thinking":
                block["thinking"] = "..."
            elif block.get("thought") is True:
                block["text"] = "..."

            compressed_count += 1
            total_chars_saved += original_len - 3

            log.debug(
                f"[LAYER-2] Compressed thinking: {original_len} → 3 chars (signature preserved)"
            )

    if compressed_count > 0:
        estimated_tokens_saved = int(total_chars_saved / 3.5)
        stats["compressed_blocks"] = compressed_count
        stats["chars_saved"] = total_chars_saved
        stats["estimated_tokens_saved"] = estimated_tokens_saved

        log.info(
            f"[LAYER-2] Compressed {compressed_count} thinking blocks "
            f"(saved ~{estimated_tokens_saved} tokens, signatures preserved)"
        )

    return compressed_messages, compressed_count > 0, stats


def extract_last_valid_signature(messages: List[Any]) -> Optional[str]:
    """
    [FIX 2026-01-29] 从消息历史中提取最后一个有效签名

    同步自 AM context_manager.rs:extract_last_valid_signature

    这对于 Layer 3 (Fork + Summary) 保持签名链至关重要。
    签名将被嵌入到 XML 摘要中，并在 fork 后恢复。

    Args:
        messages: 消息列表

    Returns:
        最后一个有效签名（长度 >= 50），如果不存在则返回 None
    """
    # 反向遍历以找到最近的签名
    for msg in reversed(messages):
        # 获取 role
        if hasattr(msg, "role"):
            role = getattr(msg, "role", "")
        elif isinstance(msg, dict):
            role = msg.get("role", "")
        else:
            continue

        if role != "assistant":
            continue

        # 获取 content
        if hasattr(msg, "content"):
            content = getattr(msg, "content", None)
        elif isinstance(msg, dict):
            content = msg.get("content")
        else:
            continue

        if not isinstance(content, list):
            continue

        for block in content:
            if not _is_thinking_block(block):
                continue

            signature = _extract_thinking_signature(block)
            if signature and len(signature) >= 50:
                log.debug(
                    f"[LAYER-3] Extracted last valid signature (len: {len(signature)})"
                )
                return signature

    log.debug("[LAYER-3] No valid signature found in history")
    return None


# ====================== 原有消息分类 ======================

def classify_messages(messages: List[Any]) -> Dict[str, List[Tuple[int, Any]]]:
    """
    将消息分类为不同类型
    
    Args:
        messages: OpenAI 格式的消息列表
        
    Returns:
        分类后的消息字典：
        {
            "system": [(index, message), ...],
            "tool_context": [(index, message), ...],  # 工具调用相关消息
            "regular": [(index, message), ...],  # 普通对话消息
        }
    """
    result = {
        "system": [],
        "tool_context": [],
        "regular": [],
    }
    
    # 第一遍：找出所有工具调用相关的消息索引
    tool_related_indices = set()
    
    for i, msg in enumerate(messages):
        if hasattr(msg, "role"):
            role = getattr(msg, "role", "")
            tool_calls = getattr(msg, "tool_calls", None)
            tool_call_id = getattr(msg, "tool_call_id", None)
        elif isinstance(msg, dict):
            role = msg.get("role", "")
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id")
        else:
            continue
        
        # 系统消息
        if role == "system":
            result["system"].append((i, msg))
            continue
        
        # 工具相关消息
        if role == "tool" or tool_call_id or tool_calls:
            tool_related_indices.add(i)
            # 如果是工具结果，向前查找对应的工具调用
            if role == "tool" or tool_call_id:
                # 向前查找最近的 assistant 消息（包含 tool_calls）
                for j in range(i - 1, -1, -1):
                    prev_msg = messages[j]
                    prev_role = getattr(prev_msg, "role", None) or (prev_msg.get("role") if isinstance(prev_msg, dict) else None)
                    prev_tool_calls = getattr(prev_msg, "tool_calls", None) or (prev_msg.get("tool_calls") if isinstance(prev_msg, dict) else None)
                    if prev_role == "assistant" and prev_tool_calls:
                        tool_related_indices.add(j)
                        break
    
    # 第二遍：分类消息
    for i, msg in enumerate(messages):
        if hasattr(msg, "role"):
            role = getattr(msg, "role", "")
        elif isinstance(msg, dict):
            role = msg.get("role", "")
        else:
            result["regular"].append((i, msg))
            continue
        
        if role == "system":
            continue  # 已处理
        elif i in tool_related_indices:
            result["tool_context"].append((i, msg))
        else:
            result["regular"].append((i, msg))
    
    return result


# ====================== 消息截断策略 ======================

def truncate_messages_smart(
    messages: List[Any],
    target_tokens: int = TARGET_TOKEN_LIMIT,
    min_keep: int = MIN_KEEP_MESSAGES,
    protect_tool_rounds: int = TOOL_CONTEXT_PROTECT_ROUNDS,
) -> Tuple[List[Any], Dict[str, Any]]:
    """
    智能截断消息列表，保持对话连贯性
    
    策略：
    1. 始终保留 system 消息
    2. 保留最近 N 轮的工具调用上下文
    3. 从最旧的普通消息开始删除
    4. 确保至少保留 min_keep 条消息
    
    Args:
        messages: OpenAI 格式的消息列表
        target_tokens: 目标 token 数量上限
        min_keep: 最小保留消息数
        protect_tool_rounds: 保护最近 N 轮工具调用
        
    Returns:
        (truncated_messages, stats)
        - truncated_messages: 截断后的消息列表
        - stats: 截断统计信息
    """
    original_count = len(messages)
    raw_tokens, calibrated_tokens, factor = estimate_messages_tokens_calibrated(messages)
    
    # 使用校准后的 token 数作为压力判断依据
    original_tokens = calibrated_tokens
    
    # 如果不需要截断，直接返回
    if original_tokens <= target_tokens:
        return messages, {
            "truncated": False,
            "original_count": original_count,
            "final_count": original_count,
            "original_tokens": raw_tokens,
            "final_tokens": original_tokens,
            "removed_count": 0,
            "calibration_factor": factor,
        }
    
    log.warning(
        "[CONTEXT TRUNCATION] Starting truncation: raw=%s, calibrated=%s (factor=%.2f) -> target %s tokens",
        f"{raw_tokens:,}",
        f"{original_tokens:,}",
        factor,
        f"{target_tokens:,}",
    )
    
    # 分类消息
    classified = classify_messages(messages)
    
    # 必须保留的消息索引
    must_keep_indices = set()
    
    # 1. 保留所有 system 消息
    for idx, _ in classified["system"]:
        must_keep_indices.add(idx)
    
    # 2. 保留最近的工具调用上下文
    tool_messages = classified["tool_context"]
    if tool_messages and protect_tool_rounds > 0:
        # 找出最近 N 轮工具调用
        # 一轮 = 一个 assistant(tool_calls) + 对应的 tool results + assistant(response)
        recent_tool_indices = set()
        rounds_counted = 0
        
        # 从后向前遍历工具消息
        for idx, msg in reversed(tool_messages):
            recent_tool_indices.add(idx)
            # 如果是 tool 消息，计为一轮的一部分
            role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
            if role == "tool":
                rounds_counted += 0.5  # tool 消息算半轮
            elif role == "assistant":
                rounds_counted += 0.5  # assistant 消息算半轮
            
            if rounds_counted >= protect_tool_rounds:
                break
        
        must_keep_indices.update(recent_tool_indices)
    
    # 3. 保留最近的普通消息
    regular_messages = classified["regular"]
    # 从后向前保留，直到满足 min_keep
    kept_regular_count = 0
    for idx, _ in reversed(regular_messages):
        if idx not in must_keep_indices:
            must_keep_indices.add(idx)
            kept_regular_count += 1
            if kept_regular_count >= min_keep:
                break
    
    # 计算当前保留消息的 token 数
    current_tokens = 0
    for idx in must_keep_indices:
        if idx < len(messages):
            current_tokens += estimate_message_tokens(messages[idx])
    
    # 4. 如果还有空间，继续从后向前添加更多消息
    remaining_budget = target_tokens - current_tokens
    
    if remaining_budget > 0:
        # 所有未添加的消息索引，按倒序排列（优先保留最近的）
        all_indices = set(range(len(messages)))
        remaining_indices = sorted(all_indices - must_keep_indices, reverse=True)
        
        for idx in remaining_indices:
            msg_tokens = estimate_message_tokens(messages[idx])
            if msg_tokens <= remaining_budget:
                must_keep_indices.add(idx)
                remaining_budget -= msg_tokens
    
    # 构建截断后的消息列表（保持原始顺序）
    truncated = []
    for i in range(len(messages)):
        if i in must_keep_indices:
            truncated.append(messages[i])
    
    # 统计信息
    final_raw_tokens = estimate_messages_tokens(truncated)
    _, final_tokens, _ = estimate_messages_tokens_calibrated(truncated)
    stats = {
        "truncated": True,
        "original_count": original_count,
        "final_count": len(truncated),
        "original_tokens": raw_tokens,
        "final_tokens": final_tokens,
        "removed_count": original_count - len(truncated),
        "system_kept": len(classified["system"]),
        "tool_context_kept": len([i for i, _ in classified["tool_context"] if i in must_keep_indices]),
        "final_raw_tokens": final_raw_tokens,
        "calibration_factor": factor,
    }
    
    log.info(f"[CONTEXT TRUNCATION] Truncation complete: "
             f"{original_count} -> {len(truncated)} messages, "
             f"{original_tokens:,} -> {final_tokens:,} tokens, "
             f"removed {stats['removed_count']} messages")
    
    return truncated, stats


def truncate_messages_aggressive(
    messages: List[Any],
    target_tokens: int = TARGET_TOKEN_LIMIT // 2,  # 更激进的目标
    keep_last_n_user_messages: int = 3,  # [FIX 2026-02-05] 从 1 提升到 3
) -> Tuple[List[Any], Dict[str, Any]]:
    """
    激进截断策略 - 用于 MAX_TOKENS 错误后的重试

    [FIX 2026-02-05] 增强版：
    1. 保留最近 3 条用户消息（约 1.5 轮对话）而非仅 1 条
    2. 强制保护进行中的工具调用链（不受 token 预算限制）
    3. 确保 tool_use 和 tool_result 成对保留

    保留策略：
    1. System 消息（必须）
    2. 最近 N 条用户消息及其响应
    3. 进行中的工具调用链（强制保护）

    Args:
        messages: OpenAI 格式的消息列表
        target_tokens: 目标 token 数量（更低的值）
        keep_last_n_user_messages: 保留最近 N 条用户消息

    Returns:
        (truncated_messages, stats)
    """
    original_count = len(messages)
    original_tokens = estimate_messages_tokens(messages)

    # 收集需要保留的消息索引
    must_keep_indices = set()

    # 1. 保留 system 消息
    for i, msg in enumerate(messages):
        role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
        if role == "system":
            must_keep_indices.add(i)

    # 2. [FIX 2026-02-05] 找到最近 N 条用户消息（不包括 tool_result 类型）
    user_message_indices = []
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
        if role == "user":
            # 检查是否是工具结果消息（Anthropic 格式可能在 user 消息中包含 tool_result）
            content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
            is_tool_result = False
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        is_tool_result = True
                        break
            tool_call_id = getattr(msg, "tool_call_id", None) or (msg.get("tool_call_id") if isinstance(msg, dict) else None)
            if tool_call_id:
                is_tool_result = True

            if not is_tool_result:
                user_message_indices.append(i)
                if len(user_message_indices) >= keep_last_n_user_messages:
                    break

    # 保留这些用户消息及其之后的所有消息
    if user_message_indices:
        earliest_user_idx = min(user_message_indices)
        for i in range(earliest_user_idx, len(messages)):
            must_keep_indices.add(i)

    # 3. [FIX 2026-02-05] 强制保护进行中的工具调用链
    # 识别所有工具轮次，确保 tool_use 和 tool_result 成对保留
    tool_rounds = identify_tool_rounds(messages)

    # 检查最后几轮是否完整
    incomplete_round_indices = set()
    for round_obj in tool_rounds[-3:]:  # 检查最近 3 轮
        # 如果轮次不完整或者与保留范围有交集，强制保留整个轮次
        if not round_obj.is_complete() or any(idx in must_keep_indices for idx in round_obj.indices):
            for idx in round_obj.indices:
                incomplete_round_indices.add(idx)

    # 合并：强制保护不完整的工具调用轮次
    must_keep_indices.update(incomplete_round_indices)

    # 4. [FIX 2026-02-05] 确保 tool_use 和 tool_result 成对
    # 遍历 must_keep_indices，如果有 tool_result，确保对应的 tool_use 也被保留
    for i in list(must_keep_indices):
        msg = messages[i]
        role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)

        # 如果是 tool 消息或包含 tool_result，向前查找对应的 tool_use
        if role == "tool" or _has_tool_result(msg):
            for j in range(i - 1, -1, -1):
                prev_msg = messages[j]
                if _has_tool_use(prev_msg):
                    must_keep_indices.add(j)
                    break

        # 如果是包含 tool_use 的 assistant 消息，向后查找对应的 tool_result
        if _has_tool_use(msg):
            for j in range(i + 1, len(messages)):
                next_msg = messages[j]
                if _has_tool_result(next_msg):
                    must_keep_indices.add(j)
                else:
                    # 遇到非 tool_result 消息就停止
                    next_role = getattr(next_msg, "role", None) or (next_msg.get("role") if isinstance(next_msg, dict) else None)
                    if next_role == "user" and not _has_tool_result(next_msg):
                        break

    # 构建截断后的消息列表（保持原始顺序）
    truncated = [messages[i] for i in sorted(must_keep_indices)]

    final_tokens = estimate_messages_tokens(truncated)
    stats = {
        "truncated": True,
        "aggressive": True,
        "original_count": original_count,
        "final_count": len(truncated),
        "original_tokens": original_tokens,
        "final_tokens": final_tokens,
        "removed_count": original_count - len(truncated),
        "kept_user_messages": len(user_message_indices),
        "protected_tool_indices": len(incomplete_round_indices),
    }

    log.warning(f"[CONTEXT TRUNCATION] Aggressive truncation (enhanced): "
                f"{original_count} -> {len(truncated)} messages, "
                f"{original_tokens:,} -> {final_tokens:,} tokens, "
                f"protected {len(incomplete_round_indices)} tool-related indices")

    return truncated, stats


# ====================== 工具结果压缩 ======================

# ================== [FIX 2026-01-15] AM兼容智能压缩 ==================
# 同步自 Antigravity-Manager/src-tauri/src/proxy/mappers/tool_result_compressor.rs

import re as _re

def deep_clean_html(html: str) -> str:
    """
    [FIX 2026-01-15] 深度清理 HTML (移除 style, script, base64 等)
    
    同步自 AM tool_result_compressor.rs:deep_clean_html
    
    Args:
        html: 原始 HTML 内容
        
    Returns:
        清理后的 HTML
    """
    result = html
    
    # 1. 移除 <style>...</style> 及其内容
    result = _re.sub(r'(?is)<style\b[^>]*>.*?</style>', '[style omitted]', result)
    
    # 2. 移除 <script>...</script> 及其内容
    result = _re.sub(r'(?is)<script\b[^>]*>.*?</script>', '[script omitted]', result)
    
    # 3. 移除 inline Base64 数据 (如 src="data:image/png;base64,...")
    result = _re.sub(r'data:[^;/]+/[^;]+;base64,[A-Za-z0-9+/=]+', '[base64 omitted]', result)
    
    # 4. 移除 SVG 内容 (通常很长)
    result = _re.sub(r'(?is)<svg\b[^>]*>.*?</svg>', '[svg omitted]', result)
    
    # 5. 移除冗余的空白字符
    result = _re.sub(r'\n\s*\n', '\n', result)
    
    return result


def is_browser_snapshot(text: str) -> bool:
    """
    [FIX 2026-01-15] 检测是否是浏览器快照
    
    同步自 AM tool_result_compressor.rs:compact_browser_snapshot 检测逻辑
    
    Args:
        text: 待检测的文本
        
    Returns:
        是否是浏览器快照
    """
    lower = text.lower()
    return (
        'page snapshot' in lower
        or '页面快照' in text
        or text.count('ref=') > 30
        or text.count('[ref=') > 30
    )


def compact_saved_output_notice(text: str, max_chars: int) -> Optional[str]:
    """
    [FIX 2026-01-15] 压缩"输出已保存到文件"类型的提示
    
    同步自 AM tool_result_compressor.rs:compact_saved_output_notice
    
    检测模式: "result (N characters) exceeds maximum allowed tokens. Output saved to <path>"
    策略: 提取关键信息(文件路径、字符数、格式说明)
    
    Args:
        text: 原始文本
        max_chars: 最大字符数
        
    Returns:
        压缩后的文本，如果不是保存输出模式则返回 None
    """
    pattern = r'(?i)result\s*\(\s*(?P<count>[\d,]+)\s*characters\s*\)\s*exceeds\s+maximum\s+allowed\s+tokens\.\s*Output\s+(?:has\s+been\s+)?saved\s+to\s+(?P<path>[^\r\n]+)'
    
    match = _re.search(pattern, text)
    if not match:
        return None
    
    count = match.group('count')
    raw_path = match.group('path')
    
    # 清理文件路径
    file_path = raw_path.strip().rstrip(')]\"\'.').strip()
    
    # 提取关键行
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    
    # 查找通知行
    notice_line = None
    for line in lines:
        if 'exceeds maximum allowed tokens' in line.lower() and 'saved to' in line.lower():
            notice_line = line
            break
    
    if not notice_line:
        notice_line = f"result ({count} characters) exceeds maximum allowed tokens. Output has been saved to {file_path}"
    
    # 查找格式说明行
    format_line = None
    for line in lines:
        if line.startswith('Format:') or 'JSON array with schema' in line or line.lower().startswith('schema:'):
            format_line = line
            break
    
    # 构建压缩后的输出
    compact_lines = [notice_line]
    if format_line and format_line != notice_line:
        compact_lines.append(format_line)
    compact_lines.append(f"[tool_result omitted to reduce prompt size; read file locally if needed: {file_path}]")
    
    result = '\n'.join(compact_lines)
    return result[:max_chars] if len(result) > max_chars else result


def compact_browser_snapshot(text: str, max_chars: int) -> Optional[str]:
    """
    [FIX 2026-01-15] 压缩浏览器快照 (头+尾保留策略)
    
    同步自 AM tool_result_compressor.rs:compact_browser_snapshot
    
    检测: "page snapshot" 或 "页面快照" 或大量 "ref=" 引用
    策略: 保留头部 70% + 尾部 40%,中间省略
    
    Args:
        text: 原始文本
        max_chars: 最大字符数
        
    Returns:
        压缩后的文本，如果不是浏览器快照则返回 None
    """
    if not is_browser_snapshot(text):
        return None
    
    desired_max = min(max_chars, SNAPSHOT_MAX_CHARS)
    if desired_max < 2000 or len(text) <= desired_max:
        return None
    
    meta = f"[page snapshot summarized to reduce prompt size; original {len(text):,} chars]"
    overhead = len(meta) + 200
    budget = desired_max - overhead
    
    if budget < 1000:
        return None
    
    # 计算头部和尾部长度
    head_len = min(int(budget * SNAPSHOT_HEAD_RATIO), 10000)
    head_len = max(head_len, 500)
    tail_len = min(budget - head_len, 3000)
    
    head = text[:head_len]
    tail = text[-tail_len:] if tail_len > 0 and len(text) > head_len else ""
    
    omitted = len(text) - head_len - tail_len
    
    if tail:
        return f"{meta}\n---[HEAD]---\n{head}\n---[...omitted {omitted:,} chars]---\n---[TAIL]---\n{tail}"
    else:
        return f"{meta}\n---[HEAD]---\n{head}\n---[...omitted {omitted:,} chars]---"


def compress_tool_result(content: str, max_length: int = None) -> str:
    """
    [FIX 2026-01-15] 智能压缩工具结果内容
    
    同步自 AM tool_result_compressor.rs:compact_tool_result_text
    
    根据内容类型自动选择最佳压缩策略:
    1. HTML 预清理 → 移除 style, script, base64, svg
    2. 大文件提示 → 提取关键信息
    3. 浏览器快照 → 头 70% + 尾 40% 保留
    4. 普通截断 → 头 70% + 尾 40% 保留
    
    Args:
        content: 工具结果内容
        max_length: 最大保留长度 (默认 200,000)
        
    Returns:
        压缩后的内容
    """
    if max_length is None:
        max_length = MAX_TOOL_RESULT_CHARS
    
    if not content or len(content) <= max_length:
        return content
    
    original_len = len(content)
    
    # 1. [NEW] 针对可能的 HTML 内容进行深度预处理
    if '<html' in content or '<body' in content or '<!DOCTYPE' in content:
        content = deep_clean_html(content)
        if len(content) != original_len:
            log.debug(f"[TOOL COMPRESS] Deep cleaned HTML: {original_len:,} -> {len(content):,} chars")
        if len(content) <= max_length:
            return content
    
    # 2. 检测大文件提示模式
    compacted = compact_saved_output_notice(content, max_length)
    if compacted:
        log.debug(f"[TOOL COMPRESS] Detected saved output notice, compacted to {len(compacted):,} chars")
        return compacted
    
    # 3. 检测浏览器快照模式
    if len(content) > SNAPSHOT_DETECTION_THRESHOLD:
        compacted = compact_browser_snapshot(content, max_length)
        if compacted:
            log.debug(f"[TOOL COMPRESS] Detected browser snapshot, compacted to {len(compacted):,} chars")
            return compacted
    
    # 4. 普通截断 (头 70% + 尾 40%，总和超过 100% 时按比例调整)
    head_ratio = COMPRESS_HEAD_RATIO
    tail_ratio = COMPRESS_TAIL_RATIO
    total_ratio = head_ratio + tail_ratio
    
    # 如果总比例超过 100%，按比例缩放
    if total_ratio > 1.0:
        head_ratio = head_ratio / total_ratio
        tail_ratio = tail_ratio / total_ratio
    
    head_len = int(max_length * head_ratio)
    tail_len = int(max_length * tail_ratio)
    
    truncation_notice = (
        f"\n\n[... Content truncated: {len(content) - head_len - tail_len:,} characters removed. "
        f"Original length: {len(content):,} characters ...]\n\n"
    )
    
    result = content[:head_len] + truncation_notice + content[-tail_len:]
    log.debug(f"[TOOL COMPRESS] Normal truncation: {len(content):,} -> {len(result):,} chars")
    
    return result


def compress_tool_results_in_messages(
    messages: List[Any],
    max_result_length: int = None,  # [FIX 2026-01-15] 默认使用 MAX_TOOL_RESULT_CHARS (200K)
) -> Tuple[List[Any], int]:
    """
    压缩消息列表中的工具结果
    
    [FIX 2026-01-15] 默认限制从 5K 提升到 200K (AM 兼容)
    [FIX 2026-01-XX] 添加异常处理，防止压缩失败导致网关500错误
    
    Args:
        messages: OpenAI 格式的消息列表
        max_result_length: 单个工具结果的最大长度 (默认 200,000)
        
    Returns:
        (compressed_messages, chars_saved)
    """
    if max_result_length is None:
        max_result_length = MAX_TOOL_RESULT_CHARS
    
    compressed = []
    total_saved = 0
    
    for msg in messages:
        # 获取消息属性
        try:
            if hasattr(msg, "role"):
                role = getattr(msg, "role", "")
                content = getattr(msg, "content", "")
                tool_call_id = getattr(msg, "tool_call_id", None)
            elif isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content", "")
                tool_call_id = msg.get("tool_call_id")
            else:
                compressed.append(msg)
                continue
            
            # 只处理工具结果消息
            if role == "tool" or tool_call_id:
                if isinstance(content, str) and len(content) > max_result_length:
                    try:
                        original_len = len(content)
                        compressed_content = compress_tool_result(content, max_result_length)
                        
                        # 创建新消息
                        if hasattr(msg, "role"):
                            from akarins_gateway.models import OpenAIChatMessage
                            new_msg = OpenAIChatMessage(
                                role=role,
                                content=compressed_content,
                                tool_call_id=tool_call_id,
                                name=getattr(msg, "name", None),
                            )
                        else:
                            new_msg = msg.copy()
                            new_msg["content"] = compressed_content
                        
                        compressed.append(new_msg)
                        total_saved += original_len - len(compressed_content)
                        log.info(f"[TOOL COMPRESS] Compressed tool result: {original_len:,} -> {len(compressed_content):,} chars")
                        continue
                    except Exception as compress_err:
                        # [FIX 2026-01-XX] 压缩失败时记录警告但继续处理，避免导致整个请求失败
                        log.warning(f"[TOOL COMPRESS] Failed to compress tool result (len={len(content) if isinstance(content, str) else 'N/A'}): {compress_err}. Using original content.", exc_info=True)
                        # 如果压缩失败，使用原始消息
                        compressed.append(msg)
                        continue
            
            compressed.append(msg)
        except Exception as msg_err:
            # [FIX 2026-01-XX] 处理单个消息时出错，记录警告但继续处理其他消息
            log.warning(f"[TOOL COMPRESS] Failed to process message: {msg_err}. Skipping message.", exc_info=True)
            # 如果处理失败，跳过该消息（避免破坏整个消息列表）
            continue
    
    if total_saved > 0:
        log.info(f"[TOOL COMPRESS] Total saved: {total_saved:,} characters from tool results")
    
    return compressed, total_saved


# ====================== 综合截断函数 ======================

def truncate_context_for_api(
    messages: List[Any],
    target_tokens: int = TARGET_TOKEN_LIMIT,
    compress_tools: bool = True,
    tool_max_length: int = 5000,
) -> Tuple[List[Any], Dict[str, Any]]:
    """
    为 API 请求截断上下文
    
    综合使用多种策略：
    1. 先压缩工具结果
    2. 再智能截断消息
    
    [FIX 2026-01-XX] 添加异常处理，防止压缩/截断失败导致网关500错误
    
    Args:
        messages: OpenAI 格式的消息列表
        target_tokens: 目标 token 数量
        compress_tools: 是否压缩工具结果
        tool_max_length: 工具结果最大长度
        
    Returns:
        (truncated_messages, stats)
    """
    stats = {
        "original_messages": len(messages),
        "original_tokens": estimate_messages_tokens(messages),
    }
    
    try:
        # Step 1: 压缩工具结果
        if compress_tools:
            try:
                messages, chars_saved = compress_tool_results_in_messages(messages, tool_max_length)
                stats["tool_chars_saved"] = chars_saved
                stats["after_tool_compress_tokens"] = estimate_messages_tokens(messages)
            except Exception as compress_err:
                # [FIX 2026-01-XX] 压缩失败时记录警告但继续处理，避免导致整个请求失败
                log.warning(f"[CONTEXT TRUNCATION] Tool compression failed: {compress_err}. Skipping compression.", exc_info=True)
                stats["tool_chars_saved"] = 0
                stats["after_tool_compress_tokens"] = stats["original_tokens"]
                stats["compression_error"] = str(compress_err)
        
        # Step 2: 检查是否需要截断
        current_tokens = estimate_messages_tokens(messages)
        if current_tokens <= target_tokens:
            stats["truncated"] = False
            stats["final_messages"] = len(messages)
            stats["final_tokens"] = current_tokens
            return messages, stats
        
        # Step 3: 智能截断
        try:
            truncated, truncation_stats = truncate_messages_smart(
                messages,
                target_tokens=target_tokens,
            )

            # [FIX 2026-02-05] Step 3.5: 确保工具链原子性配对
            truncated = _ensure_tool_chain_atomic(truncated)

            stats.update(truncation_stats)
            stats["final_messages"] = len(truncated)
            stats["final_tokens"] = estimate_messages_tokens(truncated)

            return truncated, stats
        except Exception as truncate_err:
            # [FIX 2026-01-XX] 截断失败时记录警告但返回原始消息，避免导致整个请求失败
            log.warning(f"[CONTEXT TRUNCATION] Message truncation failed: {truncate_err}. Using original messages.", exc_info=True)
            stats["truncated"] = False
            stats["final_messages"] = len(messages)
            stats["final_tokens"] = current_tokens
            stats["truncation_error"] = str(truncate_err)
            return messages, stats
    except Exception as e:
        # [FIX 2026-01-XX] 顶层异常处理：如果整个截断过程失败，返回原始消息
        log.error(f"[CONTEXT TRUNCATION] Context truncation failed completely: {e}. Using original messages.", exc_info=True)
        stats["truncated"] = False
        stats["final_messages"] = len(messages)
        stats["final_tokens"] = stats["original_tokens"]
        stats["error"] = str(e)
        return messages, stats


# ====================== MAX_TOKENS 重试支持 ======================

def prepare_retry_after_max_tokens(
    messages: List[Any],
    previous_tokens: int = 0,
) -> Tuple[List[Any], Dict[str, Any]]:
    """
    在 MAX_TOKENS 错误后准备重试
    
    使用激进截断策略，大幅减少上下文
    
    Args:
        messages: 原始消息列表
        previous_tokens: 上次请求的实际 token 数（来自 API 响应）
        
    Returns:
        (truncated_messages, stats)
    """
    # 根据上次的实际 token 数计算新的目标
    if previous_tokens > 0:
        # 减少到上次的 50%
        target = previous_tokens // 2
    else:
        # 使用默认激进目标
        target = TARGET_TOKEN_LIMIT // 2
    
    return truncate_messages_aggressive(messages, target_tokens=target)

# ====================== 智能预防性截断 ======================

def smart_preemptive_truncation(
    messages: List[Any],
    max_output_tokens: int = 16384,
    api_context_limit: int = 128000,
    safety_margin: float = 0.85,
) -> Tuple[List[Any], Dict[str, Any]]:
    """
    智能预防性截断 - 根据 API 限制和预期输出动态调整
    
    [FIX 2026-01-10] 增强版截断策略：
    - 考虑预期输出 token 数
    - 动态计算安全的输入 token 上限
    - 提供更详细的截断统计
    
    Args:
        messages: OpenAI 格式的消息列表
        max_output_tokens: 预期的最大输出 token 数
        api_context_limit: API 的总上下文限制
        safety_margin: 安全边际系数（默认 85%）
        
    Returns:
        (truncated_messages, stats)
    """
    # 计算安全的输入 token 上限
    # 公式：safe_input = (api_limit * safety_margin) - max_output
    safe_input_limit = int(api_context_limit * safety_margin) - max_output_tokens
    safe_input_limit = max(safe_input_limit, 10000)  # 至少保留 10K tokens
    
    current_tokens = estimate_messages_tokens(messages)
    
    stats = {
        "api_context_limit": api_context_limit,
        "max_output_tokens": max_output_tokens,
        "safe_input_limit": safe_input_limit,
        "original_tokens": current_tokens,
        "truncated": False,
    }
    
    if current_tokens <= safe_input_limit:
        stats["final_tokens"] = current_tokens
        stats["action"] = "none"
        return messages, stats
    
    # 需要截断
    log.warning(f"[SMART TRUNCATION] 需要截断: {current_tokens:,} tokens > {safe_input_limit:,} 安全限制 "
               f"(API限制={api_context_limit:,}, 预期输出={max_output_tokens:,})")
    
    # 首先尝试普通截断
    truncated, truncation_stats = truncate_context_for_api(
        messages,
        target_tokens=safe_input_limit,
        compress_tools=True,
        tool_max_length=5000,
    )
    
    final_tokens = estimate_messages_tokens(truncated)
    
    # 如果普通截断不够，使用激进截断
    if final_tokens > safe_input_limit:
        log.warning(f"[SMART TRUNCATION] 普通截断不足 ({final_tokens:,} > {safe_input_limit:,})，使用激进截断")
        truncated, aggressive_stats = truncate_messages_aggressive(
            messages,
            target_tokens=safe_input_limit,
        )
        final_tokens = estimate_messages_tokens(truncated)
        stats["action"] = "aggressive"
        stats["aggressive_stats"] = aggressive_stats
    else:
        stats["action"] = "normal"
        stats["truncation_stats"] = truncation_stats
    
    stats["truncated"] = True
    stats["final_tokens"] = final_tokens
    stats["tokens_removed"] = current_tokens - final_tokens
    
    log.info(f"[SMART TRUNCATION] 截断完成: {current_tokens:,} -> {final_tokens:,} tokens "
            f"(移除 {stats['tokens_removed']:,}, 策略={stats['action']})")
    
    return truncated, stats


def should_retry_with_aggressive_truncation(
    finish_reason: str,
    output_tokens: int,
    retry_count: int = 0,
    max_retries: int = 1,
) -> bool:
    """
    判断是否应该使用激进截断策略重试
    
    Args:
        finish_reason: API 返回的 finish_reason
        output_tokens: 实际输出的 token 数
        retry_count: 当前重试次数
        max_retries: 最大重试次数
        
    Returns:
        是否应该重试
    """
    # 已达到最大重试次数
    if retry_count >= max_retries:
        log.warning(f"[RETRY CHECK] 已达到最大重试次数 ({max_retries})，不再重试")
        return False
    
    # 检查是否因为 MAX_TOKENS 被截断
    if finish_reason != "MAX_TOKENS" and finish_reason != "length":
        return False
    
    # 如果输出 token 很少（<1000），可能是输入太长导致的
    # 这种情况值得重试
    if output_tokens < 1000:
        log.info(f"[RETRY CHECK] 输出 token 很少 ({output_tokens})，建议使用激进截断重试")
        return True
    
    # 如果输出 token 接近上限（>4000），说明是正常的输出截断
    # 这种情况重试意义不大
    if output_tokens >= 4000:
        log.debug(f"[RETRY CHECK] 输出 token 接近上限 ({output_tokens})，不建议重试")
        return False

    return False



# ====================== [FIX 2026-02-05] IDE 预压缩冲突检测 ======================
# Cursor/Augment/Windsurf 等 IDE 可能在请求到达网关前已进行上下文压缩
# 我们需要检测这种情况，避免双重压缩导致上下文过度丢失
#
# [FIX 2026-02-05] 重要区分：
# - IDE 客户端（Cursor, Augment, Windsurf）：可能会预压缩，需要检测
# - CLI 工具（Claude Code, Cline, Aider）：不会预压缩，跳过检测
#
# 原因：AM 只对接 Claude Code CLI，不需要此检测；gcli2api 需要适配多种客户端

# 需要 IDE 预压缩检测的客户端类型（IDE 客户端可能会在发送前压缩上下文）
IDE_CLIENTS_NEED_COMPRESSION_DETECTION = {
    "cursor",           # Cursor IDE - 已知会压缩上下文
    "augment",          # Augment/Bugment - 可能会压缩
    "windsurf",         # Windsurf IDE - 可能会压缩
    "copilot",          # GitHub Copilot - 可能会压缩
    "zed",              # Zed Editor - 可能会压缩
    "unknown",          # 未知客户端 - 保守策略，启用检测
}

# CLI 工具不需要 IDE 预压缩检测（它们有自己的状态管理，不会预压缩）
CLI_TOOLS_SKIP_COMPRESSION_DETECTION = {
    "claude_code",      # Claude Code CLI - 有自己的状态管理
    "cline",            # Cline VSCode 扩展 - 有自己的状态管理
    "aider",            # Aider CLI - 有自己的状态管理
    "continue_dev",     # Continue.dev - 有自己的状态管理
    "openai_api",       # 标准 API 调用 - 无状态，不需要检测
}

def should_detect_ide_pre_compression(client_type: Optional[str] = None) -> bool:
    """
    [FIX 2026-02-05] 判断是否需要对该客户端类型启用 IDE 预压缩检测

    Args:
        client_type: 客户端类型字符串（如 "cursor", "claude_code"）

    Returns:
        是否需要启用 IDE 预压缩检测
    """
    if client_type is None:
        # 未知客户端类型，保守策略：启用检测
        return True

    client_type_lower = client_type.lower()

    # CLI 工具明确跳过检测
    if client_type_lower in CLI_TOOLS_SKIP_COMPRESSION_DETECTION:
        log.debug(
            f"[IDE PRE-COMPRESSION] Skipping detection for CLI tool: {client_type}"
        )
        return False

    # IDE 客户端启用检测
    if client_type_lower in IDE_CLIENTS_NEED_COMPRESSION_DETECTION:
        log.debug(
            f"[IDE PRE-COMPRESSION] Enabling detection for IDE client: {client_type}"
        )
        return True

    # 默认：不在已知列表中的客户端，启用检测（保守策略）
    log.debug(
        f"[IDE PRE-COMPRESSION] Unknown client type '{client_type}', enabling detection (conservative)"
    )
    return True


@dataclass
class IDEPreCompressionIndicators:
    """
    IDE 预压缩检测指标

    用于检测 Cursor/Claude Code 等 IDE 是否已经对上下文进行了压缩。
    如果检测到 IDE 已压缩，网关应该跳过或减少自己的压缩操作。
    """
    orphan_tool_use: bool = False           # tool_use 没有对应的 tool_result
    orphan_tool_result: bool = False        # tool_result 没有对应的 tool_use
    missing_signatures: bool = False        # thinking 块缺少 signature
    compressed_thinking_detected: bool = False  # thinking 文本异常短（已被压缩）
    abnormal_message_pattern: bool = False  # 消息序列异常（可能被删除过）

    # 统计信息
    total_thinking_blocks: int = 0
    thinking_without_signature: int = 0
    ultra_short_thinking_count: int = 0     # thinking 文本 <= 10 字符
    orphan_tool_use_count: int = 0
    orphan_tool_result_count: int = 0

    def has_conflict(self) -> bool:
        """是否检测到冲突"""
        return (
            self.orphan_tool_use
            or self.orphan_tool_result
            or self.missing_signatures
            or self.compressed_thinking_detected
        )

    def get_conflict_reasons(self) -> List[str]:
        """获取冲突原因列表"""
        reasons = []
        if self.orphan_tool_use:
            reasons.append(f"orphan_tool_use({self.orphan_tool_use_count})")
        if self.orphan_tool_result:
            reasons.append(f"orphan_tool_result({self.orphan_tool_result_count})")
        if self.missing_signatures:
            reasons.append(f"missing_signatures({self.thinking_without_signature}/{self.total_thinking_blocks})")
        if self.compressed_thinking_detected:
            reasons.append(f"compressed_thinking({self.ultra_short_thinking_count})")
        if self.abnormal_message_pattern:
            reasons.append("abnormal_message_pattern")
        return reasons


def detect_ide_pre_compression(messages: List[Any]) -> IDEPreCompressionIndicators:
    """
    [FIX 2026-02-05] 检测 IDE 是否已经进行了上下文压缩

    检测指标：
    1. 孤儿 tool_use - 有 tool_use 但没有对应的 tool_result
    2. 孤儿 tool_result - 有 tool_result 但没有对应的 tool_use
    3. 签名缺失 - thinking 块存在但没有 signature
    4. 压缩的 thinking - thinking 文本异常短（<= 10 字符，可能是 "..." 或被清空）
    5. 异常消息模式 - 连续多个 assistant 消息或其他异常模式

    Args:
        messages: 消息列表

    Returns:
        IDEPreCompressionIndicators 检测结果
    """
    indicators = IDEPreCompressionIndicators()

    if not messages:
        return indicators

    # 收集所有 tool_use_id 和 tool_result 对应的 id
    tool_use_ids = set()
    tool_result_ids = set()

    for msg in messages:
        # 获取消息属性
        if hasattr(msg, "role"):
            role = getattr(msg, "role", "")
            content = getattr(msg, "content", None)
            tool_calls = getattr(msg, "tool_calls", None)
            tool_call_id = getattr(msg, "tool_call_id", None)
        elif isinstance(msg, dict):
            role = msg.get("role", "")
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id")
        else:
            continue

        # 收集 tool_use ids (OpenAI 格式)
        if tool_calls:
            for tc in tool_calls:
                if hasattr(tc, "id"):
                    tool_use_ids.add(getattr(tc, "id"))
                elif isinstance(tc, dict) and tc.get("id"):
                    tool_use_ids.add(tc.get("id"))

        # 收集 tool_result ids (OpenAI 格式)
        if role == "tool" and tool_call_id:
            tool_result_ids.add(tool_call_id)

        # Anthropic 格式：检查 content 数组
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue

                block_type = block.get("type", "")

                # 收集 tool_use ids (Anthropic 格式)
                if block_type == "tool_use":
                    block_id = block.get("id")
                    if block_id:
                        tool_use_ids.add(block_id)

                # 收集 tool_result ids (Anthropic 格式)
                if block_type == "tool_result":
                    block_tool_use_id = block.get("tool_use_id")
                    if block_tool_use_id:
                        tool_result_ids.add(block_tool_use_id)

                # 检查 thinking 块
                if block_type in ("thinking", "redacted_thinking") or block.get("thought") is True:
                    indicators.total_thinking_blocks += 1

                    # 检查 signature
                    signature = block.get("signature")
                    if not signature:
                        indicators.thinking_without_signature += 1

                    # 检查 thinking 文本长度
                    thinking_text = block.get("thinking") or block.get("text") or ""
                    if len(thinking_text) <= 10:
                        indicators.ultra_short_thinking_count += 1

    # 计算孤儿 tool_use 和 tool_result
    orphan_use = tool_use_ids - tool_result_ids
    orphan_result = tool_result_ids - tool_use_ids

    indicators.orphan_tool_use_count = len(orphan_use)
    indicators.orphan_tool_result_count = len(orphan_result)
    indicators.orphan_tool_use = len(orphan_use) > 0
    indicators.orphan_tool_result = len(orphan_result) > 0

    # 判断是否有签名缺失问题
    if indicators.total_thinking_blocks > 0:
        # 如果超过 50% 的 thinking 块缺少签名，认为有问题
        missing_ratio = indicators.thinking_without_signature / indicators.total_thinking_blocks
        indicators.missing_signatures = missing_ratio > 0.5

    # 判断是否有压缩的 thinking
    if indicators.total_thinking_blocks > 0:
        # 如果超过 30% 的 thinking 块文本异常短，认为已被压缩
        short_ratio = indicators.ultra_short_thinking_count / indicators.total_thinking_blocks
        indicators.compressed_thinking_detected = short_ratio > 0.3

    # 检查异常消息模式（连续多个相同角色的消息）
    prev_role = None
    consecutive_count = 0
    for msg in messages:
        if hasattr(msg, "role"):
            role = getattr(msg, "role", "")
        elif isinstance(msg, dict):
            role = msg.get("role", "")
        else:
            continue

        if role == prev_role and role in ("assistant", "user"):
            consecutive_count += 1
            if consecutive_count >= 2:
                indicators.abnormal_message_pattern = True
                break
        else:
            consecutive_count = 0
        prev_role = role

    return indicators


def log_ide_compression_detection(indicators: IDEPreCompressionIndicators, message_count: int) -> None:
    """
    [FIX 2026-02-05] 记录 IDE 预压缩检测结果

    Args:
        indicators: 检测结果
        message_count: 消息总数
    """
    if indicators.has_conflict():
        reasons = indicators.get_conflict_reasons()
        log.warning(
            f"[IDE PRE-COMPRESSION DETECTED] Conflict detected in {message_count} messages. "
            f"Reasons: {', '.join(reasons)}. "
            f"Stats: thinking_blocks={indicators.total_thinking_blocks}, "
            f"without_sig={indicators.thinking_without_signature}, "
            f"ultra_short={indicators.ultra_short_thinking_count}, "
            f"orphan_use={indicators.orphan_tool_use_count}, "
            f"orphan_result={indicators.orphan_tool_result_count}. "
            f"Gateway will SKIP or REDUCE compression to respect IDE's decision."
        )
    else:
        log.debug(
            f"[IDE PRE-COMPRESSION CHECK] No conflict detected in {message_count} messages. "
            f"thinking_blocks={indicators.total_thinking_blocks}"
        )


# ====================== [FIX 2026-01-29] 三层渐进式压缩调度器 ======================
# 同步自 Antigravity-Manager/src-tauri/src/proxy/mappers/context_manager.rs

def calculate_context_pressure(
    messages: List[Any],
    model_name: str = "claude",
    max_output_tokens: int = 16384,
) -> Tuple[float, int, int]:
    """
    计算上下文压力值

    压力 = 当前 token 数 / 安全输入限制

    Args:
        messages: 消息列表
        model_name: 模型名称
        max_output_tokens: 预期最大输出 token 数

    Returns:
        (pressure, current_tokens, safe_limit)
        - pressure: 压力值 (0.0 ~ 1.0+)
        - current_tokens: 当前 token 数
        - safe_limit: 安全输入限制
    """
    current_tokens = estimate_messages_tokens(messages)
    safe_limit = get_dynamic_target_limit(model_name, max_output_tokens)

    pressure = current_tokens / safe_limit if safe_limit > 0 else 1.0

    return pressure, current_tokens, safe_limit


def progressive_context_compression(
    messages: List[Any],
    model_name: str = "claude",
    max_output_tokens: int = 16384,
    layer1_threshold: float = LAYER1_PRESSURE_THRESHOLD,
    layer2_threshold: float = LAYER2_PRESSURE_THRESHOLD,
    layer3_threshold: float = LAYER3_PRESSURE_THRESHOLD,
    keep_tool_rounds: int = LAYER1_KEEP_TOOL_ROUNDS,
    protected_thinking_n: int = LAYER2_PROTECTED_LAST_N,
    respect_ide_compression: bool = True,
    client_type: Optional[str] = None,
) -> Tuple[List[Any], Dict[str, Any]]:
    """
    [FIX 2026-01-29] 三层渐进式上下文压缩 (Progressive Context Compression - PCC)

    同步自 AM context_manager.rs 的三层压缩策略

    [FIX 2026-02-05] 新增 IDE 预压缩冲突检测：
    - IDE 客户端（Cursor, Augment, Windsurf）：可能会预压缩，需要检测
    - CLI 工具（Claude Code, Cline, Aider）：不会预压缩，跳过检测
    - 如果检测到 IDE 预压缩，尊重 IDE 的决定，跳过或减少网关压缩

    压力阈值触发规则：
    - Layer 1 (60%): 工具轮次裁剪 - Cache-friendly，只删除消息
    - Layer 2 (75%): Thinking 签名保留压缩 - 会破坏 Cache
    - Layer 3 (90%): XML 摘要 Fork - 会破坏 Cache，需要外部 LLM

    Args:
        messages: 消息列表
        model_name: 模型名称
        max_output_tokens: 预期最大输出 token 数
        layer1_threshold: Layer 1 触发阈值
        layer2_threshold: Layer 2 触发阈值
        layer3_threshold: Layer 3 触发阈值
        keep_tool_rounds: Layer 1 保留的工具轮次数
        protected_thinking_n: Layer 2 保护的最后 N 条消息
        respect_ide_compression: 是否尊重 IDE 预压缩（检测到冲突时跳过网关压缩）
        client_type: 客户端类型（如 "cursor", "claude_code"），用于决定是否启用检测

    Returns:
        (compressed_messages, stats)
        - compressed_messages: 压缩后的消息列表
        - stats: 压缩统计信息
    """
    # [FIX 2026-02-05] IDE 预压缩冲突检测
    # 只对 IDE 客户端（Cursor, Augment 等）启用检测
    # CLI 工具（Claude Code, Cline 等）不会预压缩，跳过检测
    should_detect = respect_ide_compression and should_detect_ide_pre_compression(client_type)

    if should_detect:
        ide_indicators = detect_ide_pre_compression(messages)
        log_ide_compression_detection(ide_indicators, len(messages))

        if ide_indicators.has_conflict():
            # IDE 已经压缩过了，尊重 IDE 的决定
            # 返回原始消息，不进行网关压缩
            pressure, current_tokens, safe_limit = calculate_context_pressure(
                messages, model_name, max_output_tokens
            )
            stats = {
                "model": model_name,
                "client_type": client_type,
                "initial_pressure": pressure,
                "initial_tokens": current_tokens,
                "safe_limit": safe_limit,
                "layers_applied": [],
                "final_pressure": pressure,
                "final_tokens": current_tokens,
                "ide_pre_compression_detected": True,
                "ide_conflict_reasons": ide_indicators.get_conflict_reasons(),
                "ide_indicators": {
                    "orphan_tool_use_count": ide_indicators.orphan_tool_use_count,
                    "orphan_tool_result_count": ide_indicators.orphan_tool_result_count,
                    "total_thinking_blocks": ide_indicators.total_thinking_blocks,
                    "thinking_without_signature": ide_indicators.thinking_without_signature,
                    "ultra_short_thinking_count": ide_indicators.ultra_short_thinking_count,
                },
                "skipped_reason": "IDE pre-compression detected, respecting IDE's decision",
            }
            log.warning(
                f"[PCC] SKIPPED: IDE pre-compression detected. "
                f"Reasons: {', '.join(ide_indicators.get_conflict_reasons())}. "
                f"Gateway compression bypassed to prevent context over-loss."
            )
            return messages, stats

    # 计算初始压力
    pressure, current_tokens, safe_limit = calculate_context_pressure(
        messages, model_name, max_output_tokens
    )

    stats = {
        "model": model_name,
        "client_type": client_type,
        "initial_pressure": pressure,
        "initial_tokens": current_tokens,
        "safe_limit": safe_limit,
        "layers_applied": [],
        "final_pressure": pressure,
        "final_tokens": current_tokens,
        "ide_pre_compression_detected": False,
        "ide_detection_skipped": not should_detect,  # CLI 工具跳过检测
    }

    log.info(
        f"[PCC] Initial pressure: {pressure:.1%} ({current_tokens:,} / {safe_limit:,} tokens)"
    )

    # 如果压力低于 Layer 1 阈值，无需压缩
    if pressure < layer1_threshold:
        log.debug(f"[PCC] Pressure {pressure:.1%} < {layer1_threshold:.0%}, no compression needed")
        return messages, stats

    result_messages = messages

    # ===== Layer 1: 工具轮次裁剪 =====
    if pressure >= layer1_threshold:
        log.info(f"[PCC] Applying Layer 1: Tool round trimming (pressure={pressure:.1%})")

        result_messages, was_trimmed, layer1_stats = trim_tool_messages(
            result_messages, keep_tool_rounds
        )

        if was_trimmed:
            stats["layers_applied"].append("layer1_tool_trim")
            stats["layer1"] = layer1_stats

            # 重新计算压力
            pressure, current_tokens, _ = calculate_context_pressure(
                result_messages, model_name, max_output_tokens
            )
            log.info(f"[PCC] After Layer 1: pressure={pressure:.1%} ({current_tokens:,} tokens)")

    # ===== Layer 2: Thinking 签名保留压缩 =====
    if pressure >= layer2_threshold:
        log.info(f"[PCC] Applying Layer 2: Thinking compression (pressure={pressure:.1%})")

        result_messages, was_compressed, layer2_stats = compress_thinking_preserve_signature(
            result_messages, protected_thinking_n
        )

        if was_compressed:
            stats["layers_applied"].append("layer2_thinking_compress")
            stats["layer2"] = layer2_stats

            # 重新计算压力
            pressure, current_tokens, _ = calculate_context_pressure(
                result_messages, model_name, max_output_tokens
            )
            log.info(f"[PCC] After Layer 2: pressure={pressure:.1%} ({current_tokens:,} tokens)")

    # ===== Layer 3: XML 摘要 Fork =====
    if pressure >= layer3_threshold:
        log.warning(
            f"[PCC] Layer 3 threshold reached (pressure={pressure:.1%}), "
            f"applying enhanced fallback strategy with signature preservation."
        )

        # [FIX 2026-02-05] Layer 3 增强回退策略：
        # 1. 先尝试更激进的 thinking 压缩（保护最后 2 条而非 4 条）
        # 2. 再尝试更激进的工具轮次裁剪（保留 3 轮而非 8 轮）
        # 3. 最后才使用激进截断

        # Step 1: 更激进的 thinking 压缩
        result_messages, was_compressed, layer3_thinking_stats = compress_thinking_preserve_signature(
            result_messages, protected_last_n=2  # 只保护最后 2 条
        )
        if was_compressed:
            stats["layers_applied"].append("layer3_aggressive_thinking")
            stats["layer3_thinking"] = layer3_thinking_stats
            pressure, current_tokens, _ = calculate_context_pressure(
                result_messages, model_name, max_output_tokens
            )
            log.info(f"[PCC] After Layer 3 aggressive thinking: pressure={pressure:.1%}")

        # Step 2: 如果还不够，更激进的工具轮次裁剪
        if pressure >= layer3_threshold:
            result_messages, was_trimmed, layer3_tool_stats = trim_tool_messages(
                result_messages, keep_last_n_rounds=3  # 只保留 3 轮
            )
            if was_trimmed:
                stats["layers_applied"].append("layer3_aggressive_tool_trim")
                stats["layer3_tool"] = layer3_tool_stats
                pressure, current_tokens, _ = calculate_context_pressure(
                    result_messages, model_name, max_output_tokens
                )
                log.info(f"[PCC] After Layer 3 aggressive tool trim: pressure={pressure:.1%}")

        # Step 3: 如果还不够，使用增强版激进截断（保留更多上下文）
        if pressure >= layer3_threshold:
            result_messages, aggressive_stats = truncate_messages_aggressive(
                result_messages,
                target_tokens=safe_limit,
                keep_last_n_user_messages=3  # 保留 3 条用户消息
            )
            stats["layers_applied"].append("layer3_fallback_aggressive")
            stats["layer3_fallback"] = aggressive_stats

            # 重新计算压力
            pressure, current_tokens, _ = calculate_context_pressure(
                result_messages, model_name, max_output_tokens
            )
            log.info(f"[PCC] After Layer 3 fallback: pressure={pressure:.1%} ({current_tokens:,} tokens)")

    # 更新最终统计
    stats["final_pressure"] = pressure
    stats["final_tokens"] = current_tokens

    log.info(
        f"[PCC] Compression complete: {stats['initial_tokens']:,} → {current_tokens:,} tokens "
        f"(pressure: {stats['initial_pressure']:.1%} → {pressure:.1%}), "
        f"layers: {stats['layers_applied']}"
    )

    return result_messages, stats


def apply_pcc_before_request(
    messages: List[Any],
    model_name: str = "claude",
    max_output_tokens: int = 16384,
    compress_tool_results: bool = True,
    tool_result_max_length: int = None,
    client_type: Optional[str] = None,
) -> Tuple[List[Any], Dict[str, Any]]:
    """
    [FIX 2026-01-29] 请求前应用渐进式上下文压缩

    这是对外暴露的主入口函数，整合了工具结果压缩和三层 PCC。

    [FIX 2026-02-05] 新增 client_type 参数：
    - IDE 客户端（Cursor, Augment）：启用 IDE 预压缩检测
    - CLI 工具（Claude Code, Cline）：跳过 IDE 预压缩检测

    处理顺序：
    1. 先压缩工具结果（减少单个消息的大小）
    2. 再应用三层 PCC（减少消息数量和 thinking 内容）

    Args:
        messages: 消息列表
        model_name: 模型名称
        max_output_tokens: 预期最大输出 token 数
        compress_tool_results: 是否压缩工具结果
        tool_result_max_length: 工具结果最大长度
        client_type: 客户端类型（如 "cursor", "claude_code"）

    Returns:
        (compressed_messages, stats)
    """
    if tool_result_max_length is None:
        tool_result_max_length = MAX_TOOL_RESULT_CHARS

    stats = {
        "original_messages": len(messages),
        "original_tokens": estimate_messages_tokens(messages),
        "client_type": client_type,
    }

    result_messages = messages

    # Step 1: 压缩工具结果
    if compress_tool_results:
        result_messages, chars_saved = compress_tool_results_in_messages(
            result_messages, tool_result_max_length
        )
        stats["tool_result_chars_saved"] = chars_saved
        if chars_saved > 0:
            stats["after_tool_compress_tokens"] = estimate_messages_tokens(result_messages)

    # Step 2: 应用三层 PCC
    # [FIX 2026-02-05] 传递 client_type 参数，用于决定是否启用 IDE 预压缩检测
    result_messages, pcc_stats = progressive_context_compression(
        result_messages, model_name, max_output_tokens, client_type=client_type
    )
    stats["pcc"] = pcc_stats

    # 最终统计
    stats["final_messages"] = len(result_messages)
    stats["final_tokens"] = estimate_messages_tokens(result_messages)
    stats["total_tokens_saved"] = stats["original_tokens"] - stats["final_tokens"]

    return result_messages, stats


# =============================================================================
# [FIX 2026-01-29] 签名错误检测与重试支持
# =============================================================================

# 签名相关错误的关键词
SIGNATURE_ERROR_KEYWORDS = [
    "signature",
    "thinking_signature",
    "thoughtsignature",
    "thinking block",
    "thinking validation",
    "invalid thinking",
    "signature mismatch",
    "signature invalid",
]


def is_signature_related_error(error_message: str) -> bool:
    """
    [FIX 2026-01-29] 检测错误是否与签名相关

    Args:
        error_message: 错误消息

    Returns:
        是否为签名相关错误
    """
    if not error_message:
        return False

    error_lower = error_message.lower()
    return any(keyword in error_lower for keyword in SIGNATURE_ERROR_KEYWORDS)


def should_retry_with_signature_fix(
    status_code: int,
    error_message: str,
    retry_count: int = 0,
    max_retries: int = 2,
) -> Tuple[bool, str]:
    """
    [FIX 2026-01-29] 判断是否应该使用签名修复重试

    Args:
        status_code: HTTP 状态码
        error_message: 错误消息
        retry_count: 当前重试次数
        max_retries: 最大重试次数

    Returns:
        (should_retry, reason)
    """
    # 超过重试次数
    if retry_count >= max_retries:
        return False, f"Max retries ({max_retries}) exceeded"

    # 只处理 400 错误
    if status_code != 400:
        return False, f"Status code {status_code} is not 400"

    # 检查是否为签名相关错误
    if is_signature_related_error(error_message):
        return True, "Signature-related error detected, will retry with signature fix"

    return False, "Not a signature-related error"


def prepare_retry_with_signature_fix(
    messages: List[Any],
) -> Tuple[List[Any], Dict[str, Any]]:
    """
    [FIX 2026-01-29] 准备签名修复重试

    通过压缩 thinking 块（保留签名）来修复签名问题。

    Args:
        messages: 原始消息列表

    Returns:
        (fixed_messages, stats)
    """
    stats = {
        "signature_fix": True,
        "original_messages": len(messages),
    }

    # 使用 Layer 2 压缩：保留签名但压缩 thinking 内容
    fixed_messages, modified, compress_stats = compress_thinking_preserve_signature(
        messages, protected_last_n=2  # 保护最后 2 条消息
    )

    stats["thinking_compressed"] = modified
    stats["compress_stats"] = compress_stats

    return fixed_messages, stats


def prepare_retry_with_pcc(
    messages: List[Any],
    model_name: str = "claude",
    max_output_tokens: int = 16384,
) -> Tuple[List[Any], Dict[str, Any]]:
    """
    [FIX 2026-01-29] 使用完整 PCC 准备重试

    当签名修复不足以解决问题时，使用完整的三层压缩。

    Args:
        messages: 原始消息列表
        model_name: 模型名称
        max_output_tokens: 最大输出 token 数

    Returns:
        (compressed_messages, stats)
    """
    return apply_pcc_before_request(
        messages,
        model_name=model_name,
        max_output_tokens=max_output_tokens,
        compress_tool_results=True,
    )