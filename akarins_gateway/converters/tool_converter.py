"""
Tool Converter - Convert tool definitions between OpenAI and Antigravity formats
工具转换器 - 在 OpenAI 和 Antigravity 格式之间转换工具定义

增强功能：
- 工具格式验证：确保工具名称、参数格式符合 Antigravity API 要求
- 自动修复：对于可修复的格式问题，自动进行修复
- 详细日志：记录验证失败的原因，便于调试
"""

from typing import Any, Dict, List, Optional, Tuple

from akarins_gateway.core.log import log
from akarins_gateway.models import GeminiGenerationConfig


# [FIX 2026-02-17] imageSize 参数映射
# 参考 Antigravity-Manager v4.1.18
# 将 OpenAI 格式的 image size 字符串 (如 "1024x1024") 映射到 Gemini 的 imageSize 值 ("1K"/"2K"/"4K")
# 同时兼容已经是 Gemini 格式的值 ("1K"/"2K"/"4K") 直接透传
_OPENAI_IMAGE_SIZE_MAP: Dict[str, Dict[str, int]] = {
    "256x256": {"width": 256, "height": 256},
    "512x512": {"width": 512, "height": 512},
    "1024x1024": {"width": 1024, "height": 1024},
    "1792x1024": {"width": 1792, "height": 1024},
    "1024x1792": {"width": 1024, "height": 1792},
}

# Gemini 原生 imageSize 值白名单（直接透传）
_GEMINI_IMAGE_SIZE_PASSTHROUGH = {"1K", "2K", "4K"}


def map_image_size(size_str: Optional[str]) -> Optional[str]:
    """
    [FIX 2026-02-17] 将 OpenAI 格式的 image size 参数映射到 Gemini imageSize 值。
    参考 Antigravity-Manager v4.1.18。

    映射逻辑（与 antigravity_router.py txt2img 端点保持一致）：
    - max_dimension <= 1024 -> "1K"
    - max_dimension <= 2048 -> "2K"
    - max_dimension >  2048 -> "4K"

    同时兼容已经是 Gemini 格式的值 ("1K"/"2K"/"4K") 直接透传。

    Args:
        size_str: OpenAI 格式的 size 字符串 (如 "1024x1024") 或 Gemini 格式 (如 "2K")

    Returns:
        Gemini imageSize 值 ("1K"/"2K"/"4K")，无效输入返回 None
    """
    if not size_str or not isinstance(size_str, str):
        return None

    normalized = size_str.strip().upper()

    # 如果已经是 Gemini 原生格式，直接透传
    if normalized in _GEMINI_IMAGE_SIZE_PASSTHROUGH:
        return normalized

    # 尝试 OpenAI 格式映射
    lower = size_str.lower().strip()
    dimensions = _OPENAI_IMAGE_SIZE_MAP.get(lower)
    if dimensions:
        max_dimension = max(dimensions["width"], dimensions["height"])
        if max_dimension <= 1024:
            return "1K"
        elif max_dimension <= 2048:
            return "2K"
        else:
            return "4K"

    # 尝试解析任意 "WxH" 格式
    try:
        parts = lower.split("x")
        if len(parts) == 2:
            w, h = int(parts[0]), int(parts[1])
            if w > 0 and h > 0:
                max_dim = max(w, h)
                if max_dim <= 1024:
                    return "1K"
                elif max_dim <= 2048:
                    return "2K"
                else:
                    return "4K"
    except (ValueError, IndexError):
        pass

    log.warning(f"[IMAGE SIZE] Unrecognized image size format: '{size_str}', ignoring")
    return None

# [FIX 2026-02-05] 移除顶层导入避免循环依赖
# DEFAULT_THINKING_BUDGET 改为在 generate_generation_config() 函数内懒加载导入



def normalize_tool_name(name: str) -> str:
    """
    [FIX 2026-02-17] 规范化工具名称用于模糊匹配容错。
    移植自 Antigravity-Manager v4.1.17 fuzzy tool matching。

    规则：
    1. 转小写
    2. 连字符替换为下划线
    3. 移除多余空格

    Args:
        name: 原始工具名称

    Returns:
        规范化后的工具名称
    """
    if not name:
        return ""
    normalized = name.lower()
    normalized = normalized.replace("-", "_")
    normalized = normalized.strip().replace(" ", "_")
    return normalized


# ==================== 双向限制策略常量（移植自 anthropic_converter.py）====================
# [FIX 2026-01-10] 双向限制策略：既要保证足够的输出空间，又不能让 max_tokens 过大触发 429
# [FIX 2026-01-11] 提高输出空间限制，支持写长文档（MD文档可能需要 10K-30K tokens）
MAX_ALLOWED_TOKENS = 65535   # max_tokens 的绝对上限（Claude 最大值）
MIN_OUTPUT_TOKENS = 16384    # 实际输出的最小保障空间（4096 -> 16384，支持长文档输出）
DEFAULT_MAX_OUTPUT_TOKENS = 32768  # 默认的 maxOutputTokens（16384 -> 32768，确保足够的输出空间）


# ==================== 工具格式验证 ====================

def validate_tool_name(name: Any) -> Tuple[bool, str, Optional[str]]:
    """
    验证工具名称是否有效
    
    Args:
        name: 工具名称
    
    Returns:
        (is_valid, error_message, sanitized_name)
    """
    if name is None:
        return False, "Tool name is None", None
    
    if not isinstance(name, str):
        # 尝试转换为字符串
        try:
            name = str(name)
        except Exception:
            return False, f"Tool name cannot be converted to string: {type(name)}", None
    
    if not name.strip():
        return False, "Tool name is empty or whitespace only", None
    
    # 清理工具名称（移除首尾空白）
    sanitized = name.strip()

    # [FIX 2026-02-17] MCP 工具名称模糊匹配容错
    # 如果名称包含连字符，同时保留规范化版本用于匹配
    normalized = normalize_tool_name(sanitized)
    if normalized != sanitized.lower():
        log.debug(f"[TOOL VALIDATOR] 工具名称规范化: '{sanitized}' -> normalized='{normalized}'")
    
    # 检查名称长度（Antigravity API 限制）
    if len(sanitized) > 64:
        return False, f"Tool name too long: {len(sanitized)} > 64 chars", sanitized[:64]
    
    # 检查名称格式（只允许字母、数字、下划线、连字符）
    import re
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9_-]*$', sanitized):
        # [FIX 2026-02-17] 优先使用 normalized 名称作为 fallback（模糊匹配容错）
        # 修复之前 normalize_tool_name 结果被丢弃导致模糊匹配实际无效的问题
        if re.match(r'^[a-zA-Z][a-zA-Z0-9_-]*$', normalized):
            log.info(f"[TOOL VALIDATOR] Used normalized name as fallback: '{sanitized}' -> '{normalized}'")
            return True, "", normalized
        # 尝试修复：替换非法字符
        fixed_name = re.sub(r'[^a-zA-Z0-9_-]', '_', sanitized)
        if not fixed_name[0].isalpha():
            fixed_name = 'tool_' + fixed_name
        log.warning(f"[TOOL VALIDATOR] Fixed invalid tool name: '{sanitized}' -> '{fixed_name}'")
        return True, "", fixed_name
    
    return True, "", sanitized


def validate_tool_parameters(parameters: Any) -> Tuple[bool, str, Dict[str, Any]]:
    """
    验证工具参数格式是否有效
    
    Args:
        parameters: 工具参数定义
    
    Returns:
        (is_valid, error_message, sanitized_parameters)
    """
    # 空参数是有效的
    if parameters is None:
        return True, "", {"type": "object", "properties": {}}
    
    # 非字典类型
    if not isinstance(parameters, dict):
        return False, f"Parameters is not a dict: {type(parameters)}", {"type": "object", "properties": {}}
    
    # 复制参数以避免修改原始对象
    sanitized = dict(parameters)
    
    # 确保有 type 字段（Antigravity API 要求）
    if "type" not in sanitized:
        sanitized["type"] = "object"
        log.debug("[TOOL VALIDATOR] Added default type='object' to parameters")
    
    # 验证 type 字段值
    valid_types = {"object", "string", "number", "integer", "boolean", "array", "null"}
    if sanitized.get("type") not in valid_types:
        log.warning(f"[TOOL VALIDATOR] Invalid parameter type: {sanitized.get('type')}, defaulting to 'object'")
        sanitized["type"] = "object"
    
    # 如果是 object 类型，确保有 properties 字段
    if sanitized.get("type") == "object" and "properties" not in sanitized:
        sanitized["properties"] = {}
        log.debug("[TOOL VALIDATOR] Added empty properties to object type parameters")
    
    # 递归验证嵌套的 properties
    if "properties" in sanitized and isinstance(sanitized["properties"], dict):
        for prop_name, prop_value in list(sanitized["properties"].items()):
            if isinstance(prop_value, dict):
                # 检查嵌套 object 类型
                if prop_value.get("type") == "object" and "properties" not in prop_value:
                    prop_value["properties"] = {}
                    log.debug(f"[TOOL VALIDATOR] Added empty properties to nested object '{prop_name}'")
                
                # 确保嵌套属性有 type 字段
                if "type" not in prop_value:
                    prop_value["type"] = "string"  # 默认为 string
                    log.debug(f"[TOOL VALIDATOR] Added default type='string' to property '{prop_name}'")
    
    return True, "", sanitized


def validate_antigravity_tool(tool: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    验证并修复 Antigravity 工具格式
    
    Args:
        tool: 工具定义字典
    
    Returns:
        (is_valid, error_message, sanitized_tool)
        - is_valid: 是否有效（或可修复）
        - error_message: 错误消息（如果无效）
        - sanitized_tool: 修复后的工具定义（如果有效）
    """
    if not isinstance(tool, dict):
        return False, f"Tool is not a dict: {type(tool)}", None
    
    # 验证工具名称
    name = tool.get("name")
    name_valid, name_error, sanitized_name = validate_tool_name(name)
    if not name_valid:
        return False, f"Invalid tool name: {name_error}", None
    
    # 验证工具参数
    parameters = tool.get("parameters")
    params_valid, params_error, sanitized_params = validate_tool_parameters(parameters)
    if not params_valid:
        log.warning(f"[TOOL VALIDATOR] Tool '{sanitized_name}' has invalid parameters: {params_error}, using default")
        sanitized_params = {"type": "object", "properties": {}}
    
    # 构建修复后的工具定义
    sanitized_tool = {
        "name": sanitized_name,
        "description": str(tool.get("description", "")) or "",
        "parameters": sanitized_params
    }
    
    return True, "", sanitized_tool


def validate_tools_batch(tools: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    批量验证工具列表
    
    Args:
        tools: 工具定义列表
    
    Returns:
        (valid_tools, errors)
        - valid_tools: 有效的工具列表
        - errors: 错误消息列表
    """
    valid_tools = []
    errors = []
    
    for i, tool in enumerate(tools):
        is_valid, error_msg, sanitized_tool = validate_antigravity_tool(tool)
        if is_valid and sanitized_tool:
            valid_tools.append(sanitized_tool)
        else:
            errors.append(f"Tool {i}: {error_msg}")
            log.warning(f"[TOOL VALIDATOR] Skipping invalid tool {i}: {error_msg}")
    
    if errors:
        log.warning(f"[TOOL VALIDATOR] {len(errors)} tools failed validation out of {len(tools)}")
    
    return valid_tools, errors


def extract_tool_params_summary(tools: Optional[List[Any]]) -> str:
    """
    从工具定义中提取参数摘要，用于注入到System Prompt
    帮助模型了解当前正确的工具参数名
    """
    if not tools:
        return ""

    # 重点关注的常用工具
    important_tools = ["read", "read_file", "terminal", "run_terminal_command",
                       "write", "edit", "bash", "str_replace_editor", "execute_command"]

    summaries = []

    for tool in tools:
        try:
            # 获取工具名
            if hasattr(tool, "function"):
                func = tool.function
                tool_name = getattr(func, "name", None) or (func.get("name") if isinstance(func, dict) else None)
                params = getattr(func, "parameters", None) or (func.get("parameters") if isinstance(func, dict) else None)
            elif isinstance(tool, dict) and "function" in tool:
                func = tool["function"]
                tool_name = func.get("name")
                params = func.get("parameters")
            else:
                continue

            if not tool_name:
                continue

            # 只处理重要工具或名称中包含关键词的工具
            is_important = any(imp in tool_name.lower() for imp in important_tools)
            if not is_important:
                continue

            # 提取参数名
            if params and isinstance(params, dict):
                properties = params.get("properties", {})
                required = params.get("required", [])

                if properties:
                    param_list = []
                    for param_name in properties.keys():
                        if param_name in required:
                            param_list.append(f"`{param_name}` (required)")
                        else:
                            param_list.append(f"`{param_name}`")

                    if param_list:
                        summaries.append(f"- {tool_name}: {', '.join(param_list)}")
        except Exception:
            continue

    if summaries:
        return "\n\nCurrent tool parameters (use ONLY these exact names):\n" + "\n".join(summaries)
    return ""


def convert_openai_tools_to_antigravity(tools: Optional[List[Any]]) -> Optional[List[Dict[str, Any]]]:
    """
    将 OpenAI 工具定义转换为 Antigravity 格式

    支持两种输入格式：
    1. Pydantic 模型对象（使用 getattr）
    2. 普通字典（使用 .get()）
    """
    if not tools:
        return None

    # 需要排除的字段
    EXCLUDED_KEYS = {'$schema', 'additionalProperties', 'minLength', 'maxLength',
                     'minItems', 'maxItems', 'uniqueItems'}

    def clean_parameters(obj):
        """递归清理参数对象"""
        if isinstance(obj, dict):
            cleaned = {}
            for key, value in obj.items():
                if key in EXCLUDED_KEYS:
                    continue
                cleaned[key] = clean_parameters(value)
            return cleaned
        elif isinstance(obj, list):
            return [clean_parameters(item) for item in obj]
        else:
            return obj

    def get_value(obj, key, default=None):
        """从对象或字典中获取值"""
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    # 导入 clean_json_schema 函数用于清理 custom 工具的 input_schema
    from akarins_gateway.converters.anthropic_constants import clean_json_schema

    function_declarations = []

    for tool in tools:
        # 首先将 Pydantic 模型转换为字典（如果需要）
        if not isinstance(tool, dict):
            # 处理 Pydantic 模型对象
            if hasattr(tool, "model_dump"):
                tool = tool.model_dump()
            elif hasattr(tool, "dict"):
                tool = tool.dict()
            else:
                # 尝试使用 getattr 获取属性
                tool_dict = {}
                for attr in ["type", "function", "custom"]:
                    if hasattr(tool, attr):
                        value = getattr(tool, attr)
                        if hasattr(value, "model_dump"):
                            tool_dict[attr] = value.model_dump()
                        elif hasattr(value, "dict"):
                            tool_dict[attr] = value.dict()
                        else:
                            tool_dict[attr] = value
                tool = tool_dict

        # 支持字典和对象两种格式
        tool_type = get_value(tool, "type", "function")

        # DEBUG: Log tool structure
        if isinstance(tool, dict):
            tool_keys = list(tool.keys())
            log.debug(f"[ANTIGRAVITY] Processing tool: type={tool_type}, keys={tool_keys}")

        if tool_type == "function":
            function = get_value(tool, "function", None)

            # 如果 function 是 Pydantic 模型，转换为字典
            if function and not isinstance(function, dict):
                if hasattr(function, "model_dump"):
                    function = function.model_dump()
                elif hasattr(function, "dict"):
                    function = function.dict()
                else:
                    function = {k: getattr(function, k) for k in ["name", "description", "parameters"] if hasattr(function, k)}

            if function:
                func_name = get_value(function, "name")
                if not func_name:
                    log.warning(f"[ANTIGRAVITY] Skipping tool without function name")
                    continue

                func_desc = get_value(function, "description", "")
                func_params = get_value(function, "parameters", {})

                # 转换为字典（如果是 Pydantic 模型）
                if hasattr(func_params, "dict"):
                    func_params = func_params.dict()
                elif hasattr(func_params, "model_dump"):
                    func_params = func_params.model_dump()

                # 使用 clean_json_schema 清理参数（确保嵌套 object 类型正确处理）
                cleaned_params = clean_json_schema(func_params) if isinstance(func_params, dict) else clean_parameters(func_params)

                # 确保 parameters 有 type 字段（Antigravity 要求）
                if cleaned_params and "type" not in cleaned_params:
                    cleaned_params["type"] = "object"

                function_declarations.append({
                    "name": func_name,
                    "description": func_desc,
                    "parameters": cleaned_params
                })
        elif tool_type == "custom":
            # 处理 custom 类型的工具（Cursor 使用）
            log.warning(f"[ANTIGRAVITY] Received custom tool format (should have been normalized by gateway): {list(tool.keys()) if isinstance(tool, dict) else 'not a dict'}")

            custom_tool = get_value(tool, "custom", None)

            # 如果 custom_tool 是 Pydantic 模型，转换为字典
            if custom_tool and not isinstance(custom_tool, dict):
                if hasattr(custom_tool, "model_dump"):
                    custom_tool = custom_tool.model_dump()
                elif hasattr(custom_tool, "dict"):
                    custom_tool = custom_tool.dict()
                else:
                    custom_tool = {k: getattr(custom_tool, k) for k in ["name", "description", "input_schema"] if hasattr(custom_tool, k)}

            if custom_tool:
                func_name = get_value(custom_tool, "name")
                if not func_name:
                    log.warning(f"[ANTIGRAVITY] Skipping custom tool without name")
                    continue

                func_desc = get_value(custom_tool, "description", "")
                input_schema = get_value(custom_tool, "input_schema", {}) or {}

                # 如果 input_schema 是 Pydantic 模型，转换为字典
                if input_schema and not isinstance(input_schema, dict):
                    if hasattr(input_schema, "model_dump"):
                        input_schema = input_schema.model_dump()
                    elif hasattr(input_schema, "dict"):
                        input_schema = input_schema.dict()
                    else:
                        log.warning(f"[ANTIGRAVITY] input_schema is not a dict or Pydantic model: {type(input_schema)}")
                        input_schema = {}

                # 使用 clean_json_schema 清理 input_schema，确保有 type 字段
                cleaned_params = clean_json_schema(input_schema) if isinstance(input_schema, dict) else {}

                # 确保 parameters 有 type 字段（Antigravity 要求）
                if cleaned_params and "type" not in cleaned_params:
                    cleaned_params["type"] = "object"

                function_declarations.append({
                    "name": func_name,
                    "description": func_desc,
                    "parameters": cleaned_params
                })
            else:
                log.warning(f"[ANTIGRAVITY] Skipping custom tool without 'custom' field")

    # ✅ 新增：使用验证器对所有工具进行最终验证
    if function_declarations:
        validated_tools, validation_errors = validate_tools_batch(function_declarations)
        
        if validation_errors:
            log.warning(f"[ANTIGRAVITY] Tool validation found {len(validation_errors)} issues: {validation_errors[:3]}...")
        
        if validated_tools:
            log.info(f"[ANTIGRAVITY] Successfully validated {len(validated_tools)} tools out of {len(function_declarations)}")
            return [{"functionDeclarations": validated_tools}]
        else:
            log.error(f"[ANTIGRAVITY] All {len(function_declarations)} tools failed validation!")
            return None

    return None


def generate_generation_config(
    parameters: Dict[str, Any],
    enable_thinking: bool,
    model_name: str
) -> Dict[str, Any]:
    """
    生成 Antigravity generationConfig，使用 GeminiGenerationConfig 模型
    
    [FIX 2026-01-10] 添加双向限制策略：
    - 设置默认 maxOutputTokens（当客户端未指定时）
    - 添加上限保护（防止 429 错误）
    - thinking 模式下确保足够的输出空间
    """
    # 构建基础配置 - Stop sequences 使用列表变量
    stop_seqs = []
    # 添加常见的停止序列
    for tag in ["user", "bot", "context_request", "endoftext", "end_of_turn"]:
        stop_seqs.append("<|" + tag + "|>")

    config_dict = {
        "candidateCount": 1,
        "stopSequences": stop_seqs,
        "topK": parameters.get("top_k", 50),  # 默认值 50
    }

    # 添加可选参数
    if "temperature" in parameters:
        config_dict["temperature"] = parameters["temperature"]

    if "top_p" in parameters:
        config_dict["topP"] = parameters["top_p"]

    # [FIX 2026-01-10] 双向限制策略处理 maxOutputTokens
    # [FIX 2026-02-17] 使用模型感知的 maxOutputTokens 上限
    from akarins_gateway.converters.model_config import get_max_output_tokens as _get_model_max_tokens
    max_tokens = parameters.get("max_tokens")
    if max_tokens is not None:
        # 添加上限保护，防止过大的 max_tokens 导致 429 错误
        if isinstance(max_tokens, int) and max_tokens > MAX_ALLOWED_TOKENS:
            log.warning(
                f"[ANTIGRAVITY] maxOutputTokens 超过上限: {max_tokens} -> {MAX_ALLOWED_TOKENS}"
            )
            max_tokens = MAX_ALLOWED_TOKENS

        # [FIX 2026-01-12] 添加下限保护，防止客户端传来过小的 max_tokens 导致输出被截断
        # 这是之前修复都不生效的真正根因：antigravity_router 使用的是这个函数，不是 anthropic_converter 的！
        if isinstance(max_tokens, int) and max_tokens < MIN_OUTPUT_TOKENS:
            log.info(
                f"[ANTIGRAVITY] maxOutputTokens 低于下限: {max_tokens} -> {MIN_OUTPUT_TOKENS}"
            )
            max_tokens = MIN_OUTPUT_TOKENS

        # [FIX 2026-02-17] 应用模型感知上限
        max_tokens = _get_model_max_tokens(model_name, max_tokens)
        config_dict["maxOutputTokens"] = max_tokens
    else:
        # [FIX 2026-02-17] 使用模型感知的默认值
        model_default = _get_model_max_tokens(model_name, None)
        # 取模型默认值和全局默认值的较大者
        final_default = max(model_default, DEFAULT_MAX_OUTPUT_TOKENS)
        config_dict["maxOutputTokens"] = final_default
        log.debug(f"[ANTIGRAVITY] 使用模型感知默认 maxOutputTokens: {final_default} (model={model_name})")

    # 图片生成相关参数
    if "response_modalities" in parameters:
        config_dict["response_modalities"] = parameters["response_modalities"]

    if "image_config" in parameters:
        # [FIX 2026-02-17] imageSize 参数映射：将 OpenAI 格式 (如 "1024x1024") 映射到 Gemini 格式 ("1K"/"2K"/"4K")
        # 参考 Antigravity-Manager v4.1.18
        raw_config = parameters["image_config"]
        if isinstance(raw_config, dict):
            processed_config = dict(raw_config)
            # 检查是否有 size 字段（OpenAI 格式）需要映射
            openai_size = processed_config.pop("size", None)
            if openai_size:
                mapped_size = map_image_size(openai_size)
                if mapped_size and "image_size" not in processed_config and "imageSize" not in processed_config:
                    processed_config["image_size"] = mapped_size
                    log.info(f"[IMAGE SIZE] OpenAI size '{openai_size}' -> Gemini imageSize '{mapped_size}'")
            # 对已有的 image_size 字段做规范化映射（可能是 OpenAI "WxH" 格式）
            existing_size = processed_config.get("image_size") or processed_config.get("imageSize")
            if existing_size:
                mapped = map_image_size(existing_size)
                if mapped:
                    # 统一写入 image_size 字段（GeminiImageConfig 使用 snake_case）
                    processed_config.pop("imageSize", None)
                    processed_config["image_size"] = mapped
            config_dict["image_config"] = processed_config
        else:
            config_dict["image_config"] = raw_config

    # 思考模型配置
    if enable_thinking:
        # [FIX 2026-02-01] 使用统一的 DEFAULT_THINKING_BUDGET 常量，不再硬编码 1024
        # [FIX 2026-02-05] 懒加载导入，避免与 anthropic_converter.py 的循环依赖
        from akarins_gateway.converters.anthropic_constants import DEFAULT_THINKING_BUDGET
        thinking_budget = DEFAULT_THINKING_BUDGET
        
        # [FIX 2026-01-10] 双向限制策略：确保 thinking 模式下有足够的输出空间
        current_max_tokens = config_dict.get("maxOutputTokens", DEFAULT_MAX_OUTPUT_TOKENS)
        
        # Step 1: 计算需要的总 tokens
        required_tokens = thinking_budget + MIN_OUTPUT_TOKENS
        
        if required_tokens > MAX_ALLOWED_TOKENS:
            # 需要下调 thinkingBudget
            adjusted_budget = MAX_ALLOWED_TOKENS - MIN_OUTPUT_TOKENS
            if adjusted_budget > 0:
                thinking_budget = adjusted_budget
                log.info(
                    f"[ANTIGRAVITY][thinking] 双向限制生效：thinkingBudget 下调 1024 -> {adjusted_budget} "
                    f"(MAX_ALLOWED={MAX_ALLOWED_TOKENS}, MIN_OUTPUT={MIN_OUTPUT_TOKENS})"
                )
        
        # Step 2: 确保 max_tokens >= budget + MIN_OUTPUT_TOKENS
        min_required = thinking_budget + MIN_OUTPUT_TOKENS
        if current_max_tokens < min_required:
            new_max_tokens = min(min_required, MAX_ALLOWED_TOKENS)
            config_dict["maxOutputTokens"] = new_max_tokens
            log.info(
                f"[ANTIGRAVITY][thinking] 双向限制生效：maxOutputTokens 提升 {current_max_tokens} -> {new_max_tokens} "
                f"(thinkingBudget={thinking_budget}, 实际输出空间={new_max_tokens - thinking_budget})"
            )
        
        config_dict["thinkingConfig"] = {
            "includeThoughts": True,
            "thinkingBudget": thinking_budget
        }
        
        # [FIX 2026-01-17] 移除 thinkingLevel 避免与 thinkingBudget 冲突（官方版本修复）
        # 参考: gcli2api_official PR #291 (fix/thinking-budget-level-conflict)
        config_dict["thinkingConfig"].pop("thinkingLevel", None)

        # Claude 思考模型：删除 topP 参数
        if "claude" in model_name.lower():
            config_dict.pop("topP", None)

    # 使用 GeminiGenerationConfig 模型进行验证
    try:
        config = GeminiGenerationConfig(**config_dict)
        return config.model_dump(exclude_none=True)
    except Exception as e:
        log.warning(f"[ANTIGRAVITY] Failed to validate generation config: {e}, using dict directly")
        return config_dict